import hashlib
import secrets
from typing import Annotated

from app.config import settings
from app.database.profile_media_review import MEDIA_AVATAR
from app.database.user import User
from app.dependencies.cache import UserCacheService
from app.dependencies.database import Database
from app.dependencies.storage import StorageService
from app.dependencies.user import ClientUser
from app.log import log
from app.service.profile_media_review_service import record_media_upload
from app.service.suspicious_alert_service import SuspiciousAlertService
from app.utils import check_image

from .router import router

from fastapi import File, Form, Header, HTTPException

logger = log("Avatar")


async def _replace_avatar(
    session: Database,
    user: User,
    content: bytes,
    is_nsfw: bool,
    storage: StorageService,
    cache_service: UserCacheService,
) -> dict:
    """Guarda el avatar de un usuario y devuelve la url nueva.

    Vive aparte de la ruta porque hay dos formas de llegar: el cliente con su
    token, y torii-web con el token de servicio. Todo lo que importa (validar la
    imagen, borrar la anterior, dejar el rastro para moderacion, avisar si es
    nsfw, invalidar el cache) tiene que pasar igual por las dos, asi que se
    escribe una sola vez.
    """
    if await user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    # check file
    format_ = check_image(content, 5 * 1024 * 1024, 256, 256)

    if url := user.avatar_url:
        path = storage.get_file_name_by_url(url)
        if path:
            await storage.delete_file(path)

    filehash = hashlib.sha256(content).hexdigest()
    storage_path = f"avatars/{user.id}_{filehash}.png"
    if not await storage.is_exists(storage_path):
        await storage.write_file(storage_path, content, f"image/{format_}")
    url = await storage.get_file_url(storage_path)
    user.avatar_url = url
    user.avatar_nsfw = is_nsfw
    await record_media_upload(
        session,
        user_id=user.id,
        media_type=MEDIA_AVATAR,
        url=url,
        storage_path=storage_path,
        filehash=filehash,
        is_nsfw=is_nsfw,
    )
    if is_nsfw:
        # Best-effort: a failure here must never block the upload itself.
        try:
            await SuspiciousAlertService.alert_nsfw_media_upload(
                session,
                user_id=user.id,
                username=user.username,
                media_type=MEDIA_AVATAR,
                url=url,
                filehash=filehash,
            )
        except Exception:
            logger.warning(f"failed to enqueue NSFW media alert for user {user.id}", exc_info=True)
    await cache_service.invalidate_user_cache(user.id)
    await session.commit()

    return {
        "url": url,
        "avatar_url": url,
        "is_nsfw": is_nsfw,
        "filehash": filehash,
    }


async def _clear_avatar(
    session: Database,
    user: User,
    storage: StorageService,
    cache_service: UserCacheService,
) -> None:
    """Borra el avatar subido y deja al usuario con el que le toca por defecto."""
    if await user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    url = user.avatar_url
    path = None
    if url:
        path = storage.get_file_name_by_url(url)
        if path:
            await storage.delete_file(path)

    # Sacar la foto tambien la saca de la cola de revision. Antes no se hacia y
    # la fila quedaba pendiente para siempre apuntando a un archivo borrado: el
    # mod la abria, no habia nada, y no se iba nunca de la lista.
    if path:
        await record_media_upload(
            session,
            user_id=user.id,
            media_type=MEDIA_AVATAR,
            url=url,
            storage_path=path,
            filehash=None,
            is_nsfw=False,
        )

    # Back to the "no custom avatar" sentinel; the transform resolver then hands
    # out a default (the AI set, or the plain logo if the user opted out).
    user.avatar_url = User.DEFAULT_AVATAR_URL
    user.avatar_nsfw = False
    await cache_service.invalidate_user_cache(user.id)
    await session.commit()


@router.post("/avatar/upload", name="Upload Avatar", tags=["Users", "g0v0 API"])
async def upload_avatar(
    session: Database,
    content: Annotated[bytes, File(...)],
    current_user: ClientUser,
    storage: StorageService,
    cache_service: UserCacheService,
    is_nsfw: Annotated[bool, Form()] = False,
):
    """Upload user avatar

    Accepts image data, validates format and size, stores it to storage service, and updates the user's avatar URL.

    Constraints:
    - Supported formats: PNG, JPEG, GIF
    - Max file size: 5MB
    - Max image dimensions: 256x256 pixels

    Returns:
    - Avatar URL and file hash
    """
    return await _replace_avatar(session, current_user, content, is_nsfw, storage, cache_service)


@router.delete("/avatar", name="Delete Avatar", tags=["Users", "g0v0 API"], status_code=204)
async def delete_avatar(
    session: Database,
    current_user: ClientUser,
    storage: StorageService,
    cache_service: UserCacheService,
):
    """Delete the user's uploaded avatar and fall back to the default.

    Operates on the stored column value (never the emit-time default-N.png URL),
    so it only ever deletes a real custom upload, never a shared default image.
    """
    await _clear_avatar(session, current_user, storage, cache_service)


# -- torii-web -----------------------------------------------------------------
# torii-web (el osu-web nuestro) no puede pedir un token del usuario: el password
# grant exige turnstile para todo lo que no sea el cliente del juego, y meter un
# captcha en el login del sitio solo para poder subir una foto no tiene sentido.
# Pero el sitio YA autentica al usuario por su cuenta y YA escribe en lazer_users
# a traves de sus vistas, asi que confiar en el no agrega permisos que no tuviera.
#
# Entonces mismo trato que el bot de discord: un secreto compartido en el header
# y el id del usuario explicito en el cuerpo. Lo importante es que estas rutas no
# duplican nada, llaman a las mismas funciones que el cliente, asi que la imagen
# se valida igual, el rastro de moderacion queda igual y el cache se invalida
# igual. Si esto lo hiciera el sitio escribiendo el archivo a mano se perderia
# todo eso sin que nadie se entere.
def _validate_web_token(token: str | None) -> None:
    expected = (settings.torii_web_token or "").strip()
    provided = (token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="torii-web token is not configured")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid torii-web token")


async def _web_target_user(session: Database, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@router.post("/web/avatar/upload", name="Upload Avatar (torii-web)", tags=["Users", "g0v0 API"])
async def upload_avatar_from_web(
    session: Database,
    storage: StorageService,
    cache_service: UserCacheService,
    content: Annotated[bytes, File(...)],
    user_id: Annotated[int, Form()],
    is_nsfw: Annotated[bool, Form()] = False,
    x_torii_web_token: Annotated[str | None, Header(alias="X-Torii-Web-Token")] = None,
):
    _validate_web_token(x_torii_web_token)
    user = await _web_target_user(session, user_id)
    return await _replace_avatar(session, user, content, is_nsfw, storage, cache_service)


@router.post("/web/avatar/nsfw", name="Mark Avatar NSFW (torii-web)", tags=["Users", "g0v0 API"])
async def set_avatar_nsfw_from_web(
    session: Database,
    storage: StorageService,
    cache_service: UserCacheService,
    user_id: Annotated[int, Form()],
    is_nsfw: Annotated[bool, Form()],
    x_torii_web_token: Annotated[str | None, Header(alias="X-Torii-Web-Token")] = None,
):
    """Marca o desmarca como nsfw el avatar que el usuario YA tiene puesto.

    El cliente solo puede decidirlo al subir, asi que para corregir una foto mal
    etiquetada hay que volver a subirla. Desde el sitio se puede hacer bien: es
    una casilla que se guarda sola. Pasa por record_media_upload igual que una
    subida, asi marcarla la mete en la cola de revision y desmarcarla la saca.
    """
    _validate_web_token(x_torii_web_token)
    user = await _web_target_user(session, user_id)
    if await user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    user.avatar_nsfw = is_nsfw

    url = user.avatar_url
    storage_path = storage.get_file_name_by_url(url) if url else None
    has_custom = bool(url) and url != User.DEFAULT_AVATAR_URL and bool(storage_path)

    # Sin foto propia la casilla igual se guarda: asi se puede tildar ANTES de
    # subir y la subida sale ya marcada, que es el orden natural cuando ya sabes
    # que la foto es subida de tono. Lo que no se hace es meterla en la cola de
    # revision ni avisarle a nadie, porque todavia no hay nada que revisar.
    if has_custom:
        # avatars/{id}_{sha256}.png: el hash es lo que va entre el guion bajo y
        # la extension. Se saca de ahi para no releer el archivo entero solo
        # para tocar un booleano.
        filehash = storage_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].split("_", 1)[-1] or None
        await record_media_upload(
            session,
            user_id=user.id,
            media_type=MEDIA_AVATAR,
            url=url,
            storage_path=storage_path,
            filehash=filehash,
            is_nsfw=is_nsfw,
        )
    if is_nsfw and has_custom:
        try:
            await SuspiciousAlertService.alert_nsfw_media_upload(
                session,
                user_id=user.id,
                username=user.username,
                media_type=MEDIA_AVATAR,
                url=url,
                filehash=filehash,
            )
        except Exception:
            logger.warning(f"failed to enqueue NSFW media alert for user {user.id}", exc_info=True)
    await cache_service.invalidate_user_cache(user.id)
    await session.commit()

    return {"is_nsfw": is_nsfw}


@router.get("/web/avatar/state", name="estado del avatar (torii-web)", tags=["Users", "g0v0 API"])
async def get_avatar_state_from_web(
    session: Database,
    user_id: int,
    x_torii_web_token: Annotated[str | None, Header(alias="X-Torii-Web-Token")] = None,
):
    """Lo que el sitio necesita para dibujar la casilla: si hay foto propia y como esta marcada."""
    _validate_web_token(x_torii_web_token)
    user = await _web_target_user(session, user_id)
    url = user.avatar_url
    return {
        "has_custom_avatar": bool(url) and url != User.DEFAULT_AVATAR_URL,
        "is_nsfw": bool(user.avatar_nsfw),
    }


@router.delete("/web/avatar", name="Delete Avatar (torii-web)", tags=["Users", "g0v0 API"], status_code=204)
async def delete_avatar_from_web(
    session: Database,
    storage: StorageService,
    cache_service: UserCacheService,
    user_id: int,
    x_torii_web_token: Annotated[str | None, Header(alias="X-Torii-Web-Token")] = None,
):
    _validate_web_token(x_torii_web_token)
    user = await _web_target_user(session, user_id)
    await _clear_avatar(session, user, storage, cache_service)

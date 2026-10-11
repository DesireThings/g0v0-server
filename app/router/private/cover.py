import hashlib
from typing import Annotated

from app.database.profile_media_review import MEDIA_COVER
from app.database.user import User, UserProfileCover
from app.dependencies.cache import UserCacheService
from app.dependencies.database import Database
from app.dependencies.storage import StorageService
from app.dependencies.user import ClientUser
from app.log import log
from app.service.profile_media_review_service import record_media_upload
from app.service.suspicious_alert_service import SuspiciousAlertService
from app.utils import check_image

from .avatar import _validate_web_token, _web_target_user
from .router import router

from fastapi import File, Form, Header, HTTPException

logger = log("Cover")


async def _replace_cover(
    session: Database,
    user: User,
    content: bytes,
    is_nsfw: bool,
    storage: StorageService,
    cache_service: UserCacheService,
) -> dict:
    """Guarda la portada de un usuario y devuelve la url nueva.

    Igual que con el avatar, vive aparte de la ruta porque hay dos formas de
    llegar (el cliente con su token y torii-web con el de servicio) y todo lo que
    importa tiene que pasar por las dos.
    """
    if await user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    # check file
    format_ = check_image(content, 10 * 1024 * 1024, 3000, 2000)

    if url := user.cover["url"]:
        path = storage.get_file_name_by_url(url)
        if path:
            await storage.delete_file(path)

    filehash = hashlib.sha256(content).hexdigest()
    storage_path = f"cover/{user.id}_{filehash}.png"
    if not await storage.is_exists(storage_path):
        await storage.write_file(storage_path, content, f"image/{format_}")
    url = await storage.get_file_url(storage_path)
    user.cover = UserProfileCover(url=url)
    user.cover_nsfw = is_nsfw
    await record_media_upload(
        session,
        user_id=user.id,
        media_type=MEDIA_COVER,
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
                media_type=MEDIA_COVER,
                url=url,
                filehash=filehash,
            )
        except Exception:
            logger.warning(f"failed to enqueue NSFW media alert for user {user.id}", exc_info=True)
    await cache_service.invalidate_user_cache(user.id)
    await session.commit()

    return {
        "url": url,
        "cover_url": url,
        "is_nsfw": is_nsfw,
        "filehash": filehash,
    }


async def _clear_cover(
    session: Database,
    user: User,
    storage: StorageService,
    cache_service: UserCacheService,
) -> None:
    """Saca la portada propia y deja al usuario con la de por defecto.

    El avatar tenia como sacarlo desde el cliente y la portada no, asi que quien
    subia una quedaba atado a tener alguna para siempre: lo unico que podia hacer
    era taparla con otra.
    """
    if await user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    url = user.cover["url"] if user.cover else None
    path = storage.get_file_name_by_url(url) if url else None
    if path:
        await storage.delete_file(path)
        # Fuera de la cola de revision tambien, igual que con el avatar: si no,
        # la fila queda pendiente para siempre apuntando a un archivo borrado.
        await record_media_upload(
            session,
            user_id=user.id,
            media_type=MEDIA_COVER,
            url=url,
            storage_path=path,
            filehash=None,
            is_nsfw=False,
        )

    user.cover = UserProfileCover(url="")
    user.cover_nsfw = False
    await cache_service.invalidate_user_cache(user.id)
    await session.commit()


@router.post("/cover/upload", name="Upload Cover", tags=["Users", "g0v0 API"])
async def upload_cover(
    session: Database,
    content: Annotated[bytes, File(...)],
    current_user: ClientUser,
    storage: StorageService,
    cache_service: UserCacheService,
    is_nsfw: Annotated[bool, Form()] = False,
):
    """Upload user profile cover

    Accepts image data, validates format and size, stores it to storage service, and updates the user's cover URL.

    Constraints:
    - Supported formats: PNG, JPEG, GIF
    - Max file size: 10MB
    - Max image dimensions: 3000x2000 pixels

    Returns:
    - Cover URL and file hash
    """
    return await _replace_cover(session, current_user, content, is_nsfw, storage, cache_service)


@router.delete("/cover", name="Delete Cover", tags=["Users", "g0v0 API"], status_code=204)
async def delete_cover(
    session: Database,
    current_user: ClientUser,
    storage: StorageService,
    cache_service: UserCacheService,
):
    """Saca la portada propia y vuelve a la de por defecto."""
    await _clear_cover(session, current_user, storage, cache_service)


# -- torii-web -----------------------------------------------------------------
# Mismo trato que el avatar: secreto compartido en el header y el id del usuario
# explicito, porque el sitio no puede pedirse un token del usuario. Ver el
# comentario largo en avatar.py.
@router.post("/web/cover/upload", name="Upload Cover (torii-web)", tags=["Users", "g0v0 API"])
async def upload_cover_from_web(
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
    return await _replace_cover(session, user, content, is_nsfw, storage, cache_service)


@router.delete("/web/cover", name="Delete Cover (torii-web)", tags=["Users", "g0v0 API"], status_code=204)
async def delete_cover_from_web(
    session: Database,
    storage: StorageService,
    cache_service: UserCacheService,
    user_id: int,
    x_torii_web_token: Annotated[str | None, Header(alias="X-Torii-Web-Token")] = None,
):
    _validate_web_token(x_torii_web_token)
    user = await _web_target_user(session, user_id)
    await _clear_cover(session, user, storage, cache_service)

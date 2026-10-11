from datetime import datetime
from typing import Annotated

from app.database import Beatmap, Beatmapset, User
from app.database.beatmap import clear_cached_beatmap_raws
# NOTE: the previous parameter annotation was
#   Annotated[ClientUser, Security(get_current_user, scopes=["public"])]
# which is the only place in the whole router tree that combined a typedef
# carrying a Security marker (ClientUser = Annotated[User, Security(get_client_user, scopes=["*"])])
# with an additional Security() at the call site. Annotated flattens, so
# FastAPI ended up trying to satisfy BOTH dependencies — the
# `get_client_user` half hard-requires the `oauth2_password` scheme alone
# with scope `*`, while the `get_current_user` half wants any scheme with
# scope `public`. The two contradict and every BSS call landed in 401.
# Aligning with /api/v2/* pattern (`User` + a single `get_current_user`
# Security) is what makes lazer's password-grant token + OAuth-code
# clients both work, same as every other endpoint that authenticates a
# real user.
from app.dependencies.user import get_current_user
from app.log import logger

# ventana en la que varias subidas seguidas del mismo set cuentan como una sola
# noticia de "actualizado".
_UPDATE_FEED_COOLDOWN = 1800
from app.dependencies.cache import BeatmapsetCacheService
from app.dependencies.database import Database, Redis
from app.dependencies.storage import StorageService
from app.models.beatmap import BeatmapRankStatus
from app.models.beatmapset_upload import (
    PutBeatmapSetRequest,
    PutBeatmapSetResponse,
)
from app.service.beatmapset_upload_service import BeatmapsetUploadService
from fastapi import APIRouter, File, Form, HTTPException, Path, Security, UploadFile
from sqlalchemy import text
from sqlmodel import col, select

router = APIRouter(prefix="/beatmap-submission", tags=["beatmap submission"])

async def _remove_beatmap(db, beatmap: Beatmap) -> None:
    """Saca una diff de un set sin llevarse puesto lo que la gente ya jugo.

    Una diff que estuvo arriba junta cosas colgando de ella: los tiempos donde la gente
    falla, contadores de plays, tokens de score, y sobre todo SCORES. Borrar la fila a lo
    bruto choca contra esas referencias y la subida entera muere con un 500 sin
    explicacion (le paso a alguien juntando tres sets en uno).

    Lo que es derivado se borra; lo que es historia de alguien no se toca: en ese caso la
    diff se marca como borrada y deja de aparecer, pero los scores siguen existiendo.
    """
    # derivado: se puede regenerar o directamente no importa.
    if beatmap.failtimes is not None:
        await db.delete(beatmap.failtimes)

    for table in ("beatmap_playcounts", "score_tokens"):
        await db.exec(text(f"DELETE FROM {table} WHERE beatmap_id = :id").bindparams(id=beatmap.id))

    # historia de la gente: si hay algo, la diff se esconde en vez de borrarse.
    keeps_history = (
        await db.exec(
            text(
                """
                SELECT EXISTS (SELECT 1 FROM scores WHERE beatmap_id = :id)
                    OR EXISTS (SELECT 1 FROM best_scores WHERE beatmap_id = :id)
                    OR EXISTS (SELECT 1 FROM total_score_best_scores WHERE beatmap_id = :id)
                    OR EXISTS (SELECT 1 FROM room_playlists WHERE beatmap_id = :id)
                    OR EXISTS (SELECT 1 FROM matchmaking_pool_beatmaps WHERE beatmap_id = :id)
                """
            ).bindparams(id=beatmap.id)
        )
    ).one()

    if keeps_history:
        beatmap.deleted_at = datetime.utcnow()
        db.add(beatmap)
        logger.info("la diff {} tiene historia, se marca como borrada en vez de borrarla", beatmap.id)
        return

    await db.delete(beatmap)


def _local_submission_status(target: str | None) -> BeatmapRankStatus:
    # Keep local uploads scoreable without granting full ranked semantics/rewards.
    return BeatmapRankStatus.APPROVED


@router.put(
    "/beatmapsets",
    response_model=PutBeatmapSetResponse,
    name="Initialize Beatmapset Upload",
    description="Initialize the beatmapset upload process, returning beatmapset ID and existing file list.",
)
async def initialize_beatmapset_upload(
    db: Database,
    storage: StorageService,
    req: PutBeatmapSetRequest,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    cache_service: BeatmapsetCacheService,
    redis: Redis,
):
    if req.beatmapset_id:
        beatmapset = await db.get(Beatmapset, req.beatmapset_id)
        if not beatmapset:
            # Create a temporary beatmapset with the provided ID
            beatmapset = Beatmapset(
                id=req.beatmapset_id,
                artist=req.artist or "Unknown",
                artist_unicode=req.artist or "Unknown",
                title=req.title or "Unknown",
                title_unicode=req.title or "Unknown",
                creator=current_user.username,
                user_id=current_user.id,
                video=False,
                is_local=True,
                submitted_date=datetime.utcnow(),
                last_updated=datetime.utcnow(),
                beatmap_status=_local_submission_status(req.target),
            )
            db.add(beatmapset)
            await db.flush()
        elif beatmapset.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="You do not own this beatmapset")
        else:
            beatmapset.creator = current_user.username
            # Update metadata if provided
            if req.artist:
                beatmapset.artist = req.artist
                beatmapset.artist_unicode = req.artist
            if req.title:
                beatmapset.title = req.title
                beatmapset.title_unicode = req.title

        # Delete beatmaps not in beatmaps_to_keep
        deleted_ids: list[int] = []
        # ids que el cliente quiere conservar pero que no son de este set: una diff que
        # ya se subio suelta y ahora la meten adentro del set. no se le puede mudar el id
        # (seria robarselo al mapa original junto con sus scores), asi que se le reserva
        # uno nuevo, que es lo que el cliente le va a terminar poniendo.
        foreign_keep = 0

        if req.beatmaps_to_keep is not None:
            stmt = select(Beatmap).where(
                Beatmap.beatmapset_id == beatmapset.id,
                ~col(Beatmap.id).in_(req.beatmaps_to_keep),
            )
            to_delete = (await db.exec(stmt)).all()
            deleted_ids = [b.id for b in to_delete]

            for b in to_delete:
                await _remove_beatmap(db, b)

            own_ids = set(
                (
                    await db.exec(
                        select(Beatmap.id).where(Beatmap.beatmapset_id == beatmapset.id)
                    )
                ).all()
            )
            foreign_keep = len([i for i in req.beatmaps_to_keep if i not in own_ids])

        # Update status if target is provided
        beatmapset.beatmap_status = _local_submission_status(req.target)

        existing_files = await BeatmapsetUploadService.get_beatmapset_files(storage, beatmapset.id)

        # Allocate new beatmaps if needed
        to_create = req.beatmaps_to_create + foreign_keep

        if to_create > 0:
            await BeatmapsetUploadService.allocate_beatmaps(
                db, beatmapset.id, current_user.id, to_create
            )

        beatmapset_id = beatmapset.id
        await db.commit()

        # Return the complete server-assigned beatmap ID list for this set.
        beatmap_ids = (
            await db.exec(
                select(Beatmap.id).where(Beatmap.beatmapset_id == beatmapset_id).order_by(Beatmap.id.asc())
            )
        ).all()

        # las diffs borradas hay que sacarlas de la cache aca: el flujo de upload solo
        # invalida las que quedaron, asi que una diff borrada seguiria contestando por
        # /beatmaps/{id} hasta que la cache expire sola.
        if deleted_ids:
            try:
                await cache_service.invalidate_beatmapset_cache(beatmapset_id)
                for bid in deleted_ids:
                    await cache_service.invalidate_beatmap_lookup_cache(bid)
                await clear_cached_beatmap_raws(redis, deleted_ids)
            except Exception:
                logger.warning("no se pudo invalidar la cache de las diffs borradas: {}", deleted_ids)
    else:
        # Create a temporary beatmapset with default values
        # Custom ID generation: find the max ID in the 800,000,000 range and increment
        from sqlmodel import func
        stmt = select(func.max(Beatmapset.id)).where(Beatmapset.id >= 800000000)
        max_id = (await db.exec(stmt)).first()
        new_id = max(800000000, (max_id or 800000000)) + 1

        beatmapset = Beatmapset(
            id=new_id,
            artist=req.artist or "Unknown",
            artist_unicode=req.artist or "Unknown",
            title=req.title or "Unknown",
            title_unicode=req.title or "Unknown",
            creator=current_user.username,
            user_id=current_user.id,
            video=False,
            is_local=True,
            submitted_date=datetime.utcnow(),
            last_updated=datetime.utcnow(),
            beatmap_status=_local_submission_status(req.target),
        )
        db.add(beatmapset)
        await db.flush()

        beatmapset_id = beatmapset.id
        existing_files = []
        # Allocate placeholders for the beatmaps being uploaded
        await BeatmapsetUploadService.allocate_beatmaps(
            db, beatmapset_id, current_user.id, req.beatmaps_to_create
        )
        await db.commit()

        beatmap_ids = (
            await db.exec(
                select(Beatmap.id).where(Beatmap.beatmapset_id == beatmapset_id).order_by(Beatmap.id.asc())
            )
        ).all()

    return PutBeatmapSetResponse(
        beatmapset_id=beatmapset_id,
        beatmap_ids=beatmap_ids,
        files=existing_files,
    )


@router.put(
    "/beatmapsets/{beatmapset_id}",
    name="Upload Full Beatmapset",
    description="Upload a complete .osz beatmapset archive.",
    response_model=None,
)
async def upload_beatmapset_package(
    db: Database,
    redis: Redis,
    storage: StorageService,
    cache_service: BeatmapsetCacheService,
    beatmapset_id: Annotated[int, Path(..., description="Beatmapset ID")],
    beatmapArchive: Annotated[UploadFile, File(description="OSZ archive file")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
):
    beatmapset = await db.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")
    if beatmapset.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not own this beatmapset")

    # el mapper si lo snapshoteamos aca (current_user no se expira con el commit del set)
    feed_mapper = current_user.username
    feed_mapper_id = current_user.id

    content = await beatmapArchive.read()
    await storage.write_file(f"beatmapsets/{beatmapset_id}.osz", content)

    updated_ids = await BeatmapsetUploadService.process_beatmapset_package(db, storage, beatmapset_id)

    # evento al #feed de discord: la primera subida se anuncia como mapa nuevo y las
    # siguientes como update. el update tiene su propio cooldown porque subir un mapa
    # es de a tandas (arreglas algo, resubis, arreglas otra cosa) y no queremos un
    # mensaje por cada intento.
    try:
        from app.service.discord_feed import notify_beatmapset_updated, notify_beatmapset_uploaded

        is_first_upload = await redis.set(f"feed:bss:{beatmapset_id}", "1", nx=True)
        announce_update = False

        if not is_first_upload:
            announce_update = bool(
                await redis.set(f"feed:bss:update:{beatmapset_id}", "1", ex=_UPDATE_FEED_COOLDOWN, nx=True)
            )

        if is_first_upload or announce_update:
            # leer artist/title DESPUES del process: el .osz recien ahi setea la metadata real.
            # antes se snapshoteaba ANTES de process_beatmapset_package y salia "Unknown - Unknown"
            # (el PUT de create trae metadata vacia; el titulo/artista viven en el .osz).
            await db.refresh(beatmapset)

            announce = notify_beatmapset_uploaded if is_first_upload else notify_beatmapset_updated
            announce(
                username=feed_mapper,
                user_id=feed_mapper_id,
                artist=beatmapset.artist or "Unknown",
                title=beatmapset.title or "Unknown",
                beatmapset_id=beatmapset_id,
            )
    except Exception:
        pass

    # Invalidate caches
    await cache_service.invalidate_beatmapset_cache(beatmapset_id)
    for bid in updated_ids:
        await cache_service.invalidate_beatmap_lookup_cache(bid)
    await clear_cached_beatmap_raws(redis, updated_ids)

    return {"status": "success"}


@router.patch(
    "/beatmapsets/{beatmapset_id}",
    name="Incrementally Update Beatmapset",
    description="Incrementally upload modified files or delete files.",
    response_model=None,
)
async def patch_beatmapset_package(
    db: Database,
    redis: Redis,
    storage: StorageService,
    cache_service: BeatmapsetCacheService,
    beatmapset_id: Annotated[int, Path(..., description="Beatmapset ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    filesChanged: Annotated[list[UploadFile] | None, File()] = None,
    filesDeleted: Annotated[list[str] | None, Form()] = None,
):
    beatmapset = await db.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")
    if beatmapset.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not own this beatmapset")

    changed = []
    if filesChanged:
        for f in filesChanged:
            changed.append((f.filename, await f.read()))

    deleted = filesDeleted or []

    await BeatmapsetUploadService.patch_beatmapset_package(storage, beatmapset_id, changed, deleted)

    updated_ids = await BeatmapsetUploadService.process_beatmapset_package(db, storage, beatmapset_id)

    # Invalidate caches
    await cache_service.invalidate_beatmapset_cache(beatmapset_id)
    for bid in updated_ids:
        await cache_service.invalidate_beatmap_lookup_cache(bid)
    await clear_cached_beatmap_raws(redis, updated_ids)

    return {"status": "success"}



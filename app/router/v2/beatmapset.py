import asyncio
import os
import re
from typing import Annotated, Literal
from urllib.parse import parse_qs
import ipaddress

from app.config import settings
from app.database import (
    Beatmap,
    Beatmapset,
    BeatmapsetModel,
    FavouriteBeatmapset,
    SearchBeatmapsetsResp,
    User,
)
from app.dependencies.beatmap_download import DownloadService
from app.dependencies.cache import BeatmapsetCacheService, UserCacheService
from app.dependencies.database import Database, Redis, with_db
from app.dependencies.fetcher import Fetcher
from app.dependencies.geoip import IPAddress, get_geoip_helper
from app.dependencies.storage import StorageService
from app.dependencies.user import ClientUser, get_optional_user
from app.helpers.asset_proxy_helper import asset_proxy_response
from app.models.beatmap import BeatmapRankStatus, SearchQueryModel
from app.models.score import GameMode
from app.service.beatmapset_cache_service import generate_hash
from app.utils import api_doc

from .router import router

from fastapi import (
    BackgroundTasks,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Security,
)
from fastapi.responses import RedirectResponse
from httpx import HTTPError
from sqlalchemy import or_
from sqlmodel import col, select

import httpx
import logging

logger = logging.getLogger(__name__)


def _status_filters_from_query(status: str) -> list[BeatmapRankStatus]:
    if status == "leaderboard":
        if settings.enable_all_beatmap_leaderboard:
            return list(BeatmapRankStatus)
        return [
            BeatmapRankStatus.RANKED,
            BeatmapRankStatus.APPROVED,
            BeatmapRankStatus.QUALIFIED,
            BeatmapRankStatus.LOVED,
        ]
    if status == "ranked":
        return [BeatmapRankStatus.RANKED]
    if status == "qualified":
        return [BeatmapRankStatus.QUALIFIED]
    if status == "loved":
        return [BeatmapRankStatus.LOVED]
    if status == "pending":
        return [BeatmapRankStatus.PENDING]
    if status == "wip":
        return [BeatmapRankStatus.WIP]
    if status == "graveyard":
        return [BeatmapRankStatus.GRAVEYARD]
    return []


async def _search_local_beatmapsets(
    db: Database,
    query: SearchQueryModel,
    current_user: User | None,
) -> SearchBeatmapsetsResp:
    data = await _local_sets(db, query, current_user, limit=50)
    return SearchBeatmapsetsResp(total=len(data), beatmapsets=data)


def _is_ai(item: dict) -> bool:
    """Si el set salio de una generacion.

    Para los de aca manda la columna, que se escribe en la subida mirando el archivo
    que el generador deja adentro del set y no se puede perder editando la metadata.
    Para los de bancho no hay columna que valga, asi que queda el tag: si su autor
    genero el mapa con la tool y dejo el tag puesto, se marca igual, que es lo que
    corresponde.
    """
    if item.get("ai"):
        return True

    return "mapperatorinator" in (item.get("tags") or "").lower()


def _listing_date(item: dict) -> str:
    """La fecha con la que un set entra en un listado "lo ultimo".

    Los sets subidos aca no tienen ranked_date (nunca pasaron por el proceso de
    ranking de osu!), asi que se cae a last_updated, que es la que tienen.
    """
    for key in ("ranked_date", "last_updated", "submitted_date"):
        value = item.get(key)
        if value:
            return str(value)

    return ""


async def _local_sets(
    db: Database,
    query: SearchQueryModel,
    current_user: User | None,
    limit: int,
) -> list:
    stmt = (
        select(Beatmapset)
        .where(col(Beatmapset.is_local).is_(True))
        .order_by(col(Beatmapset.last_updated).desc(), col(Beatmapset.id).desc())
        .limit(limit)
    )

    if query.q:
        q = f"%{query.q.strip()}%"
        stmt = stmt.where(
            or_(
                col(Beatmapset.title).ilike(q),
                col(Beatmapset.title_unicode).ilike(q),
                col(Beatmapset.artist).ilike(q),
                col(Beatmapset.artist_unicode).ilike(q),
                col(Beatmapset.creator).ilike(q),
                col(Beatmapset.tags).ilike(q),
            )
        )

    # la columna la escribe la subida; el tag queda de respaldo para lo viejo.
    if query.ai:
        stmt = stmt.where(
            or_(
                col(Beatmapset.ai).is_(True),
                col(Beatmapset.tags).ilike("%mapperatorinator%"),
            )
        )

    status_filters = _status_filters_from_query(query.s)
    if status_filters:
        stmt = stmt.where(col(Beatmapset.beatmap_status).in_(status_filters))

    if query.m is not None:
        try:
            mode = GameMode.from_int(query.m)
            stmt = stmt.where(Beatmapset.beatmaps.any(Beatmap.mode == mode))
        except Exception:
            return []

    beatmapsets = (await db.exec(stmt)).all()
    includes = _beatmapset_includes_for_user(current_user)
    data = [await BeatmapsetModel.transform(bmset, user=current_user, includes=includes) for bmset in beatmapsets]

    # el transform no trae tags por esta ruta y el cliente los necesita para saber
    # que mapa salio de mapperatorinator (el badge de IA). se piden aparte, por
    # columna, que es lo unico que no depende de como este cargada la fila.
    ids = [bmset.id for bmset in beatmapsets]
    if ids:
        tags_by_id = {
            row[0]: row[1] or ""
            for row in (await db.exec(select(Beatmapset.id, Beatmapset.tags).where(col(Beatmapset.id).in_(ids)))).all()
        }
        ai_by_id = {
            row[0]: bool(row[1])
            for row in (await db.exec(select(Beatmapset.id, Beatmapset.ai).where(col(Beatmapset.id).in_(ids)))).all()
        }

        for item in data:
            item["tags"] = tags_by_id.get(item.get("id"), item.get("tags") or "")
            item["ai"] = ai_by_id.get(item.get("id"), bool(item.get("ai")))

    if query.hide_ai:
        data = [item for item in data if not _is_ai(item)]

    return data


async def _mix_in_local(
    db: Database,
    query: SearchQueryModel,
    current_user: User | None,
    remote: SearchBeatmapsetsResp,
    first_page: bool,
) -> SearchBeatmapsetsResp:
    """Mete los mapas subidos aca en el listado que viene de osu!.

    El listado por defecto es un proxy a osu.ppy.sh, que obviamente no conoce
    nada de lo que se sube a Torii: por eso los mapas de la gente de aca no
    aparecian nunca. Se mezclan en la primera pagina (las siguientes son cursores
    de osu!, ahi no hay donde meterlos sin romper la paginacion).
    """
    items = list(remote.beatmapsets)

    if query.hide_ai:
        items = [item for item in items if not _is_ai(item)]

    if query.hide_local or not first_page:
        return SearchBeatmapsetsResp(
            total=remote.total if not query.hide_ai else len(items),
            beatmapsets=items,
            cursor=remote.cursor,
            cursor_string=remote.cursor_string,
        )

    # buscando algo puntual conviene traer varios; en el listado pelado, un puñado,
    # que si no los de aca (que son los mas nuevos) tapan toda la primera pagina.
    local = await _local_sets(db, query, current_user, limit=24 if query.q else 8)

    if not local:
        return SearchBeatmapsetsResp(
            total=remote.total if not query.hide_ai else len(items),
            beatmapsets=items,
            cursor=remote.cursor,
            cursor_string=remote.cursor_string,
        )

    # con un orden por fecha se intercalan donde les toca; con cualquier otro
    # (relevancia, dificultad, popularidad) no hay clave comparable entre las dos
    # fuentes, asi que van primero y listo.
    if (query.sort or "").startswith(("ranked", "updated")) or not query.sort:
        merged = sorted(local + items, key=_listing_date, reverse=True)
    else:
        merged = local + items

    return SearchBeatmapsetsResp(
        total=(remote.total or 0) + len(local),
        beatmapsets=merged,
        cursor=remote.cursor,
        cursor_string=remote.cursor_string,
    )


def _beatmapset_includes_for_user(user: User | None) -> list[str]:
    if user is not None:
        return BeatmapsetModel.API_INCLUDES
    return [
        include
        for include in BeatmapsetModel.API_INCLUDES
        if not include.startswith("beatmaps.current_user_") and include != "current_user_attributes"
    ]


@router.get(
    "/beatmapsets/search",
    name="Search Beatmapsets",
    tags=["Beatmapsets"],
    response_model=SearchBeatmapsetsResp,
)
@asset_proxy_response
async def search_beatmapset(
    db: Database,
    query: Annotated[SearchQueryModel, Query()],
    request: Request,
    background_tasks: BackgroundTasks,
    fetcher: Fetcher,
    redis: Redis,
    cache_service: BeatmapsetCacheService,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    # los mapas generados con IA solo existen aca, asi que pedirlos implica busqueda local.
    if query.is_local or query.ai:
        return await _search_local_beatmapsets(db, query, current_user)

    params = parse_qs(qs=request.url.query, keep_blank_values=True)
    cursor = {}

    # Parse parameters in cursor[field] format
    for k, v in params.items():
        match = re.match(r"cursor\[(\w+)\]", k)
        if match:
            field_name = match.group(1)
            field_value = v[0] if v else None
            if field_value is not None:
                # Convert to appropriate type
                try:
                    if field_name in ["approved_date", "id"]:
                        cursor[field_name] = int(field_value)
                    else:
                        # Try converting to numeric type
                        try:
                            # First try converting to integer
                            cursor[field_name] = int(field_value)
                        except ValueError:
                            try:
                                # Then try converting to float
                                cursor[field_name] = float(field_value)
                            except ValueError:
                                # Finally keep as string
                                cursor[field_name] = field_value
                except ValueError:
                    cursor[field_name] = field_value

    if (
        "recommended" in query.c
        or len(query.r) > 0
        or query.played
        or "follows" in query.c
        or "mine" in query.s
        or "favourites" in query.s
    ):
        # TODO: search locally
        return SearchBeatmapsetsResp(total=0, beatmapsets=[])

    # Generate hash of query and cursor for caching
    query_hash = generate_hash(query.model_dump())
    cursor_hash = generate_hash(cursor)

    # Try to get search results from cache
    # la parte de osu! se cachea sola; los mapas de aca se mezclan despues, sin
    # cache, asi que una subida nueva aparece en el listado al instante.
    first_page = not cursor and not query.cursor_string

    cached_result = await cache_service.get_search_from_cache(query_hash, cursor_hash)
    if cached_result:
        sets = SearchBeatmapsetsResp(**cached_result)
        return await _mix_in_local(db, query, current_user, sets, first_page)

    try:
        sets = await fetcher.search_beatmapset(query, cursor, redis)

        # Cache search results
        await cache_service.cache_search_result(query_hash, cursor_hash, sets.model_dump())
        return await _mix_in_local(db, query, current_user, sets, first_page)
    except HTTPError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/beatmapsets/lookup",
    tags=["Beatmapsets"],
    responses={200: api_doc("Beatmapset details", BeatmapsetModel, BeatmapsetModel.BEATMAPSET_TRANSFORMER_INCLUDES)},
    name="Lookup Beatmapset by Beatmap ID",
    description=("Lookup parent beatmapset by beatmap ID."),
)
@asset_proxy_response
async def lookup_beatmapset(
    db: Database,
    request: Request,
    beatmap_id: Annotated[int, Query(description="Beatmap ID")],
    fetcher: Fetcher,
    cache_service: BeatmapsetCacheService,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    # Try getting from cache first
    cached_resp = await cache_service.get_beatmap_lookup_from_cache(beatmap_id)
    if cached_resp:
        return cached_resp

    try:
        beatmap = await Beatmap.get_or_fetch(db, fetcher, bid=beatmap_id)

        resp = await BeatmapsetModel.transform(
            beatmap.beatmapset,
            user=current_user,
            includes=_beatmapset_includes_for_user(current_user),
        )

        # Cache result
        await cache_service.cache_beatmap_lookup(beatmap_id, resp)
        return resp
    except HTTPError as exc:
        raise HTTPException(status_code=404, detail="Beatmap not found") from exc


@router.get(
    "/beatmapsets/{beatmapset_id}",
    tags=["Beatmapsets"],
    responses={200: api_doc("Beatmapset details", BeatmapsetModel, BeatmapsetModel.BEATMAPSET_TRANSFORMER_INCLUDES)},
    name="Get Beatmapset Details",
    description="Get details for a single beatmapset.",
)
@asset_proxy_response
async def get_beatmapset(
    db: Database,
    request: Request,
    beatmapset_id: Annotated[int, Path(..., description="Beatmapset ID")],
    fetcher: Fetcher,
    cache_service: BeatmapsetCacheService,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    # Try getting from cache first
    cached_resp = await cache_service.get_beatmapset_from_cache(beatmapset_id)
    if cached_resp:
        return cached_resp

    try:
        beatmapset = await Beatmapset.get_or_fetch(db, fetcher, beatmapset_id)
        if current_user is not None:
            await db.refresh(current_user)
        resp = await BeatmapsetModel.transform(
            beatmapset,
            includes=_beatmapset_includes_for_user(current_user),
            user=current_user,
        )

        # Cache result
        await cache_service.cache_beatmapset(resp)
        return resp
    except HTTPError as exc:
        raise HTTPException(status_code=404, detail="Beatmapset not found") from exc


# ──────────────────────────────────────────────────────────────────────────
# Beatmap download proxy
#
# Goal: make the "Download" click feel instant even under bursty traffic
# and partial mirror failures. Layered strategy, fastest first:
#
#   0. Local cache hit. If we proxied this map before, redirect to the
#      cached file. ~40ms TTFB.
#   1. Hedged race — fire requests to BOTH Nerinyan AND osu.direct at the
#      same time. First valid response wins, the loser is cancelled
#      mid-flight. Stress-tested numbers:
#        - Nerinyan: 30-50ms TTFB. But its catalogue isn't complete; some
#          maps return 302 → CDN → 404 after a 5s tail.
#        - osu.direct: 500-1000ms TTFB. Slower per-request but its
#          catalogue covers a different (broader) set of maps.
#      Their failure modes are complementary, so racing them gives
#      sub-50ms response on the common path AND covers each other's
#      catalogue gaps without a slow serial failover.
#   2. Sequential fallback — Gatari (~1s, decent coverage), then
#      BeatConnect (paid token, but real-world testing shows the token
#      still hits per-IP rate limits at ~10rpm; useful as a niche
#      fallback for niche maps but unsafe as a primary).
#   3. Tee-and-cache — the chosen mirror's stream is duplicated: bytes
#      flow to the client unchanged AND accumulate in a buffer that's
#      flushed to local storage after EOF, so the next request hits
#      Layer 0.
#   4. Negative cache — if every mirror 4xx-s for the same beatmap, we
#      mark it "unavailable" in Redis with a 5-minute TTL. The lazer
#      client retries automatically on failure; without negative caching
#      every retry would spam every mirror again. With it, retries
#      return 503 instantly and the mirrors get a breather.
#
# Catboy is intentionally not in the chain (confirmed unreliable by the
# operator). Chimu is gone (project shut down).
# ──────────────────────────────────────────────────────────────────────────

# Cap the in-memory buffer per request so a malicious or accidental gigabyte
# response doesn't OOM the server. Real osz files top out around 80MB — 200MB
# leaves plenty of headroom while still being safely sub-RAM.
_MAX_CACHE_BYTES = 200 * 1024 * 1024

# Transient HTTP statuses that warrant a single in-place retry before giving
# up on a mirror (it's almost certainly rate-limited or briefly overloaded).
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


async def _attempt_mirror(
    mirror_url: str,
    headers: dict[str, str] | None = None,
    *,
    label: str | None = None,
) -> httpx.Response | None:
    """Issue a streaming GET to `mirror_url`. Returns the open response (the
    caller must aclose it and the underlying client) on success, or None on
    failure / non-200 / non-osz body. Includes one retry on transient 5xx.

    The httpx AsyncClient is intentionally created per-call (not shared) so
    each request gets its own connection pool — a slow upstream stream on
    one beatmap can't choke the connection pool for unrelated requests.
    """
    label = label or mirror_url
    # Tighter timeout than before so failover is faster: 3s connect (LAN-ish
    # mirror or it's not coming up at all), 30s read (some big maps + slow
    # mirrors), 10s pool (per-client only).
    proxy_timeout = httpx.Timeout(connect=3.0, read=30.0, write=5.0, pool=10.0)
    for attempt in range(2):
        client: httpx.AsyncClient | None = None
        try:
            client = httpx.AsyncClient(follow_redirects=True, timeout=proxy_timeout)
            resp = await client.send(
                client.build_request("GET", mirror_url, headers=headers or {}),
                stream=True,
            )
        except Exception as e:
            if client is not None:
                await client.aclose()
            logger.warning(f"[beatmap dl] {label} attempt {attempt + 1} threw: {e}")
            if attempt == 0:
                await asyncio.sleep(0.4)
                continue
            return None

        if resp.status_code in _TRANSIENT_STATUS and attempt == 0:
            await resp.aclose()
            await client.aclose()
            logger.info(
                f"[beatmap dl] {label} returned {resp.status_code}; retrying once after backoff"
            )
            await asyncio.sleep(0.5)
            continue

        if resp.status_code >= 400:
            await resp.aclose()
            await client.aclose()
            logger.warning(f"[beatmap dl] {label} -> HTTP {resp.status_code}, giving up on this mirror")
            return None

        # Stash the client on the response so the caller can close both.
        resp.extensions["_torii_owned_client"] = client  # type: ignore[index]
        return resp
    return None


async def _validate_osz_first_chunk(resp: httpx.Response):
    """Peek at the first chunk and require the PK zip signature NO MATTER what
    Content-Type the mirror claims. Nerinyan (entre otros) a veces devuelve un
    200 con body JSON/HTML pero content-type de zip/octet-stream; el atajo que
    confiaba en el content-type dejaba pasar esa basura, se streameaba al
    cliente Y quedaba cacheada como .osz envenenado permanente ("beatmap
    import failed" para siempre). Los bytes no mienten: siempre miramos.
    Returns (is_valid, first_chunk, replacement_iterator). The iterator
    re-yields first_chunk before continuing the stream so the consumer
    doesn't lose those bytes.
    """
    stream_iter = resp.aiter_bytes(chunk_size=65536)
    first_chunk = await anext(stream_iter, b"")
    if not first_chunk.startswith(b"PK"):
        return False, first_chunk, stream_iter

    async def patched():
        yield first_chunk
        async for c in stream_iter:
            yield c

    return True, first_chunk, patched()


# EOCD (end of central directory, PK\x05\x06) vive en los ultimos <=65557
# bytes de todo zip valido. Un osz truncado a mitad de stream no lo tiene.
_ZIP_EOCD_WINDOW = 66000


def _payload_is_complete_osz(payload: bytes, content_length: str | None) -> bool:
    """Valida que el payload completo sea un zip integro antes de cachearlo:
    tamano == Content-Length del mirror (si lo declaro), magic PK al arranque,
    y EOCD al final. Evita cachear streams cortados/bodies de error."""
    if content_length:
        try:
            if len(payload) != int(content_length):
                return False
        except ValueError:
            pass
    if not payload.startswith(b"PK"):
        return False
    return b"PK\x05\x06" in payload[-_ZIP_EOCD_WINDOW:]


async def _cached_osz_looks_valid(storage, cache_path: str) -> bool:
    """Chequeo barato de integridad de un .osz cacheado antes de servirlo
    (head PK + EOCD en la cola, sin leer el archivo entero en local storage).
    Self-heal: los caches envenenados por bugs viejos (JSON/HTML/truncados)
    se detectan aca, el caller los borra y re-fetchea de los mirrors."""
    try:
        get_path = getattr(storage, "_get_file_path", None)
        if get_path is not None:
            p = get_path(cache_path)
            size = os.path.getsize(p)
            if size < 1024:
                return False
            with open(p, "rb") as f:
                head = f.read(4)
                tail_len = min(size, _ZIP_EOCD_WINDOW)
                f.seek(size - tail_len)
                tail = f.read(tail_len)
            return head.startswith(b"PK") and b"PK\x05\x06" in tail
        data = await storage.read_file(cache_path)
        return len(data) >= 1024 and data.startswith(b"PK") and b"PK\x05\x06" in data[-_ZIP_EOCD_WINDOW:]
    except Exception as e:
        logger.warning(f"[beatmap dl] cache validation error for {cache_path}: {e}")
        # ante la duda no rompemos el camino rapido: servimos lo que hay
        return True


async def _close_response(resp: httpx.Response | None) -> None:
    """Best-effort cleanup of a streaming httpx response and its owned client."""
    if resp is None:
        return
    client = resp.extensions.get("_torii_owned_client") if hasattr(resp, "extensions") else None  # type: ignore[union-attr]
    try:
        await resp.aclose()
    except Exception:
        pass
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass


async def _race_mirror_pair(
    candidates: list[tuple[str, dict[str, str] | None, str]],
) -> tuple[httpx.Response | None, str | None]:
    """Fire `_attempt_mirror` on every candidate in parallel; return the first
    one that produces a valid response, cancelling the rest.

    Behaviour:
      * If the first task to finish returns a Response, declare it the winner
        and cancel pending tasks. Their httpx clients close via the cancellation
        path / our finally cleanup.
      * If the first task returns None (failure), keep waiting for the next.
      * If every task returns None, return (None, None) so the caller can fall
        through to the slower serial chain.
    """
    if not candidates:
        return None, None

    tasks: dict[asyncio.Task, str] = {}
    for url, headers, label in candidates:
        task = asyncio.create_task(_attempt_mirror(url, headers, label=label))
        tasks[task] = label

    pending: set[asyncio.Task] = set(tasks.keys())
    winner: httpx.Response | None = None
    winner_label: str | None = None

    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                label = tasks[task]
                try:
                    resp = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception as e:
                    logger.warning(f"[beatmap dl] race task {label} raised: {e}")
                    continue
                if resp is not None and winner is None:
                    winner = resp
                    winner_label = label
                    break
            if winner is not None:
                break
    finally:
        # Clean up losers — cancel any still-pending tasks and close any
        # already-completed responses we didn't pick.
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        for task in tasks:
            if task in pending or task is None:
                continue
            if winner is not None and task.done():
                # Already accounted for above (we checked done set), but the
                # done set might include tasks we never inspected because we
                # broke out early after picking the winner. Close any extra
                # responses to avoid socket leaks.
                if not task.cancelled():
                    try:
                        result = task.result()
                    except Exception:
                        result = None
                    if result is not None and result is not winner:
                        await _close_response(result)

    return winner, winner_label


_bg_register_tasks: set[asyncio.Task] = set()


def ensure_beatmaps_registered_in_background(beatmapset_id: int, fetcher: Fetcher) -> None:
    """Fire-and-forget: make sure a downloaded set's beatmaps land in our DB.

    Runs concurrently with the (much slower) .osz download so the client's follow-up
    online-metadata / leaderboard lookup is a fast DB hit returning the correct status,
    instead of a slow on-demand mirror fetch that resolves too late and leaves the
    leaderboard reading "not available" until the user switches maps and back. This is
    server side on purpose: it also fixes the official lazer client + injector, which we
    cannot patch client-side.
    """

    async def _run():
        try:
            async with with_db() as db:
                if await db.get(Beatmapset, beatmapset_id) is not None:
                    return  # already registered, and its beatmaps come with it
                await Beatmapset.get_or_fetch(db, fetcher, beatmapset_id)
                await db.commit()
                logger.info(f"[beatmap dl] pre-registered set {beatmapset_id} for metadata/leaderboard lookup")
        except Exception as e:
            logger.warning(f"[beatmap dl] background beatmap registration for {beatmapset_id} failed: {e}")

    task = asyncio.create_task(_run())
    _bg_register_tasks.add(task)
    task.add_done_callback(_bg_register_tasks.discard)


@router.get("/beatmapsets/{beatmapset_id}/download", tags=["Beatmapsets"])
async def download_beatmapset(
    storage: StorageService,
    redis: Redis,
    client_ip: IPAddress,
    beatmapset_id: Annotated[int, Path(..., description="Beatmapset ID")],
    download_service: DownloadService,
    fetcher: Fetcher,
    no_video: Annotated[bool, Query(alias="noVideo")] = True,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    # Register this set's beatmaps in the background now, so that by the time the client
    # finishes downloading + importing it and asks for online metadata / the leaderboard,
    # the beatmaps are already in our DB (fast, correct status) rather than triggering a
    # slow mirror fetch that lands too late (the "not available until you switch maps and
    # back" bug). Helps the official client + injector too.
    ensure_beatmaps_registered_in_background(beatmapset_id, fetcher)

    # ── Layer 0: locally-hosted maps (admin-uploaded) — direct redirect.
    async with with_db() as db:
        local_beatmapset = await db.get(Beatmapset, beatmapset_id)
        is_local = bool(local_beatmapset and local_beatmapset.is_local)

    if is_local:
        local_file_path = f"beatmapsets/{beatmapset_id}.osz"
        if await storage.is_exists(local_file_path):
            local_url = await storage.get_file_url(local_file_path)
            logger.info(f"[beatmap dl] local redirect {beatmapset_id} -> {local_url}")
            return RedirectResponse(url=local_url, status_code=307)

    # ── Layer 1: opportunistic cache from a previous proxy success.
    cache_path = f"cache/beatmapsets/{beatmapset_id}.osz"
    if await storage.is_exists(cache_path):
        if await _cached_osz_looks_valid(storage, cache_path):
            cached_url = await storage.get_file_url(cache_path)
            logger.info(f"[beatmap dl] cache hit {beatmapset_id} -> {cached_url}")
            return RedirectResponse(url=cached_url, status_code=307)
        # cache envenenado (JSON/HTML de error o zip truncado, herencia del bug
        # de validacion): lo borramos y seguimos a la cadena de mirrors como si
        # nunca hubiera existido. Self-heal automatico, sin intervencion manual.
        logger.warning(
            f"[beatmap dl] cached osz for {beatmapset_id} is invalid (poisoned); purging and refetching"
        )
        try:
            await storage.delete_file(cache_path)
        except Exception as e:
            logger.warning(f"[beatmap dl] failed to purge poisoned cache {cache_path}: {e}")

    # ── Negative cache: avoid spamming mirrors when we already proved this
    # map is unavailable everywhere. The lazer client retries failed
    # downloads automatically; without this short-circuit each retry would
    # walk the entire mirror chain again. 5-minute TTL gives mirrors time
    # to refresh their indexes if it's a transient gap.
    neg_cache_key = f"dl_failed:{beatmapset_id}"
    try:
        neg = await redis.get(neg_cache_key)
    except Exception as e:
        logger.warning(f"[beatmap dl] negative-cache read failed: {e}")
        neg = None
    if neg:
        logger.info(f"[beatmap dl] negative-cache hit {beatmapset_id} -> 503 immediately")
        raise HTTPException(
            status_code=503,
            detail=(
                "This beatmap is not available on any of our mirrors right now. "
                "We'll re-check in 60 seconds — try again then. "
                f"(reason cached: {neg.decode() if isinstance(neg, bytes) else neg})"
            ),
        )

    # ── Layer 2: mirror chain. Decide CN vs international by GeoIP, with a
    # fallback to the authenticated user's country_code only when the IP
    # itself is public (private/loopback IPs would mis-classify Docker/dev).
    geoip_helper = get_geoip_helper()
    geo_info = geoip_helper.lookup(client_ip)
    country_code = geo_info.get("country_iso", "")
    try:
        ip_obj = ipaddress.ip_address(str(client_ip))
        is_private_ip = ip_obj.is_private or ip_obj.is_loopback
    except ValueError:
        is_private_ip = False

    if country_code:
        is_china = country_code == "CN"
    elif not is_private_ip and current_user and current_user.country_code:
        is_china = current_user.country_code == "CN"
    else:
        is_china = False

    download_urls = download_service.get_download_urls(
        beatmapset_id=beatmapset_id, no_video=no_video, is_china=is_china
    )

    # Map mirror name → (url, headers) so we can compose race/serial groups
    # in any order without losing track of which is which.
    by_name: dict[str, tuple[str, dict[str, str] | None, str]] = {}
    for url in download_urls:
        if "nerinyan" in url:
            by_name["nerinyan"] = (url, None, "Nerinyan")
        elif "osu.direct" in url:
            by_name["osu.direct"] = (url, None, "osu.direct")
        elif "akatsuki" in url:
            by_name["akatsuki"] = (url, None, "Akatsuki")
        elif "gatari" in url:
            by_name["gatari"] = (url, None, "Gatari")
        elif "sayobot" in url:
            by_name["sayobot"] = (url, None, "Sayobot")
        else:
            by_name[url] = (url, None, url)

    if settings.beatconnect_api_token:
        # BeatConnect's no-rate-limit programmatic path is the ?token= QUERY
        # param, not a Token header (confirmed by the operator). The header
        # variant still trips the per-IP ~10rpm 429 even with a paid token,
        # which is why this used to be relegated to a slow last-resort fallback.
        # The query token lifts that. The token only travels on our
        # server-to-mirror request (we proxy + stream the body), never to the
        # client, so it stays secret.
        by_name["beatconnect"] = (
            f"{str(settings.beatconnect_base_url).rstrip('/')}/b/{beatmapset_id}/?token={settings.beatconnect_api_token}",
            None,
            "BeatConnect",
        )

    if not by_name:
        raise HTTPException(status_code=503, detail="No download URLs available")

    # Race tier — mirrors fired in parallel. The first to return a valid 200
    # wins, losers cancelled mid-flight. Each fills a different niche so the
    # union catches every map any of them has:
    #   - BeatConnect: paid mirror (Patreon token), complete catalog, and on the
    #     ?token= query path it's unrate-limited — our most reliable source for
    #     maps the free mirrors 404 on. This is what fixes "some maps won't
    #     download".
    #   - Nerinyan: fast 302 (~30ms), broad newer catalog, occasional CDN 404s.
    #   - osu.direct: ~600ms-2s, very stable, broad coverage.
    #   - Akatsuki: ~3-6s, complete-ish catalog for newer maps.
    #   - Gatari: ~1-2s for older maps the other often don't have
    #     (e.g. 2015-era beatmapsets that got purged from newer mirrors).
    #
    # The data only flows from the winner, so the extra concurrent connections
    # cost little, and the complementary coverage means we almost never fall
    # through to the serial fallback. BeatConnect used to sit in that fallback
    # because the Token-header path kept hitting a per-IP 429; the ?token= query
    # path (built above) lifts that, so it now races like the rest.
    race_group: list[tuple[str, dict[str, str] | None, str]] = [
        by_name[k] for k in ("beatconnect", "nerinyan", "osu.direct", "akatsuki", "gatari") if k in by_name
    ]
    serial_fallback: list[tuple[str, dict[str, str] | None, str]] = [
        by_name[k] for k in ("sayobot",) if k in by_name
    ]
    # Anything we don't recognise (custom config) goes to serial fallback at
    # the end so we still try it before giving up.
    for key, val in by_name.items():
        if key in {"nerinyan", "osu.direct", "akatsuki", "gatari", "beatconnect", "sayobot"}:
            continue
        serial_fallback.append(val)

    from starlette.responses import StreamingResponse

    chosen: httpx.Response | None = None
    chosen_label: str | None = None

    if race_group:
        logger.info(
            f"[beatmap dl] racing {len(race_group)} mirrors for {beatmapset_id}: "
            f"{', '.join(label for _, _, label in race_group)}"
        )
        chosen, chosen_label = await _race_mirror_pair(race_group)

    if chosen is None:
        for url, headers, label in serial_fallback:
            logger.info(f"[beatmap dl] fallback try {label} for {beatmapset_id}")
            resp = await _attempt_mirror(url, headers, label=label)
            if resp is not None:
                chosen = resp
                chosen_label = label
                break

    async def _mark_unavailable(reason: str) -> None:
        """Set the negative cache key so retry storms don't re-walk the whole
        mirror chain for the same dead beatmap. 60s is short enough that a
        legitimately-existing map that's just temporarily missing from every
        mirror gets re-checked quickly, while still spamming-protecting us
        against lazer client retry loops."""
        try:
            await redis.setex(neg_cache_key, 60, reason)
        except Exception as e:
            logger.warning(f"[beatmap dl] negative-cache write failed: {e}")

    if chosen is None:
        await _mark_unavailable("all-mirrors-4xx")
        raise HTTPException(
            status_code=503,
            detail=(
                "All download mirrors failed. The map either doesn't exist on "
                "Nerinyan, osu.direct, Gatari, or BeatConnect, or every mirror "
                "is currently rate-limiting/down. Cached this status for 5 minutes."
            ),
        )

    is_valid, _first_chunk, stream_iter = await _validate_osz_first_chunk(chosen)
    if not is_valid:
        logger.warning(
            f"[beatmap dl] {chosen_label} returned non-osz body "
            f"(ct={chosen.headers.get('Content-Type')}); falling back to other mirrors"
        )
        await _close_response(chosen)
        failed_label = chosen_label
        chosen = None
        chosen_label = None
        # Retry every OTHER mirror — race losers we cancelled mid-flight + the
        # whole serial fallback tier — until one returns a real osz.
        retry_chain = [c for c in (race_group + serial_fallback) if c[2] != failed_label]
        for url, headers, label in retry_chain:
            resp = await _attempt_mirror(url, headers, label=label)
            if resp is None:
                continue
            is_valid, _first_chunk, stream_iter = await _validate_osz_first_chunk(resp)
            if is_valid:
                chosen = resp
                chosen_label = label
                break
            await _close_response(resp)
        if chosen is None:
            await _mark_unavailable("all-mirrors-non-osz")
            raise HTTPException(
                status_code=503,
                detail="All mirrors returned invalid (non-osz) responses. Cached this status for 5 minutes.",
            )

    content_length = chosen.headers.get("Content-Length")
    resp_headers = {
        "Content-Type": "application/x-osu-beatmap-archive",
        "Content-Disposition": f'attachment; filename="{beatmapset_id}.osz"',
    }
    if content_length:
        resp_headers["Content-Length"] = content_length

    owned_client = chosen.extensions.get("_torii_owned_client") if hasattr(chosen, "extensions") else None  # type: ignore[union-attr]
    chosen_resp = chosen  # bind for closure

    async def stream_body():
        buffer: list[bytes] = []
        total = 0
        too_big = False
        completed = False
        try:
            async for chunk in stream_iter:
                if not too_big:
                    total += len(chunk)
                    if total > _MAX_CACHE_BYTES:
                        too_big = True
                        buffer.clear()
                    else:
                        buffer.append(chunk)
                yield chunk
            # solo llegamos aca si el stream del mirror TERMINO entero; si el
            # mirror corto a mitad o el cliente cancelo (GeneratorExit), esto
            # nunca se setea y el finally NO cachea el buffer parcial. Antes
            # cacheabamos lo que hubiera -> .osz truncado servido para siempre.
            completed = True
        finally:
            try:
                await chosen_resp.aclose()
            except Exception:
                pass
            if owned_client is not None:
                try:
                    await owned_client.aclose()
                except Exception:
                    pass
            if completed and not too_big and buffer:
                payload = b"".join(buffer)
                buffer.clear()

                if not _payload_is_complete_osz(payload, content_length):
                    logger.warning(
                        f"[beatmap dl] NOT caching {beatmapset_id} from {chosen_label}: "
                        f"payload failed osz integrity check ({total} bytes, "
                        f"content-length={content_length})"
                    )
                else:
                    async def _flush_cache():
                        try:
                            await storage.write_file(
                                cache_path,
                                payload,
                                content_type="application/x-osu-beatmap-archive",
                            )
                            logger.info(
                                f"[beatmap dl] cached {beatmapset_id} ({total} bytes) from {chosen_label}"
                            )
                        except Exception as exc:
                            logger.warning(
                                f"[beatmap dl] cache write failed for {beatmapset_id}: {exc}"
                            )

                    asyncio.create_task(_flush_cache())

    logger.info(f"[beatmap dl] streaming {beatmapset_id} from {chosen_label}")
    return StreamingResponse(stream_body(), status_code=200, headers=resp_headers)


@router.post(
    "/beatmapsets/{beatmapset_id}/favourites",
    tags=["Beatmapsets"],
    name="Favourite or Unfavourite Beatmapset",
    description="\nFavourite or unfavourite specified beatmapset.",
)
async def favourite_beatmapset(
    db: Database,
    cache_service: UserCacheService,
    beatmapset_id: Annotated[int, Path(..., description="Beatmapset ID")],
    action: Annotated[
        Literal["favourite", "unfavourite"],
        Form(description="Action type: 'favourite' to favourite / 'unfavourite' to unfavourite"),
    ],
    current_user: ClientUser,
):
    existing_favourite = (
        await db.exec(
            select(FavouriteBeatmapset).where(
                FavouriteBeatmapset.user_id == current_user.id,
                FavouriteBeatmapset.beatmapset_id == beatmapset_id,
            )
        )
    ).first()

    if (action == "favourite" and existing_favourite) or (action == "unfavourite" and not existing_favourite):
        return

    if action == "favourite":
        favourite = FavouriteBeatmapset(user_id=current_user.id, beatmapset_id=beatmapset_id)
        db.add(favourite)
    else:
        await db.delete(existing_favourite)
    await cache_service.invalidate_user_beatmapsets_cache(current_user.id)
    await db.commit()

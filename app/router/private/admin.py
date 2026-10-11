from __future__ import annotations
from datetime import datetime, timedelta
from typing import Annotated, cast, Any

from app.auth import invalidate_user_tokens, validate_username
from app.config import settings
from app.database.auth import OAuthToken
from app.database.beatmap import Beatmap, BannedBeatmaps
from app.database.beatmapset import Beatmapset
from app.database.chat import ChannelType, ChatChannel, ChatMessage, ChatMessageModel, MessageType
from app.database.score import Score
from app.database.score_token import ScoreToken
from app.database.statistics import UserStatistics
from app.database.user_login_log import UserLoginLog
from app.database.daily_challenge_model import DailyChallenge, DailyChallengeCreate, DailyChallengeUpdate, DailyChallengeResponse
from app.database.donation import apply_supporter_grant
from app.database.team import Team, TeamMember
from app.database.user import User, UserProfileCover
from app.database.user_account_history import UserAccountHistory, UserAccountHistoryType
from app.database.user_badge import UserBadge, UserBadgeCreate, UserBadgeUpdate, UserBadgeResponse
from app.database.verification import LoginSession, LoginSessionResp, TrustedDevice, TrustedDeviceResp
from app.database.events import Event, EventType
from app.database.username_change_request import (
    STATUS_APPROVED as UCR_APPROVED,
    STATUS_PENDING as UCR_PENDING,
    STATUS_REJECTED as UCR_REJECTED,
    UsernameChangeRequest,
)
from app.database.profile_media_review import (
    MEDIA_AVATAR,
    MEDIA_COVER,
    STATUS_REVOKED as PMR_REVOKED,
    ProfileMediaReview,
)
from app.const import BANCHOBOT_ID
from app.dependencies.cache import UserCacheService
from app.dependencies.database import Database, Redis, get_redis
from app.dependencies.client_verification import ClientVerificationService
from app.dependencies.geoip import GeoIPService
from app.dependencies.storage import StorageService
from app.dependencies.user import UserAndToken, get_client_user_and_token
from app.log import log
from app.models.mods import API_MODS, APIMod, get_available_mods
from app.models.score import GameMode
from app.models.notification import ChannelMessage, GlobalAnnouncement
from app.models.torii_groups import is_currently_supporting
from app.router.notification.server import server
from app.service.ranking_cache_service import get_ranking_cache_service
from app.tasks.daily_challenge import create_daily_challenge_room
from app.utils import check_image, utcnow

from .router import router

import json
import httpx
import hashlib
from fastapi import File, Form, HTTPException, Query, Security
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_ as sql_or
from sqlmodel import col, exists, func, select

logger = log("AdminRouter")

def _parse_mods_raw(
    raw: str | None,
    *,
    ruleset_id: int | None = None,
    field_name: str = "mods",
    strict: bool = True,
) -> list[APIMod]:
    """Parse mods from a JSON string into a list of APIMod dicts.

    The admin frontend stores mods as a JSON array of acronym strings
    (legacy: ``["HD","NF"]``) OR APIMod dicts with optional settings
    (current: ``[{"acronym":"HD"},{"acronym":"DT","settings":{"speed_change":1.6}}]``).
    The rest of the server (cron jobs, room creation) expects APIMod dicts.

    Behaviour:
      * Top-level JSON parse errors raise 400 in ``strict`` mode (default
        for create/update endpoints), or fall through to ``[]`` in
        ``strict=False`` (used when reading rows back from the DB where
        we want to keep displaying broken data instead of erroring out).
      * Each entry must have a string ``acronym``. Malformed entries
        raise 400 in strict mode rather than being silently dropped —
        admins were ending up with quietly-different challenges than
        what they configured.
      * When ``ruleset_id`` is provided the acronym is checked against
        ``API_MODS`` for that ruleset. Unknown mods raise 400.

    Note: settings *values* are not range-checked here. The osu! game
    client validates them at play time — we just guard the shape so
    the row never has acronyms the runtime can't resolve.
    """
    try:
        parsed = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError) as exc:
        if strict:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in {field_name}: {exc}",
            ) from exc
        return []
    if not isinstance(parsed, list):
        if strict:
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} must be a JSON array, got {type(parsed).__name__}",
            )
        return []

    catalog: dict[str, Any] | None = None
    if ruleset_id is not None:
        # API_MODS is dict[ruleset_id, dict[acronym, Mod]]
        catalog = API_MODS.get(ruleset_id)  # pyright: ignore[reportArgumentType]
        if catalog is None and strict:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown ruleset_id {ruleset_id}",
            )

    result: list[APIMod] = []
    for idx, item in enumerate(parsed):
        if isinstance(item, str):
            entry = cast(APIMod, {"acronym": item})
        elif isinstance(item, dict) and isinstance(item.get("acronym"), str):
            entry = cast(APIMod, {k: v for k, v in item.items()})
        else:
            if strict:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field_name}[{idx}] must be a string acronym or {{acronym, settings?}} object",
                )
            continue

        if catalog is not None and entry["acronym"] not in catalog:
            if strict:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown mod acronym '{entry['acronym']}' for ruleset {ruleset_id}",
                )
            continue

        result.append(entry)
    return result


_RULESET_TO_GAMEMODE: dict[int, GameMode] = {
    0: GameMode.OSU,
    1: GameMode.TAIKO,
    2: GameMode.FRUITS,
    3: GameMode.MANIA,
}


async def _resync_and_retry_beatmap(session: AsyncSession, beatmap_id: int) -> Beatmap | None:
    """Refresca el set al que pertenece esta diff y vuelve a buscarla.

    Devuelve el Beatmap si aparecio, o None si de verdad no existe. No propaga errores: si el
    mirror esta caido o el id no existe, el llamador sigue con su 404 de siempre.
    """
    from app.dependencies.fetcher import get_fetcher

    try:
        fetcher = await get_fetcher()
        remoto = await fetcher.get_beatmap(beatmap_id=beatmap_id)
        beatmapset_id = remoto.get("beatmapset_id")

        if not beatmapset_id:
            return None

        logger.info(
            f"beatmap {beatmap_id} no estaba en la base; trayendo el set {beatmapset_id} y reintentando"
        )
        # get_or_fetch crea PRIMERO la fila del beatmapset (con sus diffs) y recien despues
        # registra el sync. Antes esto llamaba a add_missing_beatmapset(immediate=True), que
        # va al reves: commitea el BeatmapSync, que tiene FK contra beatmapsets.id, para un set
        # que todavia no existe. Con un set ya conocido no se notaba nunca; con uno nuevo
        # reventaba con IntegrityError 1452 y el admin se comia un 404 que mentia, porque el
        # mapa existe perfectamente en ppy.
        await Beatmapset.get_or_fetch(session, fetcher, beatmapset_id)
    except Exception as e:
        logger.warning(f"no se pudo traer el set de la beatmap {beatmap_id}: {e}")
        return None

    # get_or_fetch commitea en esta misma sesion; expire_all la obliga a releer lo recien escrito.
    session.expire_all()
    return await session.get(Beatmap, beatmap_id)


async def _validate_daily_challenge_inputs(
    session: Database,
    *,
    beatmap_id: int,
    ruleset_id: int,
    required_mods_raw: str | None,
    allowed_mods_raw: str | None,
) -> tuple[Beatmap, list[APIMod], list[APIMod]]:
    """Cross-validate the (beatmap, ruleset, mods) triple before persisting a challenge.

    Returns the resolved beatmap and the parsed APIMod lists. Raises 400/404
    HTTPExceptions on any mismatch — caller can store the parsed lists and
    trust them.

    Catches the audit's C2 (ruleset/beatmap-mode mismatch) and C3 (silent mod
    drops) at one chokepoint so create / update / random-pick-create all share
    the same guarantees.
    """
    if ruleset_id not in _RULESET_TO_GAMEMODE:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ruleset_id {ruleset_id} — must be one of 0 (osu!), 1 (taiko), 2 (catch), 3 (mania)",
        )

    beatmap = await session.get(Beatmap, beatmap_id)
    if beatmap is None:
        # torii: antes de darse por vencido, preguntar.
        #
        # Un mapa nuevo al que el mapper le sigue agregando dificultades puede tener el set
        # cacheado pero la diff no. Paso con "osu! MEGAMIX 2" (set 2593652): lo cacheamos con 6
        # diffs, el mapper le agrego 3 mas, y elegir una de esas tres daba 404 sin ninguna
        # explicacion para el que estaba armando el challenge.
        #
        # Y no se arreglaba solo: el sync marca el set como al dia ANTES de materializar las filas
        # (ver beatmapset_update_service), asi que si esa parte falla el JSON queda adelantado, la
        # comparacion siguiente es contra ese mismo JSON, y el set queda enterrado con backoff
        # creciente. Al momento de escribir esto habia 807 sets asi, con 1504 diffs perdidas.
        #
        # Un refresh puntual del set, aca donde de verdad hace falta, resuelve el caso concreto y
        # ademas repara el set de paso. Es UN pedido al mirror y solo cuando ya ibamos a fallar.
        beatmap = await _resync_and_retry_beatmap(session, beatmap_id)

    if beatmap is None:
        raise HTTPException(status_code=404, detail=f"Beatmap {beatmap_id} not found")

    expected_mode = _RULESET_TO_GAMEMODE[ruleset_id]
    if beatmap.mode != expected_mode:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Ruleset/beatmap mode mismatch: ruleset_id={ruleset_id} expects {expected_mode.name} "
                f"but beatmap {beatmap_id} is {beatmap.mode.name if beatmap.mode else 'unknown'}. "
                "Either pick a matching beatmap or change the game mode."
            ),
        )

    required_mods = _parse_mods_raw(required_mods_raw, ruleset_id=ruleset_id, field_name="required_mods")
    allowed_mods = _parse_mods_raw(allowed_mods_raw, ruleset_id=ruleset_id, field_name="allowed_mods")

    # Un permitido incompatible con un requerido deja elegir algo que despues no
    # se puede submitear (paso con MG + RX).
    choca_con = set()
    for mod in required_mods:
        info = API_MODS.get(ruleset_id, {}).get(mod["acronym"])
        if info:
            choca_con.update(info["IncompatibleMods"])

    conflictivos = sorted({m["acronym"] for m in allowed_mods if m["acronym"] in choca_con})
    if conflictivos:
        requeridos = ", ".join(m["acronym"] for m in required_mods)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Estos mods permitidos son incompatibles con los requeridos ({requeridos}): "
                f"{', '.join(conflictivos)}. El cliente los dejaria elegir y despues el score no "
                f"se podria enviar, asi que sacalos de allowed_mods."
            ),
        )

    return beatmap, required_mods, allowed_mods


async def _evict_user_from_live_services(session: Database, redis: Redis, user: User) -> None:
    user_id = user.id
    if user_id is None:
        return

    revoked_tokens = await invalidate_user_tokens(session, user_id)
    logger.info(f"Revoked {revoked_tokens} tokens for restricted user {user_id}")

    sockets = list(server.connect_client.get(user_id, set()))
    for websocket in sockets:
        try:
            await websocket.close(code=4003, reason="Account restricted")
        except Exception:
            pass

    try:
        await server.disconnect(user, session)
    except Exception as e:
        logger.debug(f"Failed to disconnect restricted user {user_id} from notification server: {e}")

    try:
        await redis.delete(f"metadata:online:{user_id}")
        await redis.srem("metadata:online_users_set", user_id)
        await redis.publish("osu-channel:user:invalidate", json.dumps({"user_id": user_id}))
    except Exception as e:
        logger.debug(f"Failed to clear online presence for restricted user {user_id}: {e}")


async def require_admin(session: Database, user_and_token: UserAndToken) -> User:
    """Helper function to check if user is admin"""
    current_user, _ = user_and_token
    # is_admin is an OnDemand field, so we need to await it
    is_admin = await current_user.awaitable_attrs.is_admin
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


async def user_to_dict(user: User, session: Database) -> dict:
    """Convert User object to dictionary for API response"""
    # Get basic model dump
    user_dict = user.model_dump(exclude_none=True)

    # Await OnDemand fields that might be needed
    try:
        user_dict["is_admin"] = await user.awaitable_attrs.is_admin
    except Exception:
        user_dict["is_admin"] = False

    try:
        user_dict["is_gmt"] = await user.awaitable_attrs.is_gmt
    except Exception:
        user_dict["is_gmt"] = False

    try:
        user_dict["is_qat"] = await user.awaitable_attrs.is_qat
    except Exception:
        user_dict["is_qat"] = False

    try:
        user_dict["is_restricted"] = await user.is_restricted(session)
    except Exception:
        user_dict["is_restricted"] = False

    user_dict["torii_titles"] = user.torii_titles or []

    # Handle badges - serialize datetime to ISO string
    try:
        # 1. Get badges from JSON field (legacy)
        legacy_badges = []
        json_badges = await user.awaitable_attrs.badges
        if json_badges:
            for badge in json_badges:
                badge_copy = dict(badge)
                if "awarded_at" in badge_copy and isinstance(badge_copy["awarded_at"], datetime):
                    badge_copy["awarded_at"] = badge_copy["awarded_at"].isoformat()
                legacy_badges.append(badge_copy)

        # 2. Get badges from user_badges table (new)
        db_badges = []
        user_badges_list = (
            await session.exec(
                select(UserBadge).where(UserBadge.user_id == user.id).order_by(col(UserBadge.awarded_at).desc())
            )
        ).all()
        if user_badges_list:
            for badge in user_badges_list:
                db_badges.append({
                    "id": badge.id,
                    "description": badge.description,
                    "image_url": badge.image_url,
                    "image@2x_url": badge.image_2x_url,
                    "url": badge.url,
                    "awarded_at": badge.awarded_at.isoformat() if isinstance(badge.awarded_at, datetime) else badge.awarded_at,
                    "user_id": badge.user_id
                })

        # Combine both, preferring DB badges.
        user_dict["badges"] = db_badges + legacy_badges
    except Exception:
        user_dict["badges"] = []

    return user_dict


class SessionsResp(BaseModel):
    total: int
    current: int = 0
    sessions: list[LoginSessionResp]


class AdminStatsResp(BaseModel):
    total_users: int
    online_users: int
    total_pp: float
    total_plays: int
    total_scores: int
    total_beatmaps: int
    blacklisted_beatmaps: int
    performance_server_status: str
    api_server_status: str


class AdminLoginLogItemResp(BaseModel):
    id: int
    user_id: int
    username: str | None = None
    ip_address: str
    user_agent: str | None = None
    login_time: datetime
    login_success: bool
    login_method: str
    client_label: str | None = None
    client_hash: str | None = None
    notes: str | None = None
    country_code: str | None = None
    country_name: str | None = None
    city_name: str | None = None
    organization: str | None = None


class AdminLoginLogListResp(BaseModel):
    total: int
    page: int
    per_page: int
    logs: list[AdminLoginLogItemResp]


class UnknownClientHashResp(BaseModel):
    hash: str
    count: int
    first_seen: str | None = None
    last_seen: str | None = None
    last_user_id: int | None = None
    last_user_agent: str | None = None
    last_detected_os: str | None = None
    last_source: str | None = None


class UnknownClientHashListResp(BaseModel):
    total: int
    page: int
    per_page: int
    hashes: list[UnknownClientHashResp]


class AssignClientHashReq(BaseModel):
    client_hash: str
    client_name: str
    version: str = ""
    os: str = ""
    remove_from_unknown: bool = True


async def _count_online_users(redis) -> int:
    """Count online users with set-first strategy and SCAN fallback."""
    try:
        online_set_key = "metadata:online_users_set"
        if await redis.exists(online_set_key):
            return int(await redis.scard(online_set_key))
    except Exception:
        pass

    try:
        cursor = 0
        online_count = 0
        max_iterations = 500
        iterations = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="metadata:online:*", count=1000)
            online_count += len(keys)
            iterations += 1
            if cursor == 0 or iterations >= max_iterations:
                break
        return online_count
    except Exception:
        return 0


class UserUpdateRequest(BaseModel):
    username: str | None = None
    country_code: str | None = None
    is_qat: bool | None = None
    is_gmt: bool | None = None
    is_admin: bool | None = None
    # Accept legacy payloads from older frontend builds (dict/str/list)
    badge: dict | str | list[dict] | None = None
    # List of TORII_GROUPS keys to assign as custom titles (replaces the full list)
    torii_titles: list[str] | None = None


class BeatmapBlacklistItem(BaseModel):
    id: int
    beatmapset_id: int
    beatmap_id: int
    source: str = "manual"
    reason: str | None = None
    beatmapset: dict | None = None
    # New in the single-map redesign: each row carries the difficulty's
    # own metadata so the admin UI can show "[Insane] · 5.4★ · osu! · 2:34"
    # per row instead of just a beatmapset title. Optional because some
    # historic blacklist rows may reference a beatmap_id whose Beatmap
    # row is no longer present locally.
    beatmap: dict | None = None


class BadgeCreateRequest(BaseModel):
    description: str
    image_url: str
    image_2x_url: str | None = None
    url: str | None = None
    awarded_at: str | None = None  # ISO format string


class BadgeUpdateRequest(BaseModel):
    description: str | None = None
    image_url: str | None = None
    image_2x_url: str | None = None
    url: str | None = None
    awarded_at: str | None = None  # ISO format string


class GlobalAnnouncementReq(BaseModel):
    title: str = "Server Announcement"
    message: str
    severity: str = "warning"
    also_send_pm: bool = True
    online_only: bool = True
    sender_username: str | None = None
    sender_user_id: int | None = None
    # Pop the announcement on top of every recipient's lazer client by
    # piggy-backing on the medal-unlock overlay. The regular global
    # announcement notification ends up in the notifications drawer
    # but doesn't visually interrupt the player; the medal popup does.
    # Implementation publishes a synthetic UserAchievementUnlock on
    # the chat:notification Redis channel per recipient.
    show_popup: bool = True


class GlobalAnnouncementResp(BaseModel):
    sent_to: int
    severity: str
    title: str
    online_only: bool
    sender_username: str


@router.get(
    "/admin/sessions",
    name="Get Current User Login Sessions",
    tags=["User Sessions", "g0v0 API", "Admin"],
    response_model=SessionsResp,
)
async def get_sessions(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    geoip: GeoIPService,
):
    current_user, token = user_and_token
    current = 0

    sessions = (
        await session.exec(
            select(
                LoginSession,
            )
            .where(LoginSession.user_id == current_user.id, col(LoginSession.is_verified).is_(True))
            .order_by(col(LoginSession.created_at).desc())
        )
    ).all()
    resp = []
    for s in sessions:
        resp.append(LoginSessionResp.from_db(s, geoip))
        if s.token_id == token.id:
            current = s.id

    return SessionsResp(
        total=len(sessions),
        current=current,
        sessions=resp,
    )


@router.delete(
    "/admin/sessions/{session_id}",
    name="Revoke Login Session",
    tags=["User Sessions", "g0v0 API", "Admin"],
    status_code=204,
)
async def delete_session(
    session: Database,
    session_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    current_user, token = user_and_token
    if session_id == token.id:
        raise HTTPException(status_code=400, detail="Cannot delete the current session")

    db_session = await session.get(LoginSession, session_id)
    if not db_session or db_session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")

    await session.delete(db_session)

    token = await session.get(OAuthToken, db_session.token_id or 0)
    if token:
        await session.delete(token)

    await session.commit()
    return


class TrustedDevicesResp(BaseModel):
    total: int
    current: int = 0
    devices: list[TrustedDeviceResp]


@router.get(
    "/admin/trusted-devices",
    name="Get Current User Trusted Devices",
    tags=["User Sessions", "g0v0 API", "Admin"],
    response_model=TrustedDevicesResp,
)
async def get_trusted_devices(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    geoip: GeoIPService,
):
    current_user, token = user_and_token
    devices = (
        await session.exec(
            select(TrustedDevice)
            .where(TrustedDevice.user_id == current_user.id)
            .order_by(col(TrustedDevice.last_used_at).desc())
        )
    ).all()

    current_device_id = (
        await session.exec(
            select(TrustedDevice.id)
            .join(LoginSession, col(LoginSession.device_id) == TrustedDevice.id)
            .where(
                LoginSession.token_id == token.id,
                TrustedDevice.user_id == current_user.id,
            )
            .limit(1)
        )
    ).first()

    return TrustedDevicesResp(
        total=len(devices),
        current=current_device_id or 0,
        devices=[TrustedDeviceResp.from_db(device, geoip) for device in devices],
    )


@router.delete(
    "/admin/trusted-devices/{device_id}",
    name="Remove Trusted Device",
    tags=["User Sessions", "g0v0 API", "Admin"],
    status_code=204,
)
async def delete_trusted_device(
    session: Database,
    device_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    current_user, token = user_and_token
    device = await session.get(TrustedDevice, device_id)
    current_device_id = (
        await session.exec(
            select(TrustedDevice.id)
            .join(LoginSession, col(LoginSession.device_id) == TrustedDevice.id)
            .where(
                LoginSession.token_id == token.id,
                TrustedDevice.user_id == current_user.id,
            )
            .limit(1)
        )
    ).first()
    if device_id == current_device_id:
        raise HTTPException(status_code=400, detail="Cannot delete the current trusted device")

    if not device or device.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Trusted device not found")

    await session.delete(device)
    await session.commit()
    return


# ========== Admin Statistics ==========

@router.get(
    "/admin/stats",
    name="Get Admin Statistics",
    tags=["Admin", "g0v0 API"],
    response_model=AdminStatsResp,
)
async def get_admin_stats(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get admin statistics: total users, online users, total pp, total plays, total scores, beatmaps, and blacklisted beatmaps"""
    await require_admin(session, user_and_token)

    # Count total users
    total_users = (await session.exec(select(func.count()).select_from(User))).one()

    # Count online users
    redis = get_redis()
    online_users = await _count_online_users(redis)

    # Sum total PP
    total_pp = (await session.exec(select(func.sum(UserStatistics.pp)))).one() or 0.0

    # Sum total plays
    total_plays = (await session.exec(select(func.sum(UserStatistics.play_count)))).one() or 0

    # Count total scores
    total_scores = (await session.exec(select(func.count()).select_from(Score))).one()

    # Count total beatmaps (non-deleted)
    total_beatmaps = (await session.exec(select(func.count()).select_from(Beatmapset))).one()

    # Count blacklisted beatmaps (unique beatmapsets)
    blacklisted_beatmap_ids = (
        await session.exec(select(BannedBeatmaps.beatmap_id))
    ).all()
    # Get unique beatmapsets from banned beatmaps
    if blacklisted_beatmap_ids:
        unique_beatmapsets = (
            await session.exec(
                select(func.distinct(Beatmap.beatmapset_id))
                .where(col(Beatmap.id).in_(blacklisted_beatmap_ids))
            )
        ).all()
        blacklisted_beatmaps = len(unique_beatmapsets)
    else:
        blacklisted_beatmaps = 0

    # Check server status
    performance_server_status = "offline"
    perf_urls = (
        "http://performance-server:8080/health",
        "http://performance-server:8080/",
        "http://localhost:8080/health",
        "http://localhost:8080/",
    )
    try:
        async with httpx.AsyncClient() as client:
            for url in perf_urls:
                try:
                    resp = await client.get(url, timeout=1.5)
                    if resp.status_code < 500:
                        performance_server_status = "online"
                        break
                except Exception:
                    continue
    except Exception:
        pass

    api_server_status = "online"

    return AdminStatsResp(
        total_users=total_users,
        online_users=online_users,
        total_pp=total_pp,
        total_plays=total_plays,
        total_scores=total_scores,
        total_beatmaps=total_beatmaps,
        blacklisted_beatmaps=blacklisted_beatmaps,
        performance_server_status=performance_server_status,
        api_server_status=api_server_status,
    )


@router.get(
    "/admin/login-logs",
    name="Get login history logs",
    tags=["Admin", "g0v0 API"],
    response_model=AdminLoginLogListResp,
)
async def get_admin_login_logs(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: str = Query(""),
    user_id: int | None = Query(None, ge=0),
    login_success: bool | None = Query(None),
    login_method: str | None = Query(None),
):
    await require_admin(session, user_and_token)

    conditions = []
    search_value = search.strip()

    if user_id is not None:
        conditions.append(col(UserLoginLog.user_id) == user_id)

    if login_success is not None:
        conditions.append(col(UserLoginLog.login_success) == login_success)

    if login_method:
        conditions.append(col(UserLoginLog.login_method).ilike(f"%{login_method.strip()}%"))

    if search_value:
        username_ids = (
            await session.exec(
                select(User.id).where(col(User.username).ilike(f"%{search_value}%")).limit(500)
            )
        ).all()

        text_condition = sql_or(
            col(UserLoginLog.ip_address).ilike(f"%{search_value}%"),
            col(UserLoginLog.user_agent).ilike(f"%{search_value}%"),
            col(UserLoginLog.client_label).ilike(f"%{search_value}%"),
            col(UserLoginLog.client_hash).ilike(f"%{search_value}%"),
            col(UserLoginLog.notes).ilike(f"%{search_value}%"),
            col(UserLoginLog.country_name).ilike(f"%{search_value}%"),
            col(UserLoginLog.city_name).ilike(f"%{search_value}%"),
            col(UserLoginLog.organization).ilike(f"%{search_value}%"),
            col(UserLoginLog.login_method).ilike(f"%{search_value}%"),
        )

        if search_value.isdigit():
            text_condition = sql_or(text_condition, col(UserLoginLog.user_id) == int(search_value))

        if username_ids:
            text_condition = sql_or(text_condition, col(UserLoginLog.user_id).in_(username_ids))

        conditions.append(text_condition)

    count_stmt = select(func.count()).select_from(UserLoginLog)
    data_stmt = select(UserLoginLog)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        data_stmt = data_stmt.where(*conditions)

    total = (await session.exec(count_stmt)).one()
    rows = (
        await session.exec(
            data_stmt.order_by(col(UserLoginLog.login_time).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    user_ids = sorted({row.user_id for row in rows if row.user_id > 0})
    username_map: dict[int, str] = {}
    if user_ids:
        users = (
            await session.exec(
                select(User.id, User.username).where(col(User.id).in_(user_ids))
            )
        ).all()
        username_map = {uid: uname for uid, uname in users}

    logs = [
        AdminLoginLogItemResp(
            id=row.id or 0,
            user_id=row.user_id,
            username=username_map.get(row.user_id),
            ip_address=row.ip_address,
            user_agent=row.user_agent,
            login_time=row.login_time,
            login_success=row.login_success,
            login_method=row.login_method,
            client_label=row.client_label,
            client_hash=row.client_hash,
            notes=row.notes,
            country_code=row.country_code,
            country_name=row.country_name,
            city_name=row.city_name,
            organization=row.organization,
        )
        for row in rows
    ]

    return AdminLoginLogListResp(total=total, page=page, per_page=per_page, logs=logs)


@router.get(
    "/admin/client-hashes/unknown",
    name="Get unknown client hashes",
    tags=["Admin", "g0v0 API"],
    response_model=UnknownClientHashListResp,
)
async def get_unknown_client_hashes(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    verification_service: ClientVerificationService,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: str = Query(""),
):
    await require_admin(session, user_and_token)

    unknown = await verification_service.get_unknown_hashes()
    items: list[UnknownClientHashResp] = []
    search_value = search.strip().lower()

    for hash_value, data in unknown.items():
        entry = UnknownClientHashResp(
            hash=hash_value,
            count=int(data.get("count", 0) or 0),
            first_seen=str(data.get("first_seen")) if data.get("first_seen") else None,
            last_seen=str(data.get("last_seen")) if data.get("last_seen") else None,
            last_user_id=int(data["last_user_id"]) if data.get("last_user_id") is not None else None,
            last_user_agent=str(data.get("last_user_agent")) if data.get("last_user_agent") else None,
            last_detected_os=str(data.get("last_detected_os")) if data.get("last_detected_os") else None,
            last_source=str(data.get("last_source")) if data.get("last_source") else None,
        )
        if search_value:
            search_blob = " ".join(
                [
                    entry.hash,
                    entry.last_user_agent or "",
                    entry.last_source or "",
                    str(entry.last_user_id or ""),
                ]
            ).lower()
            if search_value not in search_blob:
                continue
        items.append(entry)

    items.sort(key=lambda x: (x.last_seen or "", x.count), reverse=True)
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    return UnknownClientHashListResp(total=total, page=page, per_page=per_page, hashes=items[start:end])


@router.post(
    "/admin/client-hashes/assign",
    name="Assign unknown client hash",
    tags=["Admin", "g0v0 API"],
)
async def assign_unknown_client_hash(
    req: AssignClientHashReq,
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    verification_service: ClientVerificationService,
):
    await require_admin(session, user_and_token)
    input_hash = req.client_hash.strip().lower()
    normalized_hash, ambiguous = await verification_service.resolve_hash_input(input_hash)
    if ambiguous:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Hash prefix is ambiguous; provide a longer hash.",
                "input_hash": input_hash,
                "candidates": ambiguous[:10],
            },
        )

    try:
        await verification_service.assign_hash_override(
            normalized_hash,
            client_name=req.client_name,
            version=req.version,
            os_name=req.os,
            remove_from_unknown=req.remove_from_unknown,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    resolved = await verification_service.validate_client_version(normalized_hash)
    resolved_name = (resolved.client_name or "").strip()
    resolved_version = (resolved.version or "").strip()
    resolved_os = (resolved.os or "").strip()
    resolved_label = " ".join(part for part in (resolved_name, resolved_version) if part).strip()
    if resolved_os:
        resolved_label = f"{resolved_label} ({resolved_os})" if resolved_label else resolved_os

    updated_login_logs = 0
    if resolved_label:
        login_rows = (
            await session.exec(
                select(UserLoginLog).where(
                    col(UserLoginLog.client_hash) == normalized_hash,
                )
            )
        ).all()
        for row in login_rows:
            if row.client_label != resolved_label:
                row.client_label = resolved_label
                session.add(row)
                updated_login_logs += 1

    updated_score_tokens = 0
    hash_prefix20 = normalized_hash[:20]
    hash_prefix12 = normalized_hash[:12]
    score_rows = (
        await session.exec(
            select(ScoreToken).where(
                sql_or(
                    col(ScoreToken.client_version).like(f"hash:{hash_prefix20}%"),
                    col(ScoreToken.client_version).like(f"%(hash:{hash_prefix12}%)%"),
                )
            )
        )
    ).all()
    for token_row in score_rows:
        current = (token_row.client_version or "").strip()
        if (
            current.startswith(f"hash:{hash_prefix20}")
            or f"(hash:{hash_prefix12})" in current
        ):
            token_row.client_version = resolved_label or current
            session.add(token_row)
            updated_score_tokens += 1

    if updated_login_logs or updated_score_tokens:
        await session.commit()

    return {
        "ok": True,
        "input_hash": input_hash,
        "hash": normalized_hash,
        "resolved_os": resolved_os or None,
        "updated_login_logs": updated_login_logs,
        "updated_score_tokens": updated_score_tokens,
    }


@router.post(
    "/admin/global-announcement",
    name="å‘é€å…¨æœå…¬å‘Š",
    tags=["ç®¡ç†", "g0v0 API", "é€šçŸ¥"],
    response_model=GlobalAnnouncementResp,
)
async def send_global_announcement(
    session: Database,
    req: GlobalAnnouncementReq,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Send a global in-app announcement, optionally mirrored as PM from a bot/admin account."""
    current_user = await require_admin(session, user_and_token)

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message cannot be empty")

    severity = req.severity.lower()
    if severity not in {"info", "warning", "error"}:
        raise HTTPException(status_code=422, detail="severity must be one of: info, warning, error")

    sender: User | None = None
    if req.sender_user_id is not None:
        sender = await session.get(User, req.sender_user_id)
    elif req.sender_username:
        sender = (
            await session.exec(
                select(User).where(col(User.username) == req.sender_username.strip()).limit(1)
            )
        ).first()
    if sender is None:
        sender = await session.get(User, BANCHOBOT_ID)
    if sender is None:
        raise HTTPException(status_code=500, detail="Announcement sender user not found")
    sender_username = sender.username

    if req.online_only:
        connected_user_ids = [uid for uid, sockets in server.connect_client.items() if sockets]
        if not connected_user_ids:
            receivers: list[int] = []
        else:
            receivers = (
                await session.exec(
                    select(User.id).where(
                        col(User.id).in_(connected_user_ids),
                        User.id != BANCHOBOT_ID,
                        User.id != sender.id,
                        ~User.is_restricted_query(col(User.id)),
                    )
                )
            ).all()
    else:
        receivers = (
            await session.exec(
                select(User.id).where(
                    User.id != BANCHOBOT_ID,
                    User.id != sender.id,
                    ~User.is_restricted_query(col(User.id)),
                )
            )
        ).all()

    announcement = GlobalAnnouncement.init(
        source_user_id=current_user.id,
        title=req.title.strip() or "Server Announcement",
        message=message,
        severity=severity,  # pyright: ignore[reportArgumentType]
        receiver_ids=receivers,
    )
    await server.new_private_notification(announcement)

    if req.also_send_pm and receivers:
        targets = (
            await session.exec(
                select(User).where(
                    col(User.id).in_(receivers),
                )
            )
        ).all()

        for target in targets:
            channel = await ChatChannel.get_pm_channel(target.id, sender.id, session)
            if channel is None:
                user_min = min(target.id, sender.id)
                user_max = max(target.id, sender.id)
                channel = ChatChannel(
                    channel_name=f"pm_{user_min}_{user_max}",
                    description="Private message channel",
                    type=ChannelType.PM,
                )
                session.add(channel)
                await session.flush()
                await session.refresh(channel)

            await server.batch_join_channel([target, sender], channel)

            chat_msg = ChatMessage(
                channel_id=channel.channel_id,
                sender_id=sender.id,
                type=MessageType.PLAIN,
                content=f"[{announcement.title}] {message}",
            )
            session.add(chat_msg)
            await session.flush()
            await session.refresh(chat_msg)

            chat_resp = await ChatMessageModel.transform(chat_msg, includes=["sender"])
            await server.send_message_to_channel(chat_resp)
            pm_detail = ChannelMessage.init(
                message=chat_msg,
                user=sender,
                receiver=[target.id],
                channel_type=ChannelType.PM,
            )
            await server.new_private_notification(pm_detail)

        await session.commit()

    # ─── Popup hijack ───────────────────────────────────────────────────
    # Emit a synthetic UserAchievementUnlock per recipient on the same
    # chat:notification Redis channel that real medals use. The lazer
    # client renders these via its MedalOverlay -- a big slide-in popup
    # that interrupts whatever screen the player is on -- whereas the
    # GlobalAnnouncement above only lands in the notifications drawer.
    # Combining both: the announcement is durable AND visually loud.
    #
    # The synthetic achievement_id sits in a HUGE namespace
    # (10_000_000_000+) so it can never collide with a real medal even
    # if upstream osu! grows its catalogue tenfold. Each recipient gets
    # a slightly different timestamp-based id so the client treats them
    # as distinct events (it dedupes by id in some places).
    if req.show_popup and receivers:
        try:
            from app.models.achievement import Achievement
            from app.models.notification import UserAchievementUnlock

            popup_redis = get_redis()
            now_ms = int(utcnow().timestamp() * 1000)
            for idx, recipient_id in enumerate(receivers):
                synthetic = Achievement(
                    id=10_000_000_000 + now_ms + idx,
                    name=announcement.title,
                    desc=message,
                    assets_id="all-secret-bone",  # generic medal art that ships with osu!
                )
                detail = UserAchievementUnlock.init(
                    synthetic,
                    recipient_id,
                    GameMode.OSU,
                )
                await popup_redis.publish(
                    "chat:notification",
                    detail.model_dump_json(),
                )
        except Exception as popup_err:
            # Popup is the cherry on top -- never let it kill the
            # main announcement that's already been delivered.
            logger.warning(
                "Failed to emit announcement popup to {} recipients: {}",
                len(receivers), popup_err,
            )

    return GlobalAnnouncementResp(
        sent_to=len(receivers),
        severity=severity,
        title=announcement.title,
        online_only=req.online_only,
        sender_username=sender_username,
    )


# ========== User Management ==========

@router.get(
    "/admin/users",
    name="Get All Users",
    tags=["Admin", "g0v0 API"],
)
async def get_all_users(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    search: Annotated[str, Query(description="Optional username substring filter (case-insensitive)")] = "",
    limit: Annotated[int, Query(description="Cap on rows returned (0 = no cap, full table)", ge=0, le=500)] = 0,
):
    """Get users (admin only).

    Backwards compatible default: no params returns the full users table
    (the original behaviour). Pass ``search`` to filter by username
    substring and ``limit`` to cap the result set — useful for
    autocomplete pickers (recalc dropdown, etc.) so we don't ship
    hundreds of MB to the browser.
    """
    await require_admin(session, user_and_token)

    stmt = select(User)
    if search.strip():
        stmt = stmt.where(col(User.username).ilike(f"%{search.strip()}%"))
    stmt = stmt.order_by(col(User.id))
    if limit > 0:
        stmt = stmt.limit(limit)

    users = (await session.exec(stmt)).all()
    return [await user_to_dict(user, session) for user in users]


@router.get(
    "/admin/users/{user_id}",
    name="Get User Info",
    tags=["Admin", "g0v0 API"],
)
async def get_user(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get a specific user by ID (admin only)"""
    await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return await user_to_dict(user, session)


@router.patch(
    "/admin/users/{user_id}",
    name="Update User Info",
    tags=["Admin", "g0v0 API"],
)
async def update_user(
    session: Database,
    user_id: int,
    user_data: UserUpdateRequest,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update user information (admin only)"""
    await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Snapshot for the Discord title-grant feed: captured BEFORE any
    # mutation so we can post a clean before→after diff once the commit
    # succeeds. list() copies the JSON-decoded list — without the copy,
    # in-place reassignment below would silently mutate this snapshot too.
    titles_before: list[str] = list(user.torii_titles or [])

    # Snapshot the actor's username NOW, while the session still has live
    # attribute state. Reading it after the commit below would trigger an
    # async lazy-load on an expired attribute and crash with
    # "greenlet_spawn has not been called" (the request-scoped session
    # uses expire_on_commit=True). user_to_dict at the end of this
    # endpoint works because we await session.refresh(user) right after
    # the commit, but actor_user belongs to a different load path and
    # never gets refreshed.
    actor_user_for_log = user_and_token[0] if user_and_token else None
    actor_username_for_log: str | None = (
        actor_user_for_log.username if actor_user_for_log is not None else None
    )

    if user_data.username is not None:
        normalized_username = user_data.username.strip()
        if not normalized_username:
            raise HTTPException(status_code=422, detail="username cannot be empty")

        # Avoid uniqueness crashes and return clean validation error.
        existing_user = (
            await session.exec(
                select(User.id).where(
                    col(User.username) == normalized_username,
                    User.id != user_id,
                ).limit(1)
            )
        ).first()
        if existing_user is not None:
            raise HTTPException(status_code=422, detail="username is already in use")

        if normalized_username != user.username:
            user.username = normalized_username

    if user_data.country_code is not None:
        normalized_country = user_data.country_code.strip().upper()
        user.country_code = normalized_country if normalized_country else None

    if user_data.is_qat is not None:
        user.is_qat = user_data.is_qat

    if user_data.is_gmt is not None:
        user.is_gmt = user_data.is_gmt

    if user_data.is_admin is not None:
        user.is_admin = user_data.is_admin

    if user_data.torii_titles is not None:
        from app.models.torii_groups import TORII_GROUPS
        unknown = [k for k in user_data.torii_titles if k not in TORII_GROUPS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown torii_titles keys: {unknown}. Valid keys: {sorted(TORII_GROUPS)}",
            )
        user.torii_titles = user_data.torii_titles

    if user_data.badge is not None:
        import json
        # Note: Badges are stored as JSON, so awarded_at must be an ISO string, not datetime
        # We use plain dicts here instead of Badge TypedDict because JSON storage requires strings
        if isinstance(user_data.badge, str):
            try:
                badge_dict = json.loads(user_data.badge)
                # Ensure awarded_at is an ISO string (not datetime) for JSON storage
                if "awarded_at" in badge_dict:
                    if isinstance(badge_dict["awarded_at"], datetime):
                        badge_dict["awarded_at"] = badge_dict["awarded_at"].isoformat()
                    elif not isinstance(badge_dict["awarded_at"], str):
                        badge_dict["awarded_at"] = datetime.now().isoformat()
                else:
                    badge_dict["awarded_at"] = datetime.now().isoformat()

                # Ensure image@2x_url is present (use image_url as fallback)
                if "image@2x_url" not in badge_dict:
                    badge_dict["image@2x_url"] = badge_dict.get("image_url", "")

                # Store as list of badge dicts (JSON-compatible format)
                # Note: We store as dict with string dates for JSON compatibility, not Badge TypedDict with datetime
                user.badges = cast(Any, [badge_dict])
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                # If parsing fails, create a simple badge structure
                # Note: We store as dict with string dates for JSON compatibility
                user.badges = cast(Any, [{
                    "awarded_at": datetime.now().isoformat(),
                    "description": "",
                    "image_url": user_data.badge if user_data.badge.startswith("http") else "",
                    "image@2x_url": user_data.badge if user_data.badge.startswith("http") else "",
                    "url": "",
                }])
        elif isinstance(user_data.badge, dict):
            # Convert awarded_at to ISO string if it's a datetime
            awarded_at_str = datetime.now().isoformat()
            if "awarded_at" in user_data.badge:
                if isinstance(user_data.badge["awarded_at"], str):
                    awarded_at_str = user_data.badge["awarded_at"]
                elif isinstance(user_data.badge["awarded_at"], datetime):
                    awarded_at_str = user_data.badge["awarded_at"].isoformat()
                else:
                    awarded_at_str = datetime.now().isoformat()

            badge_dict = {
                "awarded_at": awarded_at_str,  # Store as ISO string for JSON
                "description": user_data.badge.get("description", ""),
                "image_url": user_data.badge.get("icon_url") or user_data.badge.get("image_url", ""),
                "image@2x_url": user_data.badge.get("image@2x_url") or user_data.badge.get("icon_url") or user_data.badge.get("image_url", ""),
                "url": user_data.badge.get("url", ""),
            }
            # Note: We store as dict with string dates for JSON compatibility, not Badge TypedDict with datetime
            user.badges = cast(Any, [badge_dict])
        elif isinstance(user_data.badge, list):
            # Legacy frontend may send a list of badge dicts. Keep only JSON-safe dict entries.
            safe_badges: list[dict[str, Any]] = []
            for entry in user_data.badge:
                if not isinstance(entry, dict):
                    continue
                awarded_at = entry.get("awarded_at")
                if isinstance(awarded_at, datetime):
                    awarded_at = awarded_at.isoformat()
                elif not isinstance(awarded_at, str):
                    awarded_at = datetime.now().isoformat()

                safe_badges.append(
                    {
                        "awarded_at": awarded_at,
                        "description": entry.get("description", ""),
                        "image_url": entry.get("icon_url") or entry.get("image_url", ""),
                        "image@2x_url": entry.get("image@2x_url")
                        or entry.get("icon_url")
                        or entry.get("image_url", ""),
                        "url": entry.get("url", ""),
                    }
                )
            user.badges = cast(Any, safe_badges)
        else:
            user.badges = []

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        error_text = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "username" in error_text and "duplicate" in error_text:
            raise HTTPException(status_code=422, detail="username is already in use")
        raise HTTPException(status_code=422, detail="invalid user update payload")
    await session.refresh(user)

    # Notify connected lazer clients that this user's public payload has
    # changed (groups / titles flipping in or out, badge awarded, country
    # rename, …). They re-fetch via GetUserRequest and re-render badges +
    # auras + the rest in place — no sign-out / restart required, and no
    # need for the admin to ping the user manually.
    from app.service.user_update_publisher import publish_user_updated
    await publish_user_updated(user.id)

    # Discord feed: post a diff embed when the title list changed during
    # this update. Skipped automatically when before == after, so saving
    # the modal without flipping any titles produces no spam. The actor
    # is the admin who made the change — surfaced in the embed footer so
    # the channel reads as an audit log, not just a notifications stream.
    # actor_username_for_log was captured at the top of the endpoint
    # before the commit (see comment there for the lazy-load rationale).
    from app.service.discord_title_feed import notify_titles_changed
    await notify_titles_changed(
        target_user=user,
        before=titles_before,
        after=list(user.torii_titles or []),
        actor_username=actor_username_for_log,
    )

    return await user_to_dict(user, session)


@router.post(
    "/admin/users/{user_id}/ban",
    name="Restrict User",
    tags=["Admin", "g0v0 API"],
    status_code=204,
)
async def ban_user(
    session: Database,
    user_id: int,
    redis: Redis,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Ban a user (admin only)"""
    current_user = await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot ban yourself")

    # Create restriction history
    restriction = UserAccountHistory(
        id=None,  # Will be auto-generated
        user_id=user_id,
        type=UserAccountHistoryType.RESTRICTION,
        description="Account restricted by admin",
        length=0,
        permanent=True,
    )
    session.add(restriction)
    await session.commit()
    await session.refresh(user)

    # Fire the ToriiHalo PM BEFORE eviction so the message rides the
    # currently-open WebSocket out to any live session — the user sees
    # the explanation in their chat tray the same instant the ban hits,
    # rather than only on their next reconnect.
    #
    # The connect-time hook in notification/server.py will re-send the
    # PM on every subsequent reconnect (we want the message persistent,
    # not one-shot), so a delivery race here isn't a real problem — at
    # worst the user sees the message a second later when they reconnect
    # after the eviction closes their socket.
    try:
        from app.router.notification.banchobot import bot as toriihalo
        from app.router.auth import RESTRICTED_LOGIN_MESSAGE
        bot_channel = await toriihalo._ensure_pm_channel(user, session)
        if bot_channel is not None:
            await toriihalo._send_message(
                bot_channel,
                RESTRICTED_LOGIN_MESSAGE,
                session,
            )
    except Exception as exc:
        logger.warning(f"Failed to push immediate restriction PM to user {user_id}: {exc}")

    # _send_message above commits internally, which expires every
    # attribute on `user`. The first access in _evict_user_from_live_services
    # (`user.id`) would then trigger an async re-fetch outside the
    # greenlet context and raise MissingGreenlet. Refreshing the user
    # rehydrates the attributes in the right context so the eviction
    # path can proceed normally.
    await session.refresh(user)

    await _evict_user_from_live_services(session, redis, user)


@router.post(
    "/admin/users/{user_id}/unban",
    name="Unrestrict User",
    tags=["Admin", "g0v0 API"],
    status_code=204,
)
async def unban_user(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Unban a user (admin only)"""
    await require_admin(session, user_and_token)

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Remove active restrictions
    restrictions = (
        await session.exec(
            select(UserAccountHistory).where(
                UserAccountHistory.user_id == user_id,
                UserAccountHistory.type == UserAccountHistoryType.RESTRICTION,
            )
        )
    ).all()

    for restriction in restrictions:
        await session.delete(restriction)

    await session.commit()


# ========== Manual supporter grant ==========
#
# Why this endpoint exists:
#
# The Ko-fi webhook path (`router/private/donations.py`) and the
# admin donation-match path (`router/private/admin_donations.py`)
# both call `apply_supporter_grant` to mutate the user's supporter
# state atomically — `is_supporter`, `has_supported`,
# `total_supporter_months`, `donor_end_at`, `support_level` all
# move together so the client gates / loyalty tiers / hexagon icon
# never end up in inconsistent combinations.
#
# Before this endpoint existed, the admin user-edit form (`update_user`
# below at /admin/users/{id} PATCH) could change `username`,
# `country_code`, `is_admin`, badges and titles, but NOT the
# supporter fields. So when an admin tried to "manually grant
# supporter" by adding the Supporter badge via the edit form, the
# client's gating boolean (`IsSupporter || HasSupported` — see
# `CustomUiHueHelper.IsDonatorTier` in torii-osu) stayed false and
# the UI accent hue picker remained locked even though the user
# visibly had the badge.
#
# Rather than expose the five raw fields as toggles (which would
# let admins create inconsistent states like
# `is_supporter=True, donor_end_at=NULL`), this endpoint takes a
# simple "grant N months" intent and routes it through the SAME
# `apply_supporter_grant` the donation flows use. Zero drift with
# the donation auto/match paths — if it works for a real $5 Ko-fi
# donation, it works for this.

class GrantSupporterReq(BaseModel):
    """Admin-side payload for manually granting supporter time to a user.

    Mirrors the shape of a single donation's "months_granted" effect.
    `reason` is free-form and only used for the audit log line — it
    never lands in the user's profile or any user-visible field.
    """

    months: int = Field(
        ge=1,
        le=120,
        description="Months of supporter to grant. Clamped to [1, 120] so a typo can't grant a literal decade by accident.",
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Optional free-form note for the audit log (e.g. 'comping for Ko-fi outage', 'fix-up for botched match #123').",
    )


class GrantSupporterResp(BaseModel):
    """Post-grant snapshot of the user's supporter state. Lets the admin
    UI render an immediate confirmation without needing a second
    GET /admin/users/{id} roundtrip."""

    user_id: int
    username: str
    months_granted: int
    total_supporter_months: int
    donor_end_at: datetime | None
    is_currently_supporting: bool


@router.post(
    "/admin/users/{user_id}/grant-supporter",
    name="Manually Grant User Supporter Duration",
    tags=["Admin", "g0v0 API"],
    response_model=GrantSupporterResp,
)
async def grant_supporter(
    session: Database,
    user_id: int,
    body: GrantSupporterReq,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
) -> GrantSupporterResp:
    """Grant the target user N months of supporter time using the same
    code path the donation auto-match / admin-match endpoints use.

    Effects (in one DB transaction):
      - `is_supporter` and `has_supported` flip to True (permanent
        after first grant — same semantics as a real donation).
      - `total_supporter_months` increments by `months`.
      - `donor_end_at` extends from max(now, current end) by
        30 days × months.
      - `support_level` snaps to the loyalty tier for the new total.

    Refuses to:
      - Target a non-existent user (404).
      - Target the requesting admin themselves — admins granting
        themselves supporter time is loud-noise abuse-resistant
        rather than a hard prohibition, but for v1 we just block it
        (use SQL or another admin if you genuinely need to).
      - Accept months outside [1, 120].

    The grant DOES NOT need the client to log out / log in
    immediately to take effect server-side — anywhere on the server
    that reads supporter state from the DB sees the new values right
    after this returns. The client, however, only refreshes its
    cached `LocalUser` on login, so a user who wants the UI accent
    hue picker (or any other supporter-gated client feature) to
    light up will need to log out and log back in once after the
    grant.
    """
    admin_user = await require_admin(session, user_and_token)

    target_user = await session.get(User, user_id)
    if target_user is None:
        raise HTTPException(status_code=404, detail="User not found.")

    if target_user.id == admin_user.id:
        raise HTTPException(
            status_code=400,
            detail="Refusing to grant supporter to yourself. Use a second admin or raw SQL if you genuinely need this.",
        )

    await apply_supporter_grant(
        session, user=target_user, months_granted=body.months
    )
    await session.commit()
    await session.refresh(target_user)

    logger.info(
        "Admin {} manually granted {} month(s) of supporter to user {} (id={}). Reason: {}",
        admin_user.username,
        body.months,
        target_user.username,
        target_user.id,
        body.reason or "(none)",
    )

    return GrantSupporterResp(
        user_id=target_user.id or 0,
        username=target_user.username,
        months_granted=body.months,
        total_supporter_months=target_user.total_supporter_months or 0,
        donor_end_at=target_user.donor_end_at,
        is_currently_supporting=is_currently_supporting(target_user),
    )


# ========== Beatmap Blacklist ==========

@router.get(
    "/admin/beatmaps/blacklist",
    name="Get Blacklisted Beatmaps",
    tags=["Admin", "g0v0 API"],
)
async def get_blacklisted_beatmaps(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get all blacklisted beatmaps (admin only).

    Returns ONE row per blacklisted beatmap (not deduped by beatmapset
    anymore). A "ban set" action inserts one BannedBeatmaps row per
    difficulty in the set, so a 5-difficulty set ban surfaces here as
    5 rows with the same beatmapset_id but different beatmap_id +
    version. The frontend groups them visually as needed.

    The dedup behaviour was removed so that single-map bans (where
    only one difficulty is banned out of the set) are actually
    surfaced -- previously the first-seen-set check meant they would
    be hidden behind another ban for the same set, OR they wouldn't
    show at all if no other difficulty in the set was banned.
    """
    await require_admin(session, user_and_token)

    # Pull all banned rows in one shot, then batch-fetch beatmaps and
    # beatmapsets to avoid an N+1 round trip when the blacklist grows.
    banned_beatmaps = (await session.exec(select(BannedBeatmaps))).all()
    if not banned_beatmaps:
        return []

    beatmap_ids = [b.beatmap_id for b in banned_beatmaps]
    beatmaps = (
        await session.exec(select(Beatmap).where(col(Beatmap.id).in_(beatmap_ids)))
    ).all()
    beatmap_by_id = {b.id: b for b in beatmaps}

    beatmapset_ids = list({b.beatmapset_id for b in beatmaps})
    beatmapsets = (
        await session.exec(select(Beatmapset).where(col(Beatmapset.id).in_(beatmapset_ids)))
    ).all() if beatmapset_ids else []
    beatmapset_by_id = {bs.id: bs for bs in beatmapsets}

    result: list[BeatmapBlacklistItem] = []
    for banned_item in banned_beatmaps:
        beatmap = beatmap_by_id.get(banned_item.beatmap_id)
        if not beatmap:
            # Skip rows whose beatmap isn't local -- the admin UI can't
            # render anything useful for them and they'd just look like
            # broken entries. They remain in the DB and still gate
            # submissions; only the listing hides them.
            continue
        beatmapset_id = beatmap.beatmapset_id
        beatmapset = beatmapset_by_id.get(beatmapset_id)

        beatmapset_dict = None
        if beatmapset:
            beatmapset_dict = {
                "id": beatmapset.id,
                "title": beatmapset.title,
                "artist": beatmapset.artist,
            }
        beatmap_dict = {
            "id": beatmap.id,
            "version": beatmap.version,
            "difficulty_rating": beatmap.difficulty_rating,
            "mode": beatmap.mode,
            "total_length": beatmap.total_length,
            "bpm": beatmap.bpm,
        }
        result.append(
            BeatmapBlacklistItem(
                id=banned_item.id or 0,
                beatmapset_id=beatmapset_id,
                beatmap_id=banned_item.beatmap_id,
                source=banned_item.source,
                reason=banned_item.reason,
                beatmapset=beatmapset_dict,
                beatmap=beatmap_dict,
            )
        )

    return result


class BeatmapBlacklistRequest(BaseModel):
    beatmapset_id: int | None = None
    beatmap_id: int | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.beatmapset_id is None and self.beatmap_id is None:
            raise ValueError("Either beatmapset_id or beatmap_id is required")
        if self.beatmapset_id is not None and self.beatmap_id is not None:
            raise ValueError("Provide only one of beatmapset_id or beatmap_id")
        return self


@router.post(
    "/admin/beatmaps/blacklist",
    name="Add Beatmap to Blacklist",
    tags=["Admin", "g0v0 API"],
    status_code=201,
)
async def add_blacklisted_beatmap(
    session: Database,
    request: BeatmapBlacklistRequest,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Add a beatmap or beatmapset to blacklist (admin only)"""
    await require_admin(session, user_and_token)

    if request.beatmap_id is not None:
        beatmap_id = request.beatmap_id
        beatmap = await session.get(Beatmap, beatmap_id)
        if not beatmap:
            raise HTTPException(status_code=404, detail="Beatmap not found")

        existing_banned = (
            await session.exec(
                select(BannedBeatmaps).where(BannedBeatmaps.beatmap_id == beatmap_id)
            )
        ).first()
        if existing_banned:
            raise HTTPException(status_code=400, detail="Beatmap is already blacklisted")

        session.add(BannedBeatmaps(
            beatmap_id=beatmap_id,
            source="manual",
            reason="manual admin blacklist",
        ))
        await session.commit()
        return {
            "beatmap_id": beatmap_id,
            "beatmapset_id": beatmap.beatmapset_id,
            "message": "Beatmap added to blacklist",
        }

    beatmapset_id = request.beatmapset_id
    if beatmapset_id is None:
        raise HTTPException(status_code=422, detail="beatmapset_id is required")

    # Verify beatmapset exists
    beatmapset = await session.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Get all beatmaps in this beatmapset
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset_id)
        )
    ).all()

    if not beatmaps:
        raise HTTPException(status_code=404, detail="No beatmaps found in this beatmapset")

    # Idempotente: baneamos solo los beatmaps del set que NO estan ya baneados, en vez de
    # fallar cuando algun diff ya estaba (eso rompia el "Add to blacklist" del website con un 400).
    beatmap_ids = [b.id for b in beatmaps]
    already_banned_ids = set(
        (
            await session.exec(
                select(BannedBeatmaps.beatmap_id).where(col(BannedBeatmaps.beatmap_id).in_(beatmap_ids))
            )
        ).all()
    )

    to_ban = [b for b in beatmaps if b.id not in already_banned_ids]

    if not to_ban:
        return {
            "beatmapset_id": beatmapset_id,
            "added": 0,
            "already_blacklisted": len(already_banned_ids),
            "message": "All beatmaps in this beatmapset were already blacklisted",
        }

    for beatmap in to_ban:
        session.add(BannedBeatmaps(
            beatmap_id=beatmap.id,
            source="manual",
            reason="manual admin beatmapset blacklist",
        ))

    await session.commit()

    return {
        "beatmapset_id": beatmapset_id,
        "added": len(to_ban),
        "already_blacklisted": len(already_banned_ids),
        "message": "Beatmapset added to blacklist",
    }


@router.delete(
    "/admin/beatmaps/blacklist/beatmap/{beatmap_id}",
    name="ä»Žé»‘åå•ç§»é™¤å•ä¸ªè°±é¢",
    tags=["ç®¡ç†", "g0v0 API"],
    status_code=204,
)
async def remove_blacklisted_single_beatmap(
    session: Database,
    beatmap_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Remove a single beatmap from blacklist (admin only)"""
    await require_admin(session, user_and_token)

    banned_item = (
        await session.exec(
            select(BannedBeatmaps).where(BannedBeatmaps.beatmap_id == beatmap_id)
        )
    ).first()
    if not banned_item:
        raise HTTPException(status_code=404, detail="Beatmap not in blacklist")

    await session.delete(banned_item)
    await session.commit()


@router.delete(
    "/admin/beatmaps/blacklist/{beatmapset_id}",
    name="Remove Beatmap from Blacklist",
    tags=["Admin", "g0v0 API"],
    status_code=204,
)
async def remove_blacklisted_beatmap(
    session: Database,
    beatmapset_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Remove a beatmapset from blacklist (admin only)"""
    await require_admin(session, user_and_token)

    # Get all beatmaps in this beatmapset
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset_id)
        )
    ).all()

    if not beatmaps:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Get all banned beatmaps for this beatmapset
    beatmap_ids = [b.id for b in beatmaps]
    banned_items = (
        await session.exec(
            select(BannedBeatmaps).where(col(BannedBeatmaps.beatmap_id).in_(beatmap_ids))
        )
    ).all()

    if not banned_items:
        raise HTTPException(status_code=404, detail="Beatmapset not in blacklist")

    # Remove all banned entries for this beatmapset
    for banned_item in banned_items:
        await session.delete(banned_item)

    await session.commit()


@router.post(
    "/admin/beatmaps/blacklist/reverify",
    name="Re-verify banned beatmaps",
    tags=["Admin", "g0v0 API"],
)
async def reverify_blacklisted_beatmaps(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """
    Re-verify automatic entries in BannedBeatmaps using the correct .osu fetcher.
    Manual blacklist entries are preserved.
    The next hourly recalculate_banned_beatmap task will restore PP and BestScore
    entries for any maps removed here. Admin only.
    """
    await require_admin(session, user_and_token)
    from app.tasks.recalculate_banned_beatmap import reverify_banned_beatmaps
    result = await reverify_banned_beatmaps()
    return result


# ========== Beatmap Management ==========

@router.get(
    "/admin/beatmaps",
    name="Get All Beatmaps",
    tags=["Admin", "g0v0 API"],
)
async def get_beatmaps(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    search: str = Query("", description="Search by artist, title, or ID"),
):
    """Get all beatmaps with pagination (admin only)"""
    await require_admin(session, user_and_token)

    offset = (page - 1) * limit

    # Build query with optional search
    query = select(Beatmapset)

    if search:
        search_term = f"%{search}%"
        # Try to parse as ID first
        try:
            search_id = int(search)
            query = query.where(
                sql_or(
                    col(Beatmapset.id) == search_id,
                    col(Beatmapset.title).like(search_term),
                    col(Beatmapset.artist).like(search_term)
                )
            )
        except ValueError:
            # Not a number, search in title and artist
            query = query.where(
                sql_or(
                    col(Beatmapset.title).like(search_term),
                    col(Beatmapset.artist).like(search_term)
                )
            )

    # Get total count
    if search:
        total_count = (await session.exec(select(func.count()).select_from(query.subquery()))).one()
    else:
        total_count = (await session.exec(select(func.count()).select_from(Beatmapset))).one()

    # Get beatmapsets with pagination
    beatmapsets = (
        await session.exec(
            query
            .order_by(col(Beatmapset.id).desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    result = []
    for beatmapset in beatmapsets:
        # Get beatmaps for this set
        beatmaps = (
            await session.exec(
                select(Beatmap).where(Beatmap.beatmapset_id == beatmapset.id)
            )
        ).all()

        # Get cover URL
        cover_url = None
        if beatmapset.covers:
            cover_url = beatmapset.covers.get("cover") or beatmapset.covers.get("card")

        beatmapset_dict = {
            "id": beatmapset.id,
            "title": beatmapset.title,
            "artist": beatmapset.artist,
            "creator": beatmapset.creator,
            "rank_status": beatmapset.beatmap_status.name.lower() if beatmapset.beatmap_status else None,
            "covers": beatmapset.covers,
            "cover_url": cover_url,
            "beatmaps": [
                {
                    "id": b.id,
                    "version": b.version,
                    "difficulty_rating": b.difficulty_rating,
                    "mode": b.mode.value if b.mode else None,
                }
                for b in beatmaps
            ],
        }
        result.append(beatmapset_dict)

    return {
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": (total_count + limit - 1) // limit,
        "beatmapsets": result,
    }


@router.get(
    "/admin/beatmaps/{beatmap_id}",
    name="Get Beatmap Details",
    tags=["Admin", "g0v0 API"],
)
async def get_beatmap_details(
    session: Database,
    beatmap_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get beatmap details (admin only)"""
    await require_admin(session, user_and_token)

    beatmapset = await session.get(Beatmapset, beatmap_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Get all beatmaps in this set
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset.id)
        )
    ).all()

    # Get cover URL
    cover_url = None
    if beatmapset.covers:
        cover_url = beatmapset.covers.get("cover") or beatmapset.covers.get("card")

    return {
        "id": beatmapset.id,
        "title": beatmapset.title,
        "artist": beatmapset.artist,
        "creator": beatmapset.creator,
        "rank_status": beatmapset.beatmap_status.name.lower() if beatmapset.beatmap_status else None,
        "covers": beatmapset.covers,
        "cover_url": cover_url,
        "beatmaps": [
            {
                "id": b.id,
                "version": b.version,
                "difficulty_rating": b.difficulty_rating,
                "mode": b.mode.value if b.mode else None,
            }
            for b in beatmaps
        ],
    }


class RankStatusUpdate(BaseModel):
    status: str


@router.post(
    "/admin/beatmaps/{beatmapset_id}/rank",
    name="Update Beatmap Status",
    tags=["Admin", "g0v0 API"],
)
async def update_beatmap_rank_status(
    session: Database,
    beatmapset_id: int,
    request: RankStatusUpdate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update beatmapset rank status (admin only)"""
    await require_admin(session, user_and_token)

    from app.models.beatmap import BeatmapRankStatus

    try:
        new_status = BeatmapRankStatus(request.status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid rank status: {request.status}")

    beatmapset = await session.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    beatmapset.beatmap_status = new_status
    await session.commit()
    await session.refresh(beatmapset)

    return {"id": beatmapset.id, "rank_status": beatmapset.beatmap_status.value}


@router.post(
    "/admin/beatmaps/{beatmapset_id}/ban",
    name="Ban Beatmap",
    tags=["Admin", "g0v0 API"],
)
async def ban_beatmapset(
    session: Database,
    beatmapset_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Ban a beatmapset and remove all scores (admin only)"""
    await require_admin(session, user_and_token)

    from app.database.score import Score

    # Get all beatmaps in this set
    beatmaps = (
        await session.exec(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset_id)
        )
    ).all()

    if not beatmaps:
        raise HTTPException(status_code=404, detail="Beatmapset not found")

    # Delete all scores for these beatmaps
    from app.service.points_service import reverse_score_points

    for beatmap in beatmaps:
        scores = (
            await session.exec(
                select(Score).where(Score.beatmap_id == beatmap.id)
            )
        ).all()
        for score in scores:
            # Claw back any top-play points the score granted before deleting it,
            # so a re-landed play can't re-earn for the same content.
            await reverse_score_points(session, score.id)
            await session.delete(score)

    # Add all beatmaps to blacklist
    for beatmap in beatmaps:
        # Check if already blacklisted
        existing = (
            await session.exec(
                select(BannedBeatmaps).where(BannedBeatmaps.beatmap_id == beatmap.id)
            )
        ).first()
        if not existing:
            banned_beatmap = BannedBeatmaps(beatmap_id=beatmap.id)
            session.add(banned_beatmap)

    await session.commit()

    return {"beatmapset_id": beatmapset_id, "message": "Beatmapset banned and scores removed"}


# ========== User Wipe ==========

class WipeRequest(BaseModel):
    mode: str  # e.g., "osu", "taiko", "fruits", "mania"


@router.post(
    "/admin/users/{user_id}/wipe",
    name="Wipe User Data",
    tags=["Admin", "g0v0 API"],
)
async def wipe_user_stats(
    session: Database,
    user_id: int,
    request: WipeRequest,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Wipe user statistics and scores for a specific mode (admin only)"""
    await require_admin(session, user_and_token)

    from app.database.score import Score
    from app.models.score import GameMode

    try:
        mode = GameMode(request.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid game mode: {request.mode}")

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Delete all scores for this user and mode
    scores = (
        await session.exec(
            select(Score).where(
                Score.user_id == user_id,
                Score.gamemode == mode,
            )
        )
    ).all()

    from app.service.points_service import reverse_score_points

    deleted_count = 0
    for score in scores:
        # Claw back any top-play points before deleting so a re-wipe + resubmit
        # can't re-earn for the same play.
        await reverse_score_points(session, score.id)
        await session.delete(score)
        deleted_count += 1

    await session.commit()

    return {
        "user_id": user_id,
        "mode": request.mode,
        "deleted_scores": deleted_count,
        "message": f"Wiped {deleted_count} scores for mode {request.mode}",
    }


# ========== Badge Management ==========
# Now using user_badges table instead of JSON in User.badges field

@router.get(
    "/admin/user-badges",
    name="Get All Badges",
    tags=["Admin", "g0v0 API"],
)
async def get_user_badges(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get all badges from user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    try:
        # Join with User to get username
        statement = (
            select(UserBadge, User.username)
            .outerjoin(User, col(UserBadge.user_id) == User.id)
            .order_by(col(UserBadge.id).desc())
        )
        results = (await session.exec(statement)).all()
    except Exception:
        # Keep admin page usable even if table/schema is missing in a partially migrated environment.
        return []

    badges = []
    for badge, username in results:
        badge_dict = badge.model_dump()
        badge_dict["username"] = username
        # Convert datetime to ISO string
        if badge_dict.get("awarded_at") and isinstance(badge_dict["awarded_at"], datetime):
            badge_dict["awarded_at"] = badge_dict["awarded_at"].isoformat()
        badges.append(badge_dict)

    return badges


@router.post(
    "/admin/user-badges",
    name="Create Badge",
    tags=["Admin", "g0v0 API"],
    status_code=201,
)
async def create_user_badge(
    session: Database,
    badge_data: UserBadgeCreate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Create a badge in user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    # Set default awarded_at if not provided
    awarded_at = badge_data.awarded_at or datetime.now()

    # Create new badge
    new_badge = UserBadge(
        description=badge_data.description,
        image_url=badge_data.image_url,
        image_2x_url=badge_data.image_2x_url or badge_data.image_url,
        url=badge_data.url or "",
        awarded_at=awarded_at,
        user_id=badge_data.user_id,
    )

    session.add(new_badge)
    await session.commit()
    await session.refresh(new_badge)

    return new_badge


@router.patch(
    "/admin/user-badges/{badge_id}",
    name="Update Badge",
    tags=["Admin", "g0v0 API"],
)
async def update_user_badge(
    session: Database,
    badge_id: int,
    badge_data: UserBadgeUpdate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update a badge in user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    # Get the badge
    badge = await session.get(UserBadge, badge_id)
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")

    # Update fields if provided
    if badge_data.description is not None:
        badge.description = badge_data.description
    if badge_data.image_url is not None:
        badge.image_url = badge_data.image_url
    if badge_data.image_2x_url is not None:
        badge.image_2x_url = badge_data.image_2x_url
    if badge_data.url is not None:
        badge.url = badge_data.url
    if badge_data.awarded_at is not None:
        badge.awarded_at = badge_data.awarded_at
    if badge_data.user_id is not None:
        badge.user_id = badge_data.user_id

    await session.commit()
    await session.refresh(badge)

    return badge


@router.delete(
    "/admin/user-badges/{badge_id}",
    name="Delete Badge",
    tags=["Admin", "g0v0 API"],
    status_code=204,
)
async def delete_user_badge(
    session: Database,
    badge_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Delete a badge from user_badges table (admin only)"""
    await require_admin(session, user_and_token)

    badge = await session.get(UserBadge, badge_id)
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")

    await session.delete(badge)
    await session.commit()


# ========== Team Management ==========

@router.get(
    "/admin/teams",
    name="Get All Teams",
    tags=["Admin", "g0v0 API"],
)
async def get_all_teams(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get all teams (admin only)"""
    await require_admin(session, user_and_token)

    teams = (await session.exec(select(Team).order_by(col(Team.created_at).desc()))).all()
    return teams


@router.patch(
    "/admin/teams/{team_id}",
    name="Update Team",
    tags=["Admin", "g0v0 API"],
)
async def update_team_admin(
    session: Database,
    team_id: int,
    storage: StorageService,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    flag: bytes | None = File(None),
    cover: bytes | None = File(None),
    name: str | None = Form(None, max_length=100),
    short_name: str | None = Form(None, max_length=10),
    leader_id: int | None = Form(None),
    playmode: GameMode | None = Form(None),
    description: str | None = Form(None, max_length=2000),
    website: str | None = Form(None, max_length=255),
):
    """Update team (admin only)."""
    await require_admin(session, user_and_token)

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise HTTPException(status_code=400, detail="Team name cannot be empty")
        if (
            await session.exec(
                select(exists()).where(
                    Team.name == clean_name,
                    Team.id != team_id,
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Name already exists")
        team.name = clean_name

    if short_name is not None:
        clean_short_name = short_name.strip()
        if not clean_short_name:
            raise HTTPException(status_code=400, detail="Team short name cannot be empty")
        if (
            await session.exec(
                select(exists()).where(
                    Team.short_name == clean_short_name,
                    Team.id != team_id,
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Short name already exists")
        team.short_name = clean_short_name

    if playmode is not None:
        team.playmode = playmode

    if description is not None:
        clean_description = description.strip()
        team.description = clean_description or None

    if website is not None:
        clean_website = website.strip()
        if clean_website and not (clean_website.startswith("http://") or clean_website.startswith("https://")):
            clean_website = "https://" + clean_website
        team.website = clean_website or None

    if flag is not None:
        fmt = check_image(flag, 2 * 1024 * 1024, 240, 120)
        if old_flag := team.flag_url:
            if path := storage.get_file_name_by_url(old_flag):
                await storage.delete_file(path)
        filehash = hashlib.sha256(flag).hexdigest()
        storage_path = f"team_flag/{team.id}_{filehash}.png"
        if not await storage.is_exists(storage_path):
            await storage.write_file(storage_path, flag, f"image/{fmt}")
        team.flag_url = await storage.get_file_url(storage_path)

    if cover is not None:
        fmt = check_image(cover, 10 * 1024 * 1024, 3000, 2000)
        if old_cover := team.cover_url:
            if path := storage.get_file_name_by_url(old_cover):
                await storage.delete_file(path)
        filehash = hashlib.sha256(cover).hexdigest()
        storage_path = f"team_cover/{team.id}_{filehash}.png"
        if not await storage.is_exists(storage_path):
            await storage.write_file(storage_path, cover, f"image/{fmt}")
        team.cover_url = await storage.get_file_url(storage_path)

    if leader_id is not None:
        if not (await session.exec(select(exists()).where(User.id == leader_id))).first():
            raise HTTPException(status_code=404, detail="Leader not found")
        is_member = (
            await session.exec(
                select(exists()).where(
                    TeamMember.user_id == leader_id,
                    TeamMember.team_id == team_id,
                )
            )
        ).first()
        if not is_member:
            raise HTTPException(status_code=404, detail="Leader is not a member of the team")
        team.leader_id = leader_id

    await session.commit()
    await session.refresh(team)

    redis = get_redis()
    cache_service = get_ranking_cache_service(redis)
    await cache_service.invalidate_team_cache()

    return team


@router.delete(
    "/admin/teams/{team_id}",
    name="Delete Team",
    tags=["Admin", "g0v0 API"],
    status_code=204,
)
async def delete_team_admin(
    session: Database,
    team_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Delete a team (admin only)"""
    await require_admin(session, user_and_token)

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Mirror the user-facing delete_team in private/team.py: clear the
    # team_members rows first because the FK on team_members.team_id →
    # teams.id is declared without ON DELETE CASCADE. Materialise the
    # ScalarResult with .all() before mutating the session to avoid the
    # SQLAlchemy `MissingGreenlet` race where iterating an open result
    # while issuing concurrent deletes on the same connection trips the
    # async-IO guard.
    team_members = (await session.exec(select(TeamMember).where(TeamMember.team_id == team_id))).all()
    for member in team_members:
        await session.delete(member)

    await session.delete(team)
    await session.commit()


# ========== Daily Challenge Statistics ==========

class DailyChallengeStatsResponse(BaseModel):
    user_id: int
    daily_streak_best: int = 0
    daily_streak_current: int = 0
    weekly_streak_best: int = 0
    weekly_streak_current: int = 0
    top_10p_placements: int = 0
    top_50p_placements: int = 0
    playcount: int = 0
    last_update: str | None = None  # ISO format
    last_weekly_streak: str | None = None  # ISO format


@router.get(
    "/admin/daily-challenge/stats/{user_id}",
    name="Get User Daily Challenge Stats",
    tags=["Admin", "g0v0 API"],
    response_model=DailyChallengeStatsResponse,
)
async def get_daily_challenge_stats(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get daily challenge statistics for a user (admin only) - Matches osu.Game APIUserDailyChallengeStatistics"""
    await require_admin(session, user_and_token)

    # For now, return default stats. In a real implementation, this would query user statistics
    # from a dedicated daily challenge statistics table or calculate from scores
    return DailyChallengeStatsResponse(
        user_id=user_id,
        daily_streak_best=0,
        daily_streak_current=0,
        weekly_streak_best=0,
        weekly_streak_current=0,
        top_10p_placements=0,
        top_50p_placements=0,
        playcount=0,
        last_update=None,
        last_weekly_streak=None,
    )


# ========== Daily Challenge Management ==========

class DailyChallengeListResponse(BaseModel):
    total: int
    challenges: list[DailyChallengeResponse]
    page: int = 1
    per_page: int = 50


@router.get(
    "/admin/daily-challenges",
    name="Get Daily Challenges",
    tags=["Admin", "g0v0 API"],
    response_model=DailyChallengeListResponse,
)
async def list_daily_challenges(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=100, description="Items per page"),
    date_from: str | None = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="End date (YYYY-MM-DD)"),
):
    """List daily challenges with pagination and optional date filtering - Enhanced for osu.Game compatibility"""
    await require_admin(session, user_and_token)

    # Build query
    query = select(DailyChallenge)

    # Apply date filtering if provided
    if date_from:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            query = query.where(col(DailyChallenge.date) >= from_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from format. Use YYYY-MM-DD")

    if date_to:
        try:
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            query = query.where(col(DailyChallenge.date) <= to_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to format. Use YYYY-MM-DD")

    # Order by date descending (newest first)
    query = query.order_by(col(DailyChallenge.date).desc())

    # Get total count
    total_query = select(func.count()).select_from(DailyChallenge)
    if date_from:
        total_query = total_query.where(col(DailyChallenge.date) >= from_date)
    if date_to:
        total_query = total_query.where(col(DailyChallenge.date) <= to_date)

    total = (await session.exec(total_query)).one()

    # Apply pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    challenges = (await session.exec(query)).all()

    # Add beatmap info to each challenge
    challenges_with_beatmap = []
    for challenge in challenges:
        # Convert to response model to avoid SQLModel/Pydantic validation errors
        challenge_res = DailyChallengeResponse.model_validate(challenge)
        beatmap = await session.get(Beatmap, challenge.beatmap_id)
        if beatmap:
            # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
            beatmapset = await beatmap.awaitable_attrs.beatmapset
            challenge_res.beatmap = {
                "id": beatmap.id,
                "beatmapset_id": beatmap.beatmapset_id,
                "title": beatmapset.title if beatmapset else "Unknown",
                "artist": beatmapset.artist if beatmapset else "Unknown",
                "creator": beatmapset.creator if beatmapset else None,
                "version": beatmap.version,
                "difficulty_rating": beatmap.difficulty_rating,
                "total_length": beatmap.total_length,
                "bpm": beatmap.bpm,
                "mode": beatmap.mode.name if beatmap.mode else None,
            }
        challenges_with_beatmap.append(challenge_res)

    return DailyChallengeListResponse(
        total=total,
        challenges=challenges_with_beatmap,
        page=page,
        per_page=per_page,
    )

@router.get(
    "/admin/daily-challenge/{date}",
    name="Get Daily Challenge",
    tags=["Admin", "g0v0 API"],
    response_model=DailyChallengeResponse | None,
)
async def get_daily_challenge(
    session: Database,
    date: str,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Get daily challenge for a specific date (admin only)"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if not challenge:
        return None

    # Convert to response model to avoid SQLModel/Pydantic validation errors
    challenge_res = DailyChallengeResponse.model_validate(challenge)

    # Try to get beatmap info
    beatmap = await session.get(Beatmap, challenge.beatmap_id)
    if beatmap:
        # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
        beatmapset = await beatmap.awaitable_attrs.beatmapset
        challenge_res.beatmap = {
            "id": beatmap.id,
            "title": beatmapset.title if beatmapset else "Unknown",
            "artist": beatmapset.artist if beatmapset else "Unknown",
            "difficulty_rating": beatmap.difficulty_rating,
        }

    return challenge_res


@router.post(
    "/admin/daily-challenge/trigger",
    name="Manually Trigger Daily Challenge",
    tags=["Admin", "g0v0 API"],
)
async def trigger_daily_challenge(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Manually trigger the daily challenge job (admin only)"""
    await require_admin(session, user_and_token)

    from app.tasks.daily_challenge import daily_challenge_job
    await daily_challenge_job()

    return {"message": "Daily challenge job triggered successfully"}


@router.post(
    "/admin/daily-challenge",
    name="Create Daily Challenge",
    tags=["Admin", "g0v0 API"],
    status_code=201,
    response_model=DailyChallengeResponse,
)
async def create_daily_challenge(
    session: Database,
    challenge_data: DailyChallengeCreate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Create a daily challenge (admin only) - Enhanced to match osu.Game Room structure"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(challenge_data.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # Single chokepoint: confirms beatmap exists, beatmap.mode matches ruleset_id,
    # and every acronym in the mod payloads is real for that ruleset. Returns the
    # parsed APIMod lists so we don't re-parse below.
    beatmap, required_mods_list, allowed_mods_list = await _validate_daily_challenge_inputs(
        session,
        beatmap_id=challenge_data.beatmap_id,
        ruleset_id=challenge_data.ruleset_id,
        required_mods_raw=challenge_data.required_mods,
        allowed_mods_raw=challenge_data.allowed_mods,
    )

    # Check if challenge already exists for this date
    existing_challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if existing_challenge:
        raise HTTPException(
            status_code=409,
            detail=f"A daily challenge already exists for {challenge_date.isoformat()}.",
        )

    # Check if room_id is already used (if provided)
    if hasattr(challenge_data, 'room_id') and challenge_data.room_id is not None:
        existing_room_challenge = (
            await session.exec(select(DailyChallenge).where(col(DailyChallenge.room_id) == challenge_data.room_id))
        ).first()
        if existing_room_challenge:
            raise HTTPException(status_code=409, detail="Room ID already in use by another daily challenge")

    # Store mods in canonical APIMod format so that the cron job can consume
    # them directly without further conversion.
    required_mods_json = json.dumps(required_mods_list)
    allowed_mods_json = json.dumps(allowed_mods_list)

    # Create new challenge with enhanced fields
    new_challenge = DailyChallenge(
        date=challenge_date,
        beatmap_id=challenge_data.beatmap_id,
        ruleset_id=challenge_data.ruleset_id,
        required_mods=required_mods_json,
        allowed_mods=allowed_mods_json,
        room_id=getattr(challenge_data, 'room_id', None),
        max_attempts=getattr(challenge_data, 'max_attempts', None),
        time_limit=getattr(challenge_data, 'time_limit', None),
    )

    # Sync to Redis (matching tools/add_daily_challenge.py)
    redis = get_redis()
    redis_key = f"daily_challenge:{challenge_date}"

    await redis.hset(
        redis_key,
        mapping={
            "beatmap": new_challenge.beatmap_id,
            "ruleset_id": new_challenge.ruleset_id,
            "required_mods": required_mods_json,
            "allowed_mods": allowed_mods_json,
        },
    )

    # Automatically assign room_id if for today and not provided
    sala_recien_creada = False
    if new_challenge.room_id is None and challenge_date == utcnow().date():
        now = utcnow()
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # Duration should be in minutes, not seconds
        duration = int((next_day - now).total_seconds() / 60)

        room = await create_daily_challenge_room(
            beatmap=new_challenge.beatmap_id,
            ruleset_id=new_challenge.ruleset_id,
            duration=duration,
            required_mods=required_mods_list,
            allowed_mods=allowed_mods_list,
        )
        new_challenge.room_id = room.id
        sala_recien_creada = True

    session.add(new_challenge)
    await session.commit()
    await session.refresh(new_challenge)

    # Si el challenge es para hoy, la sala nace ACA y no en el job del cron. El aviso al
    # #feed vivia solamente adentro del job, asi que cargar un challenge a mano abria la
    # sala y no avisaba nunca: cuando el job corria despues, veia que ya habia sala viva y
    # salia sin postear. Va despues del commit para no anunciar algo que no quedo guardado.
    if sala_recien_creada:
        from app.tasks.daily_challenge import announce_daily_challenge

        await announce_daily_challenge(
            new_challenge.beatmap_id,
            new_challenge.ruleset_id,
            required_mods_list,
        )

    # Refresh beatmap to avoid MissingGreenlet after commit
    if beatmap:
        await session.refresh(beatmap)

    # Add beatmap info to response
    challenge_res = DailyChallengeResponse.model_validate(new_challenge)
    if beatmap:
        # Re-fetch beatmap to ensure we have a fresh session-attached instance
        beatmap = await session.get(Beatmap, new_challenge.beatmap_id)
        if beatmap:
            # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
            beatmapset = await beatmap.awaitable_attrs.beatmapset
            challenge_res.beatmap = {
                "id": beatmap.id,
                "title": beatmapset.title if beatmapset else "Unknown",
                "artist": beatmapset.artist if beatmapset else "Unknown",
                "difficulty_rating": beatmap.difficulty_rating,
            }

    return challenge_res


@router.patch(
    "/admin/daily-challenge/{date}",
    name="Update Daily Challenge",
    tags=["Admin", "g0v0 API"],
    response_model=DailyChallengeResponse,
)
async def update_daily_challenge(
    session: Database,
    date: str,
    challenge_data: DailyChallengeUpdate,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Update a daily challenge (admin only) - Enhanced with new fields"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if not challenge:
        raise HTTPException(status_code=404, detail="Daily challenge not found")

    # A4 audit fix: if the body sneaks a `date` in (some clients do, e.g. echoing
    # the full record), reject the mismatch loudly. Date is the PK; mutating it
    # from this endpoint isn't supported.
    body_date = getattr(challenge_data, 'date', None)
    if body_date is not None and str(body_date) != date:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot change a daily challenge's date from {date} to {body_date}. "
                "Delete and recreate it on the new date instead."
            ),
        )

    # Determine the *post-update* (beatmap_id, ruleset_id) so we can validate
    # them as a coherent pair. The patch may touch either field, both, or
    # neither; whatever the final state is, it has to satisfy the same C2/C3
    # invariants the create endpoint enforces.
    next_beatmap_id = challenge_data.beatmap_id if getattr(challenge_data, 'beatmap_id', None) is not None else challenge.beatmap_id
    next_ruleset_id = challenge_data.ruleset_id if getattr(challenge_data, 'ruleset_id', None) is not None else challenge.ruleset_id
    next_required_raw = challenge_data.required_mods if getattr(challenge_data, 'required_mods', None) is not None else challenge.required_mods
    next_allowed_raw = challenge_data.allowed_mods if getattr(challenge_data, 'allowed_mods', None) is not None else challenge.allowed_mods

    _, validated_required, validated_allowed = await _validate_daily_challenge_inputs(
        session,
        beatmap_id=next_beatmap_id,
        ruleset_id=next_ruleset_id,
        required_mods_raw=next_required_raw,
        allowed_mods_raw=next_allowed_raw,
    )

    # Apply the validated state.
    challenge.beatmap_id = next_beatmap_id
    challenge.ruleset_id = next_ruleset_id
    if getattr(challenge_data, 'required_mods', None) is not None:
        challenge.required_mods = json.dumps(validated_required)
    if getattr(challenge_data, 'allowed_mods', None) is not None:
        challenge.allowed_mods = json.dumps(validated_allowed)
    if getattr(challenge_data, 'room_id', None) is not None:
        # Check if room_id is already used by another challenge
        existing_room_challenge = (
            await session.exec(
                select(DailyChallenge)
                .where(
                    col(DailyChallenge.room_id) == challenge_data.room_id,
                    col(DailyChallenge.date) != challenge_date
                )
            )
        ).first()
        if existing_room_challenge:
            raise HTTPException(status_code=409, detail="Room ID already in use by another daily challenge")
        challenge.room_id = challenge_data.room_id
    if getattr(challenge_data, 'max_attempts', None) is not None:
        challenge.max_attempts = challenge_data.max_attempts
    if getattr(challenge_data, 'time_limit', None) is not None:
        challenge.time_limit = challenge_data.time_limit

    # Sync to Redis (matching tools/add_daily_challenge.py)
    redis = get_redis()
    redis_key = f"daily_challenge:{challenge_date}"

    await redis.hset(
        redis_key,
        mapping={
            "beatmap": challenge.beatmap_id,
            "ruleset_id": challenge.ruleset_id,
            "required_mods": challenge.required_mods,
            "allowed_mods": challenge.allowed_mods,
        },
    )

    # Propagate mod/beatmap edits to the live room's playlist. The osu! client
    # reads the ROOM (room_playlists), not the daily_challenge row — so without
    # this, editing an already-materialised challenge silently no-ops in-game
    # (the bug behind "I fixed the mods but players still see the broken ones").
    if challenge.room_id is not None:
        from app.database.playlists import Playlist  # local import, mirrors Room in delete

        playlist_items = (
            await session.exec(select(Playlist).where(col(Playlist.room_id) == challenge.room_id))
        ).all()
        for item in playlist_items:
            item.beatmap_id = next_beatmap_id
            item.ruleset_id = next_ruleset_id
            item.required_mods = validated_required
            item.allowed_mods = validated_allowed
            session.add(item)

    # Automatically assign room_id if for today and not provided
    if challenge.room_id is None and challenge_date == utcnow().date():
        now = utcnow()
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # Duration should be in minutes, not seconds
        duration = int((next_day - now).total_seconds() / 60)

        required_mods_list = _parse_mods_raw(challenge.required_mods)
        allowed_mods_list = _parse_mods_raw(challenge.allowed_mods)

        room = await create_daily_challenge_room(
            beatmap=challenge.beatmap_id,
            ruleset_id=challenge.ruleset_id,
            duration=duration,
            required_mods=required_mods_list,
            allowed_mods=allowed_mods_list,
        )
        challenge.room_id = room.id

    await session.commit()
    await session.refresh(challenge)

    # Refresh beatmap to avoid MissingGreenlet after commit
    beatmap = await session.get(Beatmap, challenge.beatmap_id)
    if beatmap:
        await session.refresh(beatmap)

    # Add beatmap info to response
    challenge_res = DailyChallengeResponse.model_validate(challenge)
    if beatmap:
        # Re-fetch beatmap to ensure we have a fresh session-attached instance
        beatmap = await session.get(Beatmap, challenge.beatmap_id)
        if beatmap:
            # Get beatmapset info using awaitable_attrs to avoid MissingGreenlet error
            beatmapset = await beatmap.awaitable_attrs.beatmapset
            challenge_res.beatmap = {
                "id": beatmap.id,
                "title": beatmapset.title if beatmapset else "Unknown",
                "artist": beatmapset.artist if beatmapset else "Unknown",
                "difficulty_rating": beatmap.difficulty_rating,
            }

    return challenge_res


async def _purge_room_and_dependents(session: Database, room_id: int) -> None:
    """Hard-delete a multiplayer room and every row that FK-references it.

    The schema points several tables at rooms.id / scores.id WITHOUT
    ON DELETE CASCADE, so `DELETE FROM rooms` on its own trips MySQL error
    1451 (foreign key constraint fails) and the surrounding transaction
    rolls back. That's the bug behind "the daily challenge won't delete"
    (the endpoint 500s and nothing is removed). We tear the dependents down
    child-first so the final room delete actually lands:

      1. score-children keyed by this room's score ids
         (best_scores, total_score_best_scores, score_anticheat_analysis)
      2. playlist_best_scores  (room-scoped; also a score-child)
      3. the room's score rows
      4. room-children with no score link
         (item_attempts_count, multiplayer_events, room_participated_users)
      5. room_playlists, then the room itself

    Idempotent: every statement is room-scoped, so calling it for a room
    that is already partly gone simply deletes zero rows.
    """
    from sqlalchemy import text

    score_ids = "SELECT id FROM scores WHERE room_id = :rid"
    statements = (
        f"DELETE FROM best_scores WHERE score_id IN ({score_ids})",
        f"DELETE FROM total_score_best_scores WHERE score_id IN ({score_ids})",
        f"DELETE FROM score_anticheat_analysis WHERE score_id IN ({score_ids})",
        "DELETE FROM playlist_best_scores WHERE room_id = :rid",
        "DELETE FROM scores WHERE room_id = :rid",
        "DELETE FROM item_attempts_count WHERE room_id = :rid",
        "DELETE FROM multiplayer_events WHERE room_id = :rid",
        "DELETE FROM room_participated_users WHERE room_id = :rid",
        "DELETE FROM room_playlists WHERE room_id = :rid",
        "DELETE FROM rooms WHERE id = :rid",
    )
    for stmt in statements:
        await session.execute(text(stmt), {"rid": room_id})


@router.delete(
    "/admin/daily-challenge/{date}",
    name="Delete Daily Challenge",
    tags=["Admin", "g0v0 API"],
    status_code=204,
)
async def delete_daily_challenge(
    session: Database,
    date: str,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Delete a daily challenge (admin only)"""
    await require_admin(session, user_and_token)

    try:
        challenge_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    challenge = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()

    if not challenge:
        raise HTTPException(status_code=404, detail="Daily challenge not found")

    # Clean up the linked multiplayer Room (if any) and the Redis queue entry,
    # otherwise the challenge row goes but its materialised state lingers —
    # leaderboards keep referencing a "ghost" daily and the Redis key still
    # flags the date as taken to the cron job.
    #
    # The room and its dependents are torn down by _purge_room_and_dependents,
    # which deletes the FK-referencing rows (item_attempts_count,
    # multiplayer_events, playlist_best_scores, room_participated_users, scores)
    # before the room itself — those FKs have no ON DELETE CASCADE, so deleting
    # the room directly used to 500 with a 1451 constraint error.
    if challenge.room_id is not None:
        await _purge_room_and_dependents(session, challenge.room_id)

    redis = get_redis()
    try:
        await redis.delete(f"daily_challenge:{challenge_date}")
    except Exception as e:
        logger.warning(f"Failed to clear Redis queue entry for {challenge_date}: {e}")

    await session.delete(challenge)
    await session.commit()


# ──────────────────────────────────────────────────────────────────────────
# Daily Challenge — random map picker
#
# Convenience endpoint for admins to roll a candidate beatmap when
# building tomorrow's DC. The caller can constrain by mode and star
# range; we filter to ranked/approved/loved only (the only statuses
# eligible for daily challenge), then pick one at random server-side.
#
# We use SQL ORDER BY RAND() LIMIT 1 -- O(N) but the local beatmaps
# table is small enough (tens of thousands max) that a full scan once
# per admin click is fine. If this ever gets called from anything but
# a button press, swap to OFFSET + COUNT for a constant-cost picker.
# ──────────────────────────────────────────────────────────────────────────


class RandomDailyChallengeReq(BaseModel):
    """Body for the random-pick endpoint. All filters optional; defaults
    pick any ranked osu! beatmap of any difficulty. ``create_challenge``
    flips the endpoint between "preview" (just return the rolled beatmap
    metadata) and "create" (persist a DailyChallenge row for ``date``).
    """
    date: str | None = None
    ruleset_id: int = 0
    min_difficulty: float | None = None
    max_difficulty: float | None = None
    required_mods: str = "[]"
    allowed_mods: str = "[]"
    create_challenge: bool = False


@router.post(
    "/admin/daily-challenge/random",
    name="Randomly Pick Daily Challenge Beatmap",
    tags=["Admin", "g0v0 API"],
)
async def pick_random_daily_challenge_beatmap(
    session: Database,
    req: RandomDailyChallengeReq,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Roll one ranked/approved/loved beatmap matching the filters.

    The same endpoint serves two modes via ``create_challenge``:
      false (default) -> preview: just return the rolled beatmap.
      true            -> persist: create a DailyChallenge row for
                         ``date`` (or today's date in UTC if omitted).

    Two-stage flow lets the admin UI show a preview first ("here's
    what I'd pick — accept?") before committing, instead of dropping
    a fait-accompli on the playerbase if the random roll lands on
    something unsuitable.
    """
    await require_admin(session, user_and_token)

    import random
    from sqlalchemy import func as sa_func
    from sqlalchemy.orm import noload, lazyload
    from app.models.beatmap import BeatmapRankStatus

    # Eligible statuses match the rest of our DC code path. We include
    # QUALIFIED too because qualified maps still have leaderboards; they
    # might be the wrong choice for a multi-day DC but they're fine for
    # a one-shot pick if the admin really wants them.
    eligible_statuses = [
        BeatmapRankStatus.RANKED,
        BeatmapRankStatus.APPROVED,
        BeatmapRankStatus.QUALIFIED,
        BeatmapRankStatus.LOVED,
    ]

    # Ruleset_id -> GameMode mapping. We only support the four canonical
    # rulesets (0..3) here on purpose: the Torii alt-modes
    # (osurx / osuap / taikorx / fruitsrx) are not separate beatmap
    # categories — they're ways to *play* the same osu / taiko / catch
    # beatmap with RX or AP mod attached, and the underlying
    # `beatmaps.mode` column stores only the canonical ruleset. To set
    # up a "relax daily challenge" the admin picks ruleset 0 and adds
    # RX as a required mod via the new ModPicker. Anything outside 0..3
    # -> osu! so a mistyped admin payload never returns "no maps found".
    try:
        mode = [GameMode.OSU, GameMode.TAIKO, GameMode.FRUITS, GameMode.MANIA][req.ruleset_id]
    except IndexError:
        mode = GameMode.OSU

    wheres = [
        col(Beatmap.beatmap_status).in_(eligible_statuses),
        Beatmap.mode == mode,
    ]
    if req.min_difficulty is not None:
        wheres.append(Beatmap.difficulty_rating >= req.min_difficulty)
    if req.max_difficulty is not None:
        wheres.append(Beatmap.difficulty_rating <= req.max_difficulty)

    # Replaces the previous `ORDER BY rand() LIMIT 1` over the eligible
    # set, which on the prod database (~50k+ ranked beatmaps joined with
    # beatmapsets + failtime via SQLModel's auto-eager-load) blew the
    # MySQL sort buffer with `(1038, 'Out of sort memory, consider
    # increasing server sort buffer size')`. Constant-cost replacement:
    #   1) COUNT(*) over the same WHERE clause (index-friendly, single
    #      row aggregate)
    #   2) Pick a random offset in [0, count)
    #   3) LIMIT 1 OFFSET <offset> (index-friendly with the WHERE index)
    # Both queries also use noload/lazyload to suppress the auto-eager-
    # load — we don't need beatmapset on the COUNT, and we'll fetch it
    # by id below for the picked row only.
    eligible_count = (
        await session.exec(
            select(sa_func.count(col(Beatmap.id)))
            .where(*wheres)
        )
    ).one()
    if not eligible_count:
        raise HTTPException(
            status_code=404,
            detail=(
                "No eligible beatmaps matched the requested filters. "
                "Loosen the star range or pick a different ruleset."
            ),
        )

    random_offset = random.randint(0, eligible_count - 1)

    beatmap = (
        await session.exec(
            select(Beatmap)
            .options(noload(Beatmap.beatmapset), lazyload(Beatmap.failtimes))
            .where(*wheres)
            .order_by(col(Beatmap.id))
            .limit(1)
            .offset(random_offset)
        )
    ).first()

    if beatmap is None:
        # Race-condition guard: count > 0 but the offset row vanished
        # between the two queries (someone deleted a beatmap mid-pick).
        # Vanishingly rare; if it happens, returning 404 with the same
        # body the empty-set case uses lets the admin click Roll again.
        raise HTTPException(
            status_code=404,
            detail=(
                "No eligible beatmaps matched the requested filters. "
                "Loosen the star range or pick a different ruleset."
            ),
        )

    beatmapset = await session.get(Beatmapset, beatmap.beatmapset_id)
    preview = {
        "beatmap_id": beatmap.id,
        "beatmapset_id": beatmap.beatmapset_id,
        "version": beatmap.version,
        "difficulty_rating": beatmap.difficulty_rating,
        "mode": beatmap.mode,
        "total_length": beatmap.total_length,
        "bpm": beatmap.bpm,
        "title": beatmapset.title if beatmapset else None,
        "artist": beatmapset.artist if beatmapset else None,
        "creator": beatmapset.creator if beatmapset else None,
    }

    if not req.create_challenge:
        return {"created": False, "beatmap": preview}

    # ── Create branch ───────────────────────────────────────────────
    # Resolve date: explicit YYYY-MM-DD wins, otherwise default to
    # today (UTC, matching the cron scheduler at 00:00 UTC).
    if req.date:
        try:
            challenge_date = datetime.strptime(req.date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        challenge_date = utcnow().date()

    # Bail early on duplicates so we don't accidentally clobber a row
    # the cron job already inserted for the same day.
    existing = (
        await session.exec(select(DailyChallenge).where(col(DailyChallenge.date) == challenge_date))
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A daily challenge already exists for {challenge_date.isoformat()}.",
        )

    # C1 audit fix: normalise mods through _parse_mods_raw so the random
    # pick endpoint stores the same canonical APIMod[] shape that the
    # create endpoint does. Beatmap.mode is already known to match
    # ruleset_id (we filtered by it above), so we pass it through here
    # too — both invariants are now enforced in one place.
    required_mods_validated = _parse_mods_raw(
        req.required_mods, ruleset_id=req.ruleset_id, field_name="required_mods"
    )
    allowed_mods_validated = _parse_mods_raw(
        req.allowed_mods, ruleset_id=req.ruleset_id, field_name="allowed_mods"
    )
    challenge = DailyChallenge(
        beatmap_id=beatmap.id,
        ruleset_id=req.ruleset_id,
        required_mods=json.dumps(required_mods_validated),
        allowed_mods=json.dumps(allowed_mods_validated),
        date=challenge_date,
    )
    session.add(challenge)
    await session.commit()
    await session.refresh(challenge)

    return {
        "created": True,
        "date": challenge_date.isoformat(),
        "beatmap": preview,
    }


# ──────────────────────────────────────────────────────────────────────────
# Recalculation queue (admin-triggered per-user PP recalcs)
#
# The actual queue + subprocess management lives in
# app/service/recalculation_service.py — these endpoints are thin
# wrappers that gate on is_admin and hand off to the service.
# ──────────────────────────────────────────────────────────────────────────


@router.post(
    "/admin/recalculate/user/{user_id}",
    name="Queue User Recalculation",
    tags=["Admin", "g0v0 API"],
)
async def enqueue_user_pp_recalc(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Queue a single-user PP recalc. Returns immediately with the task
    record (status=pending) — the subprocess runs async. Poll
    /admin/recalculate/status to see when it's done."""
    actor = await require_admin(session, user_and_token)
    actor_username = actor.username

    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"User id {user_id} not found")
    target_username = target.username

    from app.service.recalculation_service import enqueue_user_recalc
    task = await enqueue_user_recalc(
        target_user_id=user_id,
        target_username=target_username,
        actor_username=actor_username,
    )
    return task.to_dict()


@router.get(
    "/admin/recalculate/status",
    name="Recalculation Queue Status",
    tags=["Admin", "g0v0 API"],
)
async def get_recalculation_status(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """Snapshot of the recalc queue: currently-running job (if any),
    pending queue, recent completed history (last 25)."""
    await require_admin(session, user_and_token)
    from app.service.recalculation_service import get_status
    return await get_status()


# ──────────────────────────────────────────────────────────────────────────
# Maintenance mode (server-wide score-submission gate)
#
# Three endpoints:
#   GET    /admin/maintenance  -> current state (admin viewer)
#   POST   /admin/maintenance  -> enable, with optional message body
#   DELETE /admin/maintenance  -> disable
#
# Score submission gating happens in app/router/v2/score.py — we only
# expose the *toggle* here. The state itself lives in Redis (see
# app/service/maintenance_mode.py) so the flag is shared across every
# uvicorn worker without DB round-trips.
#
# Self-lockout posture: maintenance does NOT block authentication or
# admin endpoints, so an admin who just enabled maintenance can always
# log back in and disable it. We deliberately did NOT implement the
# upstream "cannot self-disable" rule — see the module docstring on
# maintenance_mode.py for the rationale.
# ──────────────────────────────────────────────────────────────────────────


class _MaintenanceEnableBody(BaseModel):
    message: str | None = None


@router.get(
    "/admin/maintenance",
    name="Maintenance Mode Status",
    tags=["Admin", "g0v0 API"],
)
async def get_maintenance_state_endpoint(
    session: Database,
    redis: Redis,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await require_admin(session, user_and_token)
    from app.service.maintenance_mode import get_state, to_admin_dict
    state = await get_state(redis)
    return to_admin_dict(state)


@router.post(
    "/admin/maintenance",
    name="Enable Maintenance Mode",
    tags=["Admin", "g0v0 API"],
)
async def enable_maintenance_endpoint(
    session: Database,
    redis: Redis,
    body: _MaintenanceEnableBody,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    actor = await require_admin(session, user_and_token)
    # Snapshot the actor's username here, before any commit / refresh
    # could expire the loaded attribute (same lazy-load pattern that
    # bit us in update_user). The maintenance toggle itself doesn't
    # write to the SQL session, but capture defensively anyway.
    actor_username = actor.username
    actor_id = actor.id
    from app.service.maintenance_mode import enable, to_admin_dict
    state = await enable(
        redis,
        message=body.message,
        actor_user_id=actor_id,
        actor_username=actor_username,
    )
    return to_admin_dict(state)


@router.delete(
    "/admin/maintenance",
    name="Disable Maintenance Mode",
    tags=["Admin", "g0v0 API"],
)
async def disable_maintenance_endpoint(
    session: Database,
    redis: Redis,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    await require_admin(session, user_and_token)
    from app.service.maintenance_mode import disable, to_admin_dict
    state = await disable(redis)
    return to_admin_dict(state)


# ──────────────────────────────────────────────────────────────────────────
# Manual score submission (admin recovery tool)
#
# Honour a play whose live submission was lost (missed window, transient
# lookup failure, etc.) by uploading the player's .osr. /preview is a
# dry-run — parse + resolve user/beatmap, NO writes — that backs the
# confirm step in the admin UI; /commit does the real insert via the same
# process_score(...) the live POST handler uses. Both admin-gated + audited.
#
# Wraps app/service/manual_submit.py (the canonical server path; the CLI
# tools/submit_replay.py is the older offline equivalent).
# ──────────────────────────────────────────────────────────────────────────


@router.post(
    "/admin/manual-submit/preview",
    name="Manual Score Submission Preview",
    tags=["Admin", "g0v0 API"],
)
async def manual_submit_preview_endpoint(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    replay: bytes = File(...),
    user_id: int | None = Form(None),
):
    await require_admin(session, user_and_token)
    from app.service.manual_submit import ReplayParseError, preview
    try:
        return await preview(session, replay, user_id)
    except ReplayParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/admin/manual-submit/commit",
    name="Manual Score Submission",
    tags=["Admin", "g0v0 API"],
)
async def manual_submit_commit_endpoint(
    session: Database,
    redis: Redis,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    replay: bytes = File(...),
    user_id: int | None = Form(None),
):
    admin = await require_admin(session, user_and_token)
    admin_username = admin.username
    admin_id = admin.id
    from app.dependencies.fetcher import get_fetcher
    from app.service.manual_submit import ReplayParseError, commit
    try:
        fetcher = await get_fetcher()
        result = await commit(session, replay, user_id, redis, fetcher)
    except (ReplayParseError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info(
        f"admin {admin_username}#{admin_id} manually submitted score {result['score_id']} "
        f"for user {result['user_id']} ({result['username']}) on beatmap {result['beatmap_id']}"
    )
    return result


# ──────────────────────────────────────────────────────────────────────────
# Mods catalog
#
# Exposes the in-memory `API_MODS` dict (populated from static/mods.json
# at server startup) to the admin web UI. The frontend's daily-challenge
# composer uses this to render its mod picker dynamically: every mod the
# server knows about (acronym, name, type, settings schema, incompat
# list, multiplayer validity flags, etc.) is surfaced for selection,
# including Torii-specific additions like PA. Without this endpoint the
# admin web had to keep its own hard-coded list, which drifted every
# time mods.json changed and never picked up new additions.
# ──────────────────────────────────────────────────────────────────────────


@router.get(
    "/admin/mods-catalog",
    name="Admin Mod Catalog",
    tags=["Admin", "g0v0 API"],
)
async def get_mods_catalog(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
) -> dict[str, Any]:
    """Return the full per-ruleset mod catalog.

    Shape mirrors the in-memory `API_MODS` dict — keyed by ruleset id
    (string for JSON-friendliness) → mapping of acronym → mod definition
    object (Acronym, Name, Description, Type, Settings, IncompatibleMods,
    RequiresConfiguration, UserPlayable, ValidForMultiplayer,
    ValidForFreestyleAsRequiredMod, ValidForMultiplayerAsFreeMod,
    AlwaysValidForSubmission).

    Restricted to admins for now because the only consumer is the admin
    composer; nothing here is sensitive but there's no public surface
    that needs it yet either.
    """
    await require_admin(session, user_and_token)

    # API_MODS dict keys are integers (RulesetID) per the static/mods.json
    # convention. JSON keys must be strings — stringify on the way out so
    # consumers can deserialise without surprises.
    return {
        "rulesets": {str(rid): mods for rid, mods in API_MODS.items()},
    }


# ========== Username Change Requests ==========


class AdminUsernameChangeRequestResp(BaseModel):
    id: int
    user_id: int
    username: str | None
    avatar_url: str | None
    current_username: str
    requested_username: str
    status: str
    reject_reason: str | None
    created_at: datetime
    reviewed_at: datetime | None
    reviewed_by_id: int | None


class AdminUsernameChangeRequestListResp(BaseModel):
    total: int
    page: int
    per_page: int
    requests: list[AdminUsernameChangeRequestResp]


class RejectUsernameChangeReq(BaseModel):
    reason: str | None = None


def _ucr_to_admin_resp(
    request: UsernameChangeRequest,
    username: str | None,
    avatar_url: str | None,
) -> AdminUsernameChangeRequestResp:
    return AdminUsernameChangeRequestResp(
        id=request.id or 0,
        user_id=request.user_id,
        username=username,
        avatar_url=avatar_url,
        current_username=request.current_username,
        requested_username=request.requested_username,
        status=request.status,
        reject_reason=request.reject_reason,
        created_at=request.created_at,
        reviewed_at=request.reviewed_at,
        reviewed_by_id=request.reviewed_by_id,
    )


@router.get(
    "/admin/username-change-requests",
    name="Get Username Change Request List",
    tags=["Admin", "g0v0 API"],
    response_model=AdminUsernameChangeRequestListResp,
)
async def list_username_change_requests(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    status: str = Query("pending"),
    search: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    await require_admin(session, user_and_token)

    conditions = []
    status_value = status.strip().lower()
    if status_value and status_value != "all":
        conditions.append(col(UsernameChangeRequest.status) == status_value)

    search_value = search.strip()
    if search_value:
        conditions.append(
            sql_or(
                col(UsernameChangeRequest.current_username).ilike(f"%{search_value}%"),
                col(UsernameChangeRequest.requested_username).ilike(f"%{search_value}%"),
            )
        )

    count_stmt = select(func.count()).select_from(UsernameChangeRequest)
    data_stmt = select(UsernameChangeRequest)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        data_stmt = data_stmt.where(*conditions)

    total = (await session.exec(count_stmt)).one()
    rows = (
        await session.exec(
            data_stmt.order_by(col(UsernameChangeRequest.created_at).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    user_ids = sorted({row.user_id for row in rows})
    user_map: dict[int, tuple[str | None, str | None]] = {}
    if user_ids:
        users = (
            await session.exec(
                select(User.id, User.username, User.avatar_url).where(col(User.id).in_(user_ids))
            )
        ).all()
        user_map = {uid: (uname, avatar) for uid, uname, avatar in users}

    requests = [
        _ucr_to_admin_resp(row, *user_map.get(row.user_id, (None, None)))
        for row in rows
    ]
    return AdminUsernameChangeRequestListResp(total=total, page=page, per_page=per_page, requests=requests)


@router.post(
    "/admin/username-change-requests/{request_id}/approve",
    name="Approve Username Change Request",
    tags=["Admin", "g0v0 API"],
    response_model=AdminUsernameChangeRequestResp,
)
async def approve_username_change_request(
    session: Database,
    request_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    cache_service: UserCacheService,
):
    admin = await require_admin(session, user_and_token)

    request = await session.get(UsernameChangeRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if request.status != UCR_PENDING:
        raise HTTPException(status_code=409, detail="Request already reviewed")

    user = await session.get(User, request.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_name = request.requested_username
    # Re-validate at approval time: the name may have been taken or changed
    # banned-list status since the request was submitted.
    errors = validate_username(new_name)
    if errors:
        raise HTTPException(status_code=409, detail="\n".join(errors))
    taken = (
        await session.exec(
            select(User.id).where(col(User.username) == new_name, col(User.id) != user.id)
        )
    ).first()
    if taken is not None:
        raise HTTPException(status_code=409, detail="Username is already in use")

    old_username = user.username
    previous = list(await user.awaitable_attrs.previous_usernames)
    # Dedup: don't pile up the same old name twice (this is what produced the
    # duplicated "Torii User 297" entries some profiles accumulated).
    if old_username not in previous:
        previous.append(old_username)
    user.username = new_name
    user.previous_usernames = previous

    rename_event = Event(
        created_at=utcnow(),
        type=EventType.USERNAME_CHANGE,
        user_id=user.id,
    )
    rename_event.event_payload["user"] = {
        "username": new_name,
        "url": settings.web_url + "users/" + str(user.id),
        "previous_username": old_username,
    }
    session.add(rename_event)

    request.status = UCR_APPROVED
    request.reviewed_at = utcnow()
    request.reviewed_by_id = admin.id
    session.add(request)

    # Snapshot antes del commit: con expire_on_commit en True, leer user.id despues
    # dispara IO lazy y revienta.
    approved_user_id = user.id

    await cache_service.invalidate_user_cache(user.id)
    await session.commit()
    await session.refresh(request)
    await session.refresh(user)

    try:
        announcement = GlobalAnnouncement.init(
            source_user_id=admin.id,
            title="Username change approved",
            message=f"Your username change to '{new_name}' has been approved.",
            severity="info",
            receiver_ids=[user.id],
        )
        await server.new_private_notification(announcement)
    except Exception as e:
        logger.debug(f"Failed to notify user {user.id} about approved rename: {e}")

    # La respuesta se arma ANTES del PM: mandarlo commitea, y eso deja expirados a
    # request y a user, que son justo lo que lee _ucr_to_admin_resp.
    resp = _ucr_to_admin_resp(request, new_name, user.avatar_url)

    # Ademas de la notificacion in-game, que solo la ve quien esta conectado en ese
    # momento, le dejamos un PM: queda en el canal y lo lee cuando entre.
    try:
        from app.service.torii_welcome import send_bot_pm

        await send_bot_pm(
            session,
            approved_user_id,
            f"Your username change to '{new_name}' was approved. "
            f"You were {old_username} and you're now {new_name}.",
        )
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.debug(f"Failed to PM user {approved_user_id} about approved rename: {e}")

    return resp


@router.post(
    "/admin/username-change-requests/{request_id}/reject",
    name="Reject Username Change Request",
    tags=["Admin", "g0v0 API"],
    response_model=AdminUsernameChangeRequestResp,
)
async def reject_username_change_request(
    session: Database,
    request_id: int,
    req: RejectUsernameChangeReq,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    admin = await require_admin(session, user_and_token)

    request = await session.get(UsernameChangeRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if request.status != UCR_PENDING:
        raise HTTPException(status_code=409, detail="Request already reviewed")

    reason = (req.reason or "").strip() or None
    # Snapshot antes del commit, mismo motivo que en approve.
    rejected_user_id = request.user_id
    request.status = UCR_REJECTED
    request.reject_reason = reason
    request.reviewed_at = utcnow()
    request.reviewed_by_id = admin.id
    session.add(request)
    await session.commit()
    await session.refresh(request)

    user = await session.get(User, request.user_id)
    try:
        message = f"Your username change to '{request.requested_username}' was rejected."
        if reason:
            message += f" Reason: {reason}"
        announcement = GlobalAnnouncement.init(
            source_user_id=admin.id,
            title="Username change rejected",
            message=message,
            severity="warning",
            receiver_ids=[request.user_id],
        )
        await server.new_private_notification(announcement)
    except Exception as e:
        logger.debug(f"Failed to notify user {request.user_id} about rejected rename: {e}")

    # Igual que en approve: la respuesta primero, el PM despues.
    resp = _ucr_to_admin_resp(request, user.username if user else None, user.avatar_url if user else None)

    # Aca el PM importa mas que en approve, porque lleva el motivo del rechazo y la
    # notificacion in-game se pierde si la persona no estaba conectada.
    try:
        from app.service.torii_welcome import send_bot_pm

        texto = f"Your username change to '{request.requested_username}' was rejected."
        if reason:
            texto += f" Reason: {reason}"

        await send_bot_pm(session, rejected_user_id, texto)
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.debug(f"Failed to PM user {rejected_user_id} about rejected rename: {e}")

    return resp


# ========== Previous Usernames (admin edit) ==========


class PreviousUsernamesResp(BaseModel):
    user_id: int
    username: str
    avatar_url: str | None
    previous_usernames: list[str]


class RemovePreviousUsernamesReq(BaseModel):
    names: list[str]


@router.get(
    "/admin/users/{user_id}/previous-usernames",
    name="Get Previous Usernames",
    tags=["Admin", "g0v0 API"],
    response_model=PreviousUsernamesResp,
)
async def get_previous_usernames(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
):
    """List a user's stored previous usernames (the profile 'formerly known as'). Admin only."""
    await require_admin(session, user_and_token)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    names = list(await user.awaitable_attrs.previous_usernames)
    return PreviousUsernamesResp(
        user_id=user.id,
        username=user.username,
        avatar_url=user.avatar_url,
        previous_usernames=names,
    )


@router.post(
    "/admin/users/{user_id}/previous-usernames/remove",
    name="Remove Previous Username",
    tags=["Admin", "g0v0 API"],
    response_model=PreviousUsernamesResp,
)
async def remove_previous_usernames(
    session: Database,
    user_id: int,
    req: RemovePreviousUsernamesReq,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    cache_service: UserCacheService,
):
    """Remove one or more specific names from a user's previous-usernames list. Admin only.

    Missing names are ignored, so the call is idempotent. Removing a name that
    appears more than once (e.g. a duplicated default name) drops every copy.
    """
    await require_admin(session, user_and_token)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Read the loaded columns before commit; expire_on_commit would expire them.
    username = user.username
    avatar_url = user.avatar_url
    to_remove = set(req.names)
    updated = [n for n in list(await user.awaitable_attrs.previous_usernames) if n not in to_remove]
    user.previous_usernames = updated
    session.add(user)
    # previous_usernames is part of the cached profile payload, so the edit
    # won't show until the user's profile cache is dropped.
    await cache_service.invalidate_user_cache(user_id)
    await session.commit()
    return PreviousUsernamesResp(
        user_id=user_id,
        username=username,
        avatar_url=avatar_url,
        previous_usernames=updated,
    )


@router.delete(
    "/admin/users/{user_id}/previous-usernames",
    name="Clear Previous Usernames",
    tags=["Admin", "g0v0 API"],
    response_model=PreviousUsernamesResp,
)
async def clear_previous_usernames(
    session: Database,
    user_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    cache_service: UserCacheService,
):
    """Clear ALL of a user's previous usernames (wipes the 'formerly known as'). Admin only."""
    await require_admin(session, user_and_token)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    username = user.username
    avatar_url = user.avatar_url
    user.previous_usernames = []
    session.add(user)
    await cache_service.invalidate_user_cache(user_id)
    await session.commit()
    return PreviousUsernamesResp(
        user_id=user_id,
        username=username,
        avatar_url=avatar_url,
        previous_usernames=[],
    )


# ========== NSFW Profile Media Review ==========


class AdminProfileMediaReviewResp(BaseModel):
    id: int
    user_id: int
    username: str | None
    media_type: str
    url: str
    status: str
    is_current: bool
    created_at: datetime
    reviewed_at: datetime | None


class AdminProfileMediaReviewListResp(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[AdminProfileMediaReviewResp]


def _pmr_to_admin_resp(review: ProfileMediaReview, username: str | None) -> AdminProfileMediaReviewResp:
    return AdminProfileMediaReviewResp(
        id=review.id or 0,
        user_id=review.user_id,
        username=username,
        media_type=review.media_type,
        url=review.url,
        status=review.status,
        is_current=review.is_current,
        created_at=review.created_at,
        reviewed_at=review.reviewed_at,
    )


@router.get(
    "/admin/profile-media-reviews",
    name="Get Pending NSFW Profile Media List",
    tags=["Admin", "g0v0 API"],
    response_model=AdminProfileMediaReviewListResp,
)
async def list_profile_media_reviews(
    session: Database,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    status: str = Query("pending"),
    media_type: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    await require_admin(session, user_and_token)

    conditions = []
    status_value = status.strip().lower()
    if status_value and status_value != "all":
        conditions.append(col(ProfileMediaReview.status) == status_value)

    media_value = media_type.strip().lower()
    if media_value in (MEDIA_AVATAR, MEDIA_COVER):
        conditions.append(col(ProfileMediaReview.media_type) == media_value)

    count_stmt = select(func.count()).select_from(ProfileMediaReview)
    data_stmt = select(ProfileMediaReview)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        data_stmt = data_stmt.where(*conditions)

    total = (await session.exec(count_stmt)).one()
    rows = (
        await session.exec(
            data_stmt.order_by(col(ProfileMediaReview.created_at).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).all()

    user_ids = sorted({row.user_id for row in rows})
    username_map: dict[int, str] = {}
    if user_ids:
        users = (
            await session.exec(select(User.id, User.username).where(col(User.id).in_(user_ids)))
        ).all()
        username_map = {uid: uname for uid, uname in users}

    items = [_pmr_to_admin_resp(row, username_map.get(row.user_id)) for row in rows]
    return AdminProfileMediaReviewListResp(total=total, page=page, per_page=per_page, items=items)


@router.post(
    "/admin/profile-media-reviews/{review_id}/revoke",
    name="Takedown NSFW Profile Media",
    tags=["Admin", "g0v0 API"],
    response_model=AdminProfileMediaReviewResp,
)
async def revoke_profile_media(
    session: Database,
    review_id: int,
    user_and_token: Annotated[UserAndToken, Security(get_client_user_and_token)],
    storage: StorageService,
    cache_service: UserCacheService,
):
    admin = await require_admin(session, user_and_token)

    review = await session.get(ProfileMediaReview, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")
    if review.status == PMR_REVOKED:
        raise HTTPException(status_code=409, detail="Already revoked")

    user = await session.get(User, review.user_id)
    if user is not None:
        if review.media_type == MEDIA_AVATAR:
            user.avatar_url = User.DEFAULT_AVATAR_URL
            user.avatar_nsfw = False
        else:
            user.cover = UserProfileCover(url=User.DEFAULT_COVER_URL)
            user.cover_nsfw = False
        session.add(user)

    # Remove the offending file from storage (best-effort).
    path = review.storage_path
    if not path and review.url:
        path = storage.get_file_name_by_url(review.url)
    if path:
        try:
            await storage.delete_file(path)
        except Exception as e:
            logger.debug(f"Failed to delete revoked media file {path}: {e}")

    review.status = PMR_REVOKED
    review.is_current = False
    review.reviewed_at = utcnow()
    review.reviewed_by_id = admin.id
    session.add(review)

    if user is not None:
        await cache_service.invalidate_user_cache(user.id)
    await session.commit()
    await session.refresh(review)

    return _pmr_to_admin_resp(review, user.username if user else None)

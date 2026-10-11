from datetime import UTC, date
import time
from typing import Annotated
import asyncio

from app.calculator import clamp
from app.config import settings
from app.database import (
    Beatmap,
    Playlist,
    Room,
    Score,
    ScoreToken,
    ScoreTokenResp,
    User,
)
from app.database.total_score_best_scores import TotalScoreBestScore
from app.database.user import UserModel
from app.database.achievement import process_achievements
from app.database.counts import ReplayWatchedCount
from app.database.daily_challenge import process_daily_challenge_score
from app.database.item_attempts_count import ItemAttemptsCount
from app.database.playlist_best_score import (
    PlaylistBestScore,
    get_position,
    process_playlist_best_score,
)
from app.database.relationship import Relationship, RelationshipType
from app.database.score import (
    LegacyScoreResp,
    MultiplayerScores,
    MultiplayScoreDict,
    ScoreModel,
    get_leaderboard,
    get_score_position_by_id,
    process_score,
    process_user,
)
from app.dependencies.api_version import APIVersion
from app.dependencies.cache import UserCacheService
from app.dependencies.client_verification import ClientVerificationService
from app.dependencies.database import Database, Redis, get_redis, with_db
from app.dependencies.fetcher import Fetcher, get_fetcher
from app.dependencies.storage import StorageService
from app.dependencies.user import ClientUser, get_current_user
from app.log import log
from app.models.beatmap import BeatmapRankStatus
from app.models.room import RoomCategory
from app.models.score import (
    GameMode,
    LeaderboardType,
    Rank,
    SoloScoreSubmissionInfo,
)
from app.models.version import VersionCheckResult
from app.service.beatmap_cache_service import get_beatmap_cache_service
from app.service.login_log_service import LoginLogService
from app.service.pp_variant_service import (
    apply_pp_variant_to_score_responses,
    get_score_pp_variant_batch,
    normalize_pp_variant,
)
from app.service.user_cache_service import refresh_user_cache_background
from app.utils import api_doc, utcnow

from .router import router

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Security,
)
from fastapi.responses import RedirectResponse
from fastapi_limiter.depends import RateLimiter
from httpx import HTTPError
from pydantic import BaseModel
from sqlalchemy.orm import joinedload
from sqlmodel import col, exists, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

READ_SCORE_TIMEOUT = 10
# torii: el aura + name colour + grupos del jugador viajan en cada score para que el leaderboard de
# song-select (y el score detail) muestren el cosmetico de CUALQUIER jugador, no solo el local.
_COSMETIC_USER_INCLUDES = ["user.equipped_aura", "user.equipped_name_colour", "user.groups"]
DEFAULT_SCORE_INCLUDES = ["user", "user.country", "user.cover", "user.team", *_COSMETIC_USER_INCLUDES]
SCORE_DETAIL_INCLUDES = [
    "beatmap",
    "beatmapset",
    "position",
    "rank_global",
    "rank_country",
    "user",
    "user.country",
    "user.cover",
    "user.team",
    *_COSMETIC_USER_INCLUDES,
]
logger = log("Score")
LAST_CLIENT_HASH_KEY = "metadata:user:last_client_hash:{user_id}"


async def _store_last_client_hash(redis: Redis, user_id: int, version_hash: str) -> None:
    hash_value = (version_hash or "").strip().lower()
    if not hash_value:
        return
    try:
        await redis.set(
            LAST_CLIENT_HASH_KEY.format(user_id=user_id),
            hash_value,
            ex=60 * 60 * 24 * 120,  # 120 days
        )
    except Exception as e:
        logger.debug(f"Failed to store last client hash for user {user_id}: {e}")


def _format_client_version_label(client_version_result: VersionCheckResult) -> str | None:
    name = (client_version_result.client_name or "").strip()
    version = (client_version_result.version or "").strip()
    os_name = (client_version_result.os or "").strip()
    if not any((name, version, os_name)):
        return None
    base = " ".join(part for part in (name, version) if part).strip()
    if os_name:
        return f"{base} ({os_name})" if base else os_name
    return base or None


def _extract_user_agent_client_hint(user_agent: str) -> str | None:
    ua = (user_agent or "").strip()
    if not ua:
        return None

    lower = ua.lower()
    compact = "".join(lower.split())
    # Generic client UAs have no useful build information.
    if compact in {"osu!", "osu", "osu!lazer", "lazer"}:
        return None

    # Skip common browser UAs to avoid polluting DB with irrelevant long strings.
    if "mozilla/" in lower and all(marker not in lower for marker in ("osu", "tachyon", "shigetiro")):
        return None

    return ua[:180]


def _derive_client_version_label(
    client_version_result: VersionCheckResult,
    request: Request,
    version_hash: str,
) -> str | None:
    mapped = _format_client_version_label(client_version_result)

    user_agent_hint = _extract_user_agent_client_hint(request.headers.get("user-agent") or "")
    hash_value = (version_hash or "").strip()

    # If verification only produced a generic name (e.g. "osu!") and no explicit
    # version, prefer a richer client hint from user-agent when available.
    has_explicit_version = bool((client_version_result.version or "").strip())
    if mapped and has_explicit_version:
        return mapped
    if mapped and user_agent_hint and user_agent_hint.lower() != mapped.lower():
        return user_agent_hint
    if mapped and hash_value:
        return f"{mapped} (hash:{hash_value[:12]})"
    if mapped:
        return mapped
    if user_agent_hint:
        return user_agent_hint

    if hash_value:
        return f"hash:{hash_value[:20]}"

    return None


async def _process_user_achievement(score_id: int):
    async with with_db() as session:
        await process_achievements(session, get_redis(), score_id)


async def _process_user(score_id: int, user_id: int, redis: Redis, fetcher: Fetcher):
    # Use expire_on_commit=False here. process_user calls session.commit()
    # multiple times during stat / pp / leaderboard processing, and the
    # default behaviour expires every loaded attribute on managed objects.
    # Subsequent attribute reads (e.g. score.total_score, user.username)
    # then trigger a synchronous lazy-load, which crashes with
    # `greenlet_spawn has not been called` because we're not inside a
    # greenlet when accessed. Disabling expiration keeps the in-memory
    # attributes valid after commit and avoids the unnecessary refetch.
    from app.dependencies.database import engine as _engine
    from app.dependencies.database import release_session as _release_session
    from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

    # Explicit try/finally instead of `async with`. _process_user is awaited INLINE
    # in the submit-score request (and also scheduled as a background task). If the
    # osu! client disconnects while a slow pp calc runs the task gets cancelled, and
    # a bare `async with` would have its __aexit__ close() cancelled too, leaking the
    # connection idle-in-transaction with its row locks held. That is exactly what
    # produced the 2026-06 "Lock wait timeout" that blocked admin bans. release_session
    # shields rollback+close so the connection is always returned to the pool.
    session = _AsyncSession(_engine, expire_on_commit=False)
    try:
        user = await session.get(User, user_id)
        if not user:
            logger.warning(
                "User {user_id} not found when processing score {score_id}", user_id=user_id, score_id=score_id
            )
            return
        score = await session.get(Score, score_id)
        if not score:
            logger.warning(
                "Score {score_id} not found when processing user {user_id}", score_id=score_id, user_id=user_id
            )
            return
        score_token = (await session.exec(select(ScoreToken.id).where(ScoreToken.score_id == score_id))).first()
        if not score_token:
            logger.warning(
                "ScoreToken for score {score_id} not found when processing user {user_id}",
                score_id=score_id,
                user_id=user_id,
            )
            return
        beatmap = (
            await session.exec(
                select(Beatmap.total_length, Beatmap.beatmap_status).where(Beatmap.id == score.beatmap_id)
            )
        ).first()
        if not beatmap:
            logger.warning(
                "Beatmap {beatmap_id} not found when processing user {user_id} for score {score_id}",
                beatmap_id=score.beatmap_id,
                user_id=user_id,
                score_id=score_id,
            )
            return
        await process_user(session, redis, fetcher, user, score, score_token, beatmap[0], BeatmapRankStatus(beatmap[1]))
    finally:
        await _release_session(session)


async def _process_user_background(score_id: int, user_id: int, redis: Redis, fetcher: Fetcher):
    t0 = time.time()
    try:
        await _process_user(score_id, user_id, redis, fetcher)
        logger.info(
            "[submit_score] background _process_user finished in {:.3f}s score_id={} user_id={}",
            time.time() - t0,
            score_id,
            user_id,
        )
    except Exception as e:
        logger.warning(
            "[submit_score] background _process_user FAILED in {:.3f}s score_id={} user_id={} err={}",
            time.time() - t0,
            score_id,
            user_id,
            e,
        )


async def submit_score(
    background_task: BackgroundTasks,
    info: SoloScoreSubmissionInfo,
    token: int,
    current_user: User,
    db: AsyncSession,
    redis: Redis,
    fetcher: Fetcher,
):
    # Fetch user ID immediately to avoid subsequent lazy-loading issues
    user_id = current_user.id

    logger.info(
        "[submit_score] start user_id={} token={} passed={} rank={}",
        user_id,
        token,
        info.passed,
        getattr(info, "rank", None),
    )

    if await current_user.is_restricted(db):
        raise HTTPException(
            status_code=403,
            detail="You are restricted from logging in or submitting scores on Torii.",
        )

    # Maintenance-mode gate. Admins are exempt so an admin can verify
    # the server is healthy mid-maintenance without flipping the toggle
    # off; everyone else gets a 503 with the operator-supplied message
    # so the client UI can show *why* their submit was rejected. We
    # check the flag against Redis here (single HGET, sub-millisecond)
    # rather than storing it in a column anyone has to refresh.
    if not getattr(current_user, "is_admin", False):
        from app.service.maintenance_mode import get_state
        m_state = await get_state(redis)
        if m_state.enabled:
            raise HTTPException(
                status_code=503,
                detail=m_state.message
                or "Score submission is temporarily disabled for maintenance.",
            )

    if not info.passed:
        info.rank = Rank.F

    score_token = (
        await db.exec(select(ScoreToken).options(joinedload(ScoreToken.beatmap)).where(ScoreToken.id == token))
    ).first()
    if not score_token or score_token.user_id != user_id:
        raise HTTPException(status_code=404, detail="Score token not found")

    if score_token.score_id:
        score = (
            await db.exec(
                select(Score).where(
                    Score.id == score_token.score_id,
                    Score.user_id == user_id,
                )
            )
        ).first()
        if not score:
            raise HTTPException(status_code=404, detail="Score not found")
    else:
        # Distributed lock: prevents two concurrent requests for the same token
        # from both seeing score_token.score_id=None and creating duplicate scores.
        lock_key = f"score:submit:lock:{token}"
        lock_acquired = await redis.set(lock_key, "1", nx=True, ex=30)

        if not lock_acquired:
            # Another request is already processing this token.
            # Wait briefly and re-check — the other request should have committed
            # score_token.score_id atomically inside process_score by now.
            await asyncio.sleep(0.5)
            await db.refresh(score_token)
            if not score_token.score_id:
                raise HTTPException(status_code=409, detail="Score submission already in progress")
            score = (
                await db.exec(
                    select(Score).where(
                        Score.id == score_token.score_id,
                        Score.user_id == user_id,
                    )
                )
            ).first()
            if not score:
                raise HTTPException(status_code=409, detail="Score not found after concurrent submission")
            logger.info(
                "[submit_score] concurrent request resolved via lock check, returning existing score_id={}",
                score_token.score_id,
            )
        else:
            try:
                beatmap = score_token.beatmap_id

                try:
                    cache_service = get_beatmap_cache_service(redis, fetcher)
                    await cache_service.smart_preload_for_score(beatmap)
                except Exception as e:
                    logger.debug(f"Beatmap preload failed for {beatmap}: {e}")

                try:
                    db_beatmap = await Beatmap.get_or_fetch(db, fetcher, bid=beatmap)
                except HTTPError:
                    raise HTTPException(status_code=404, detail="Beatmap not found")

                status = db_beatmap.beatmap_status

                t0 = time.time()
                logger.info("[submit_score] calling process_score user_id={} beatmap_id={} status={}", user_id, beatmap, status)

                score = await process_score(
                    current_user,
                    beatmap,
                    status.has_pp() or settings.enable_all_beatmap_pp,
                    score_token,
                    info,
                    db,
                )

                logger.info(
                    "[submit_score] process_score done in {:.3f}s score_id={} user_id={} beatmap_id={}",
                    time.time() - t0,
                    score.id,
                    user_id,
                    beatmap,
                )
            finally:
                # Release the lock now. score_token.score_id was committed atomically
                # inside process_score, so any subsequent request for this token will
                # find score_id set and return the existing score (no duplicate).
                await redis.delete(lock_key)

            score_id = score.id

            t_commit = time.time()
            # Refresh MySQL REPEATABLE READ snapshot so we see the committed state.
            await db.commit()
            await db.refresh(score)
            logger.info(
                "[submit_score] db commit+refresh done in {:.3f}s score_id={}",
                time.time() - t_commit,
                score_id,
            )

            # Failed / quit submissions do not need to block the client on
            # statistics and leaderboard housekeeping before returning.
            if score.passed:
                t_user = time.time()
                logger.info("[submit_score] BEFORE _process_user score_id={} user_id={}", score_id, user_id)
                _process_user_succeeded = False
                try:
                    await _process_user(score_id, user_id, redis, fetcher)
                    _process_user_succeeded = True
                    logger.info(
                        "[submit_score] AFTER _process_user in {:.3f}s score_id={} user_id={}",
                        time.time() - t_user,
                        score_id,
                        user_id,
                    )
                except Exception as e:
                    logger.warning(
                        "[submit_score] _process_user FAILED in {:.3f}s score_id={} user_id={} err={}",
                        time.time() - t_user,
                        score_id,
                        user_id,
                        e,
                    )

                # ─── Self-heal: verify the leaderboard row landed ───
                #
                # _process_user is supposed to write a TotalScoreBestScore
                # for this (user, beatmap) so the leaderboard query sees
                # the just-submitted score. Several failure modes have
                # historically been swallowed silently inside that path
                # (greenlet errors mid-commit, transient DB hiccups,
                # subscriber publishes that raise after the row had
                # already been queued for insert but before the commit
                # landed, ...) and the next leaderboard fetch from the
                # client returned an empty "Overall Ranking" panel.
                #
                # The verify is a sanity check — if it misses, we schedule
                # a background retry. We DO NOT re-run _process_user inline
                # here: the previous version did and it was doubling the
                # response latency for every single submission (the verify
                # query referenced a non-existent column, AttributeError
                # was swallowed into "row missing", retry path triggered
                # unconditionally, scoring screen "Overall Ranking" hung
                # while we ran process_user a second time). Background
                # task is the right safety net — it doesn't block the
                # client's score-result UI.
                #
                # Subtlety: _process_user runs in a DEDICATED session and
                # commits there. The outer `db` session has a stale MVCC
                # snapshot under MySQL REPEATABLE READ — a fresh SELECT
                # won't see the row even though it's committed. We commit
                # `db` first to refresh the snapshot, then verify.
                await db.commit()

                try:
                    leaderboard_row = (
                        await db.exec(
                            select(TotalScoreBestScore.score_id).where(
                                TotalScoreBestScore.score_id == score_id,
                            )
                        )
                    ).first()
                except Exception as verify_err:
                    leaderboard_row = None
                    logger.warning(
                        "[submit_score] verify-leaderboard query failed score_id={} err={}",
                        score_id,
                        verify_err,
                    )

                if not _process_user_succeeded:
                    # The inline call raised — schedule a background pass
                    # so the score eventually finishes processing without
                    # delaying the response.
                    logger.info(
                        "[submit_score] _process_user did not succeed for score_id={} "
                        "— scheduling background retry",
                        score_id,
                    )
                    background_task.add_task(
                        _process_user_background, score_id, user_id, redis, fetcher
                    )
                elif leaderboard_row is None:
                    # process_user claimed success and committed, but the
                    # row isn't visible. Either MVCC is still holding a
                    # stale snapshot (rare after the commit above, but
                    # possible across replication boundaries) or the row
                    # legitimately wasn't written (e.g. the score didn't
                    # improve the user's previous best on this beatmap,
                    # so no TotalScoreBestScore was created for this
                    # score_id). The latter is normal — DON'T block on
                    # a retry. Schedule a background pass as a cheap
                    # safety net for the rare bug case.
                    logger.info(
                        "[submit_score] no leaderboard row for score_id={} after "
                        "successful _process_user — likely a non-improving score; "
                        "scheduling background pass as safety net",
                        score_id,
                    )
                    background_task.add_task(
                        _process_user_background, score_id, user_id, redis, fetcher
                    )
            else:
                logger.info(
                    "[submit_score] scheduling background _process_user for failed score_id={} user_id={}",
                    score_id,
                    user_id,
                )
                background_task.add_task(_process_user_background, score_id, user_id, redis, fetcher)

    # _process_user runs in a dedicated session.
    # Commit before re-reading to avoid stale snapshots (e.g. MySQL REPEATABLE READ).
    # Use the cached score_id local instead of `score.id`: the verify-leaderboard
    # block above issues its own db.commit() to refresh the MVCC snapshot, and
    # the default expire_on_commit=True on this session expires every loaded
    # attribute on managed entities. Reading `score.id` here would then trigger
    # a sync lazy-load — which crashes with `greenlet_spawn has not been called`
    # in this async context and bubbles up as a 500. score_id was captured
    # earlier as a plain int, so it sidesteps the expired state entirely.
    response_score_id = score_id
    t_commit2 = time.time()
    await db.commit()
    logger.info(
        "[submit_score] pre-response db.commit done in {:.3f}s score_id={}",
        time.time() - t_commit2,
        response_score_id,
    )

    # Build response from final score object (after processing).
    await db.refresh(score)

    # Build response from final score object (after processing)
    t_resp = time.time()
    resp = await ScoreModel.transform(score)
    logger.info("[submit_score] ScoreModel.transform done in {:.3f}s score_id={}", time.time() - t_resp, resp["id"])

    score_gamemode = score.gamemode

    # Kick off cache refresh as a background task so the score submission response
    # is returned to the client immediately without waiting for pp-dev recalculation.
    # Previously this was awaited inline, causing 10-30s timeouts because pp-dev
    # calculations (2.5s each x many scores) blocked the response.
    if user_id is not None:
        background_task.add_task(refresh_user_cache_background, redis, user_id, score_gamemode)

    # Achievements can stay async
    background_task.add_task(_process_user_achievement, resp["id"])

    logger.info("[submit_score] END user_id={} score_id={}", user_id, resp["id"])
    return resp


async def _preload_beatmap_for_pp_calculation(beatmap_id: int) -> None:
    """
    Pre-cache beatmap file to speed up PP calculation
    Asynchronously preload raw beatmap file into Redis cache when player starts playing
    """
    # Check if beatmap preloading is enabled
    if not settings.enable_beatmap_preload:
        return

    try:
        # Asynchronously get fetcher and redis connection
        fetcher = await get_fetcher()
        redis = get_redis()

        # Check if already cached to avoid re-downloading
        cache_key = f"beatmap:raw:{beatmap_id}"
        if await redis.exists(cache_key):
            logger.debug(f"Beatmap {beatmap_id} already cached, skipping preload")
            return

        await fetcher.get_or_fetch_beatmap_raw(redis, beatmap_id)
        logger.debug(f"Successfully preloaded beatmap {beatmap_id} for PP calculation")

    except Exception as e:
        # Pre-cache failure should not affect normal gameplay flow
        logger.warning(f"Failed to preload beatmap {beatmap_id}: {e}")


LeaderboardScoreType = ScoreModel.generate_typeddict(tuple(DEFAULT_SCORE_INCLUDES)) | LegacyScoreResp
ScoreDetailType = ScoreModel.generate_typeddict(tuple(SCORE_DETAIL_INCLUDES)) | LegacyScoreResp


class BeatmapUserScore(BaseModel):
    position: int
    score: LeaderboardScoreType  # pyright: ignore[reportInvalidTypeForm]


class BeatmapScores(BaseModel):
    scores: list[LeaderboardScoreType]  # pyright: ignore[reportInvalidTypeForm]
    user_score: BeatmapUserScore | None = None
    score_count: int = 0


@router.get(
    "/scores/{score_id}",
    tags=["Scores"],
    responses={
        200: api_doc(
            "Get details of the specified score.",
            ScoreDetailType,
            SCORE_DETAIL_INCLUDES,
        )
    },
    name="Get Score Details",
    description="Get full score details by score ID (including player, beatmap, and leaderboard rank).",
)
async def get_score_by_id(
    db: Database,
    api_version: APIVersion,
    score_id: Annotated[int, Path(description="Score ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    legacy_only: Annotated[bool | None, Query(description="Whether to query stable scores only")] = None,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    score = (
        await db.exec(
            select(Score).where(
                Score.id == score_id,
                ~User.is_restricted_query(col(Score.user_id)),
            )
        )
    ).first()
    if not score:
        raise HTTPException(status_code=404, detail="Score not found")

    return await score.to_resp(
        db,
        api_version,
        includes=SCORE_DETAIL_INCLUDES,
        show_nsfw_media=show_nsfw_media,
    )


@router.get(
    "/beatmaps/{beatmap_id}/scores",
    tags=["Scores"],
    responses={
        200: {
            "model": BeatmapScores,
            "description": (
                "Leaderboard and current user scores.\n\n"
                f"If `x-api-version >= 20220705`, returns `BeatmapScores[Score]`"
                f" (includes: {', '.join([f'`{inc}`' for inc in DEFAULT_SCORE_INCLUDES])}),"
                "otherwise `BeatmapScores[LegacyScoreResp]`."
            ),
        }
    },
    name="Get Beatmap Leaderboard",
    description="Get leaderboard and current user scores for specified beatmap under specified conditions.",
)
async def get_beatmap_scores(
    db: Database,
    api_version: APIVersion,
    beatmap_id: Annotated[int, Path(description="Beatmap ID")],
    mode: Annotated[GameMode, Query(description="Specified ruleset")],
    mods: Annotated[list[str], Query(default_factory=set, alias="mods[]", description="Filter by mods (optional, multiple values)")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    fetcher: Annotated[Fetcher, Depends(get_fetcher)],
    legacy_only: Annotated[bool | None, Query(description="Whether to query stable scores only")] = None,
    type: Annotated[
        LeaderboardType,
        Query(
            description=("Leaderboard type: GLOBAL / COUNTRY / FRIENDS / TEAM"),
        ),
    ] = LeaderboardType.GLOBAL,
    limit: Annotated[int, Query(ge=1, le=200, description="Return count (1-200)")] = 50,
    pp_variant: Annotated[str | None, Query(description="pp variant: stable / pp_dev")] = None,
):
    # Ensure beatmap exists in local DB, fetch if missing
    try:
        await Beatmap.get_or_fetch(db, fetcher, bid=beatmap_id)
    except Exception as e:
        logger.warning(f"Failed to fetch beatmap {beatmap_id} for leaderboard: {e}")

    resolved_pp_variant = normalize_pp_variant(pp_variant)

    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    all_scores, user_score, count = await get_leaderboard(
        db,
        beatmap_id,
        mode,
        type=type,
        user=current_user,
        limit=limit,
        mods=sorted(mods),
    )

    # Build score responses
    all_score_resps = [
        await score.to_resp(
            db,
            api_version,
            includes=DEFAULT_SCORE_INCLUDES,
            show_nsfw_media=show_nsfw_media,
        )
        for score in all_scores
    ]

    user_score_resp = (
        await user_score.to_resp(
            db,
            api_version,
            includes=DEFAULT_SCORE_INCLUDES,
            show_nsfw_media=show_nsfw_media,
        )
        if user_score
        else None
    )

    # Apply pp-dev variant PP values if requested.
    # Scores are already sorted by stable PP from the leaderboard query;
    # pp-dev may reorder them visually but we leave the ordering as-is
    # since the leaderboard position is determined server-side by stable PP.
    if resolved_pp_variant == "pp_dev":
        all_scores_for_variant = list(all_scores)
        if user_score:
            all_scores_for_variant.append(user_score)
        all_resps_for_variant = list(all_score_resps)
        if user_score_resp:
            all_resps_for_variant.append(user_score_resp)

        pp_by_score_id = await get_score_pp_variant_batch(
            session=db,
            scores=all_scores_for_variant,
            pp_variant=resolved_pp_variant,
            redis=get_redis(),
            fetcher=fetcher,
            recalc_top_n=len(all_scores_for_variant),
        )
        apply_pp_variant_to_score_responses(
            scores=all_scores_for_variant,
            score_responses=all_resps_for_variant,
            pp_by_score_id=pp_by_score_id,
            add_weight=False,
        )

    return {
        "scores": all_score_resps,
        "user_score": (
            {
                "score": user_score_resp,
                "position": (
                    await get_score_position_by_id(
                        db,
                        user_score.beatmap_id,
                        user_score.id,
                        mode=user_score.gamemode,
                        user=user_score.user,
                    )
                    or 0
                ),
            }
            if user_score and user_score_resp
            else None
        ),
        "score_count": count,
    }


@router.get(
    "/beatmaps/{beatmap_id}/scores/users/{user_id}",
    tags=["Scores"],
    responses={
        200: {
            "model": BeatmapUserScore,
            "description": (
                "Highest score for specified user on specified beatmap\n\n"
                "If `x-api-version >= 20220705`, returns `BeatmapUserScore[Score]`, "
                f" (includes: {', '.join([f'`{inc}`' for inc in DEFAULT_SCORE_INCLUDES])}),"
                "otherwise `BeatmapUserScore[LegacyScoreResp]`."
            ),
        }
    },
    name="Get User Best Score on Beatmap",
    description="Get highest score for specified user on specified beatmap.",
)
async def get_user_beatmap_score(
    db: Database,
    api_version: APIVersion,
    beatmap_id: Annotated[int, Path(description="Beatmap ID")],
    user_id: Annotated[int, Path(description="User ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    legacy_only: Annotated[bool | None, Query(description="Whether to query stable scores only")] = None,
    mode: Annotated[GameMode | None, Query(description="Specified ruleset (optional)")] = None,
    mods: Annotated[str | None, Query(description="Filter by mods (not implemented yet)")] = None,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    user_score = (
        await db.exec(
            select(Score)
            .where(
                Score.gamemode == mode if mode is not None else True,
                Score.beatmap_id == beatmap_id,
                Score.user_id == user_id,
                col(Score.passed).is_(True),
            )
            .order_by(col(Score.total_score).desc())
            .limit(1)
        )
    ).first()

    if not user_score:
        raise HTTPException(
            status_code=404,
            detail=f"Cannot find user {user_id}'s score on this beatmap",
        )
    else:
        resp = await user_score.to_resp(
            db,
            api_version=api_version,
            includes=DEFAULT_SCORE_INCLUDES,
            show_nsfw_media=show_nsfw_media,
        )
        return {
            "position": (
                await get_score_position_by_id(
                    db,
                    user_score.beatmap_id,
                    user_score.id,
                    mode=user_score.gamemode,
                    user=user_score.user,
                )
                or 0
            ),
            "score": resp,
        }


@router.get(
    "/beatmaps/{beatmap_id}/scores/users/{user_id}/all",
    tags=["Scores"],
    responses={
        200: api_doc(
            (
                "All scores of user on beatmap\n\n"
                "If `x-api-version >= 20220705`, returns list of `Score`, "
                "otherwise list of `LegacyScoreResp`."
            ),
            list[ScoreModel] | list[LegacyScoreResp],
            DEFAULT_SCORE_INCLUDES,
        )
    },
    name="Get All User Scores on Beatmap",
    description="Get all scores list for specified user on specified beatmap.",
)
async def get_user_all_beatmap_scores(
    db: Database,
    api_version: APIVersion,
    beatmap_id: Annotated[int, Path(description="Beatmap ID")],
    user_id: Annotated[int, Path(description="User ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    legacy_only: Annotated[bool | None, Query(description="Whether to query stable scores only")] = None,
    ruleset: Annotated[GameMode | None, Query(description="Specified ruleset (optional)")] = None,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    all_user_scores = (
        await db.exec(
            select(Score)
            .where(
                Score.gamemode == ruleset if ruleset is not None else True,
                Score.beatmap_id == beatmap_id,
                Score.user_id == user_id,
                col(Score.passed).is_(True),
                ~User.is_restricted_query(col(Score.user_id)),
            )
            .order_by(col(Score.total_score).desc())
        )
    ).all()

    return [
        await score.to_resp(
            db,
            api_version,
            includes=DEFAULT_SCORE_INCLUDES,
            show_nsfw_media=show_nsfw_media,
        )
        for score in all_user_scores
    ]


@router.post(
    "/beatmaps/{beatmap_id}/solo/scores",
    tags=["Gameplay"],
    response_model=ScoreTokenResp,
    name="Create Solo Score Submission Token",
    description="\nCreate a one-time score submission token for specified beatmap.",
)
async def create_solo_score(
    background_task: BackgroundTasks,
    request: Request,
    db: Database,
    redis: Redis,
    fetcher: Fetcher,
    verification_service: ClientVerificationService,
    beatmap_id: Annotated[int, Path(description="Beatmap ID")],
    beatmap_hash: Annotated[str, Form(description="Beatmap file hash")],
    ruleset_id: Annotated[int, Form(..., description="Ruleset numeric ID (0-3)")],
    current_user: ClientUser,
    version_hash: Annotated[str, Form(description="Game version hash")] = "",
    ruleset_hash: Annotated[str, Form(description="Ruleset version hash")] = "",
):
    # Fetch user ID immediately to avoid lazy-loading issues
    user_id = current_user.id

    try:
        gamemode = GameMode.from_int(ruleset_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ruleset ID")

    client_version = await verification_service.validate_client_version(version_hash)

    # torii: las cuentas de prueba propias corren builds compilados a mano, o sea
    # con un hash que por definicion no esta registrado. Se las exime aca tambien y
    # no solo en el login, si no entrarian pero no podrian mandar un score, que es
    # justo lo que hay que poder probar.
    if not client_version and user_id not in settings.client_check_exempt_user_ids:
        if version_hash:
            await verification_service.record_unknown_hash(
                version_hash,
                user_agent=request.headers.get("user-agent") or "",
                user_id=user_id,
                source="create_solo_score_blocked",
            )
        logger.info(
            f"Client version check failed for user {current_user.id} on beatmap {beatmap_id} "
            f"(version hash: {version_hash})"
        )
        raise HTTPException(status_code=422, detail="invalid client hash")
    if not any((client_version.client_name, client_version.version, client_version.os)) and version_hash:
        await verification_service.record_unknown_hash(
            version_hash,
            user_agent=request.headers.get("user-agent") or "",
            user_id=user_id,
            source="create_solo_score",
        )

    await _store_last_client_hash(redis, user_id, version_hash)

    if await current_user.is_restricted(db):
        raise HTTPException(
            status_code=403,
            detail="You are restricted from creating score tokens on Torii.",
        )

    beatmap = await Beatmap.get_or_fetch(db, fetcher, bid=beatmap_id)
    if not beatmap:
        raise HTTPException(status_code=404, detail="beatmap not found")

    # Local uploads can end up with hash drift between editor/export/import paths.
    # For local maps we trust the beatmap_id and only enforce hash for non-local maps.
    if not beatmap.is_local and beatmap.checksum and beatmap.checksum.lower() != beatmap_hash.lower():
        # Checksum mismatch — the map may have been updated on osu! since we last
        # synced.  Re-fetch from the official API to get the latest checksum before
        # rejecting the score.  This handles pending/qualified/WIP maps that update
        # frequently and mirrors that lag behind.
        try:
            fresh_resp = await fetcher.get_beatmap(beatmap_id, None)
            fresh_checksum = (fresh_resp.get("checksum") or "").lower()
            if fresh_checksum and fresh_checksum == beatmap_hash.lower():
                # API confirms the client has the correct (newer) version — update our DB.
                beatmap.checksum = fresh_checksum
                fresh_ranked = fresh_resp.get("ranked")
                if fresh_ranked is not None:
                    try:
                        from app.database.beatmap import BeatmapRankStatus
                        beatmap.beatmap_status = BeatmapRankStatus(int(fresh_ranked))
                    except (TypeError, ValueError):
                        pass
                db.add(beatmap)
                await db.commit()
                await db.refresh(beatmap)
                logger.info(
                    "Beatmap {} checksum updated from API (client had newer version): {}",
                    beatmap_id,
                    fresh_checksum,
                )
            else:
                raise HTTPException(status_code=422, detail="invalid or missing beatmap_hash")
        except HTTPException:
            raise
        except Exception as refetch_err:
            logger.warning("Failed to re-fetch beatmap {} for checksum update: {}", beatmap_id, refetch_err)
            raise HTTPException(status_code=422, detail="invalid or missing beatmap_hash")

    if not (result := gamemode.check_ruleset_version(ruleset_hash)):
        logger.info(
            f"Ruleset version check failed for user {current_user.id} on beatmap {beatmap_id} "
            f"(ruleset: {ruleset_id}, hash: {ruleset_hash})"
        )
        raise HTTPException(
            status_code=422,
            detail=result.error_msg or "Ruleset version check failed",
        )

    # Ensure beatmap exists in database to prevent foreign key constraint failures
    await Beatmap.get_or_fetch(db, fetcher, bid=beatmap_id)

    background_task.add_task(_preload_beatmap_for_pp_calculation, beatmap_id)
    client_version_label = _derive_client_version_label(client_version, request, version_hash)
    try:
        await LoginLogService.attach_client_identity_to_recent_login(
            db=db,
            user_id=user_id,
            request=request,
            client_hash=version_hash,
            client_label=client_version_label,
        )
    except Exception as e:
        logger.debug(f"Failed to backfill login client identity for user {user_id}: {e}")

    async with db:
        score_token = ScoreToken(
            user_id=user_id,
            beatmap_id=beatmap_id,
            ruleset_id=GameMode.from_int(ruleset_id),
            client_version=client_version_label,
        )
        db.add(score_token)
        await db.commit()
        await db.refresh(score_token)
        logger.debug(
            "User {user_id} created solo score {score_token} for beatmap {beatmap_id} "
            "(mode: {mode}), using client {client_version}",
            user_id=user_id,
            score_token=score_token.id,
            beatmap_id=beatmap_id,
            mode=ruleset_id,
            client_version=client_version_label or "unknown",
        )
        return ScoreTokenResp.from_db(score_token)


@router.put(
    "/beatmaps/{beatmap_id}/solo/scores/{token}",
    tags=["Gameplay"],
    name="Submit Solo Score",
    description="\nSubmit solo score using token.",
    responses={200: api_doc("Solo score submission result.", ScoreModel)},
)
async def submit_solo_score(
    background_task: BackgroundTasks,
    db: Database,
    beatmap_id: Annotated[int, Path(description="Beatmap ID")],
    token: Annotated[int, Path(description="Score token ID")],
    info: Annotated[SoloScoreSubmissionInfo, Body(description="Score submission info")],
    current_user: ClientUser,
    redis: Redis,
    fetcher: Fetcher,
):
    return await submit_score(background_task, info, token, current_user, db, redis, fetcher)


@router.post(
    "/rooms/{room_id}/playlist/{playlist_id}/scores",
    tags=["Gameplay"],
    response_model=ScoreTokenResp,
    name="Create Room Item Score Token",
    description="\nCreate score submission token for room playlist item.",
)
async def create_playlist_score(
    session: Database,
    background_task: BackgroundTasks,
    request: Request,
    redis: Redis,
    fetcher: Fetcher,
    room_id: int,
    playlist_id: int,
    verification_service: ClientVerificationService,
    beatmap_id: Annotated[int, Form(description="Beatmap ID")],
    beatmap_hash: Annotated[str, Form(description="Beatmap file hash")],
    ruleset_id: Annotated[int, Form(..., description="Ruleset numeric ID (0-3)")],
    current_user: ClientUser,
    version_hash: Annotated[str, Form(description="Beatmap version hash")] = "",
    ruleset_hash: Annotated[str, Form(description="Ruleset version hash")] = "",
):
    try:
        gamemode = GameMode.from_int(ruleset_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ruleset ID")

    if not (
        client_version := await verification_service.validate_client_version(
            version_hash,
        )
    ):
        if version_hash:
            await verification_service.record_unknown_hash(
                version_hash,
                user_agent=request.headers.get("user-agent") or "",
                user_id=current_user.id,
                source="create_playlist_score_blocked",
            )
        logger.info(
            f"Client version check failed for user {current_user.id} on room {room_id}, playlist {playlist_id} "
            f"(version hash: {version_hash})"
        )
        raise HTTPException(status_code=422, detail="invalid client hash")
    if not any((client_version.client_name, client_version.version, client_version.os)) and version_hash:
        await verification_service.record_unknown_hash(
            version_hash,
            user_agent=request.headers.get("user-agent") or "",
            user_id=current_user.id,
            source="create_playlist_score",
        )

    await _store_last_client_hash(redis, current_user.id, version_hash)

    if not (result := gamemode.check_ruleset_version(ruleset_hash)):
        logger.info(
            f"Ruleset version check failed for user {current_user.id} on room {room_id}, playlist {playlist_id},"
            f" (ruleset: {ruleset_id}, hash: {ruleset_hash})"
        )
        raise HTTPException(
            status_code=422,
            detail=result.error_msg or "Ruleset version check failed",
        )

    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="You are restricted from submitting multiplayer scores")

    # Ensure beatmap exists in database to prevent foreign key constraint failures
    await Beatmap.get_or_fetch(session, fetcher, bid=beatmap_id)

    user_id = current_user.id

    room = await session.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    db_room_time = room.ends_at.replace(tzinfo=UTC) if room.ends_at else None
    if db_room_time and db_room_time < utcnow().replace(tzinfo=UTC):
        raise HTTPException(status_code=400, detail="Room has ended")
    item = (await session.exec(select(Playlist).where(Playlist.id == playlist_id, Playlist.room_id == room_id))).first()
    if not item:
        raise HTTPException(status_code=404, detail="Playlist not found")

    # validate
    if not item.freestyle:
        if item.ruleset_id != ruleset_id:
            raise HTTPException(status_code=400, detail="Ruleset mismatch in playlist item")
        if item.beatmap_id != beatmap_id:
            raise HTTPException(status_code=400, detail="Beatmap ID mismatch in playlist item")
    agg = await session.exec(
        select(ItemAttemptsCount).where(
            ItemAttemptsCount.room_id == room_id,
            ItemAttemptsCount.user_id == user_id,
        )
    )
    agg = agg.first()
    if agg and room.max_attempts and agg.attempts >= room.max_attempts:
        raise HTTPException(
            status_code=422,
            detail="You have reached the maximum attempts for this room",
        )
    if item.expired:
        raise HTTPException(status_code=400, detail="Playlist item has expired")
    if item.played_at:
        raise HTTPException(status_code=400, detail="Playlist item has already been played")
    # Mod validation probably not needed here...
    background_task.add_task(_preload_beatmap_for_pp_calculation, beatmap_id)
    client_version_label = _derive_client_version_label(client_version, request, version_hash)
    try:
        await LoginLogService.attach_client_identity_to_recent_login(
            db=session,
            user_id=user_id,
            request=request,
            client_hash=version_hash,
            client_label=client_version_label,
        )
    except Exception as e:
        logger.debug(f"Failed to backfill login client identity for user {user_id}: {e}")

    score_token = ScoreToken(
        user_id=user_id,
        beatmap_id=beatmap_id,
        ruleset_id=GameMode.from_int(ruleset_id),
        playlist_item_id=playlist_id,
        room_id=room_id,
        client_version=client_version_label,
    )
    session.add(score_token)
    await session.commit()
    await session.refresh(score_token)
    logger.debug(
        "User {user_id} created playlist score {score_token} for beatmap {beatmap_id} "
        "(mode: {mode}, room {room_id}, item {playlist_id}), using client {client_version}",
        user_id=user_id,
        score_token=score_token.id,
        beatmap_id=beatmap_id,
        mode=ruleset_id,
        room_id=room_id,
        playlist_id=playlist_id,
        client_version=client_version_label or "unknown",
    )
    return ScoreTokenResp.from_db(score_token)


@router.put(
    "/rooms/{room_id}/playlist/{playlist_id}/scores/{token}",
    tags=["Gameplay"],
    name="Submit Room Item Score",
    description="\nSubmit score for room playlist item.",
    responses={200: api_doc("Solo score submission result.", ScoreModel)},
)
async def submit_playlist_score(
    background_task: BackgroundTasks,
    session: Database,
    room_id: int,
    playlist_id: int,
    token: int,
    info: SoloScoreSubmissionInfo,
    current_user: ClientUser,
    redis: Redis,
    fetcher: Fetcher,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="You are restricted from submitting multiplayer scores")

    user_id = current_user.id

    item = (await session.exec(select(Playlist).where(Playlist.id == playlist_id, Playlist.room_id == room_id))).first()
    if not item:
        raise HTTPException(status_code=404, detail="Playlist item not found")
    room = await session.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    room_category = room.category
    score_resp = await submit_score(
        background_task,
        info,
        token,
        current_user,
        session,
        redis,
        fetcher,
    )
    await process_playlist_best_score(
        room_id,
        playlist_id,
        user_id,
        score_resp["id"],
        score_resp["total_score"],
        session,
        redis,
    )
    await session.commit()
    if room_category == RoomCategory.DAILY_CHALLENGE and score_resp["passed"]:
        await process_daily_challenge_score(session, user_id, room_id)
    await ItemAttemptsCount.get_or_create(room_id, user_id, session)
    await session.commit()
    return score_resp


class IndexedScoreResp(MultiplayerScores):
    total: int
    user_score: MultiplayScoreDict | None = None  # pyright: ignore[reportInvalidTypeForm]


@router.get(
    "/rooms/{room_id}/playlist/{playlist_id}/scores",
    # response_model=IndexedScoreResp,
    name="Get Room Item Leaderboard",
    description="Get leaderboard for room playlist item.",
    tags=["Scores"],
    responses={
        200: {
            "description": (
                f"Room item leaderboard.\n\nIncludes: {', '.join([f'`{inc}`' for inc in Score.MULTIPLAYER_BASE_INCLUDES])}"
            ),
            "model": IndexedScoreResp,
        }
    },
)
async def index_playlist_scores(
    session: Database,
    room_id: int,
    playlist_id: int,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    limit: Annotated[int, Query(ge=1, le=50, description="Return count (1-50)")] = 50,
    cursor: Annotated[int, Query(alias="cursor[total_score]", description="Pagination cursor (lowest score of previous page)")] = 2000000,
):
    # Fetch user ID immediately to avoid lazy-loading issues
    user_id = current_user.id

    room = await session.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    limit = clamp(limit, 1, 50)

    scores = (
        await session.exec(
            select(PlaylistBestScore)
            .where(
                PlaylistBestScore.playlist_id == playlist_id,
                PlaylistBestScore.room_id == room_id,
                PlaylistBestScore.total_score < cursor,
                ~User.is_restricted_query(col(PlaylistBestScore.user_id)),
            )
            .order_by(col(PlaylistBestScore.total_score).desc())
            .limit(limit + 1)
        )
    ).all()
    has_more = len(scores) > limit
    if has_more:
        scores = scores[:-1]

    user_score = None
    score_resp = [await ScoreModel.transform(score.score, includes=Score.MULTIPLAYER_BASE_INCLUDES) for score in scores]
    for score in score_resp:
        if (room.category == RoomCategory.DAILY_CHALLENGE and score["user_id"] == user_id and score["passed"]) or score[
            "user_id"
        ] == user_id:
            user_score = score
            user_score["position"] = await get_position(room_id, playlist_id, score["id"], session)
            break

    resp = IndexedScoreResp(
        scores=score_resp,
        user_score=user_score,
        total=len(scores),
        params={
            "limit": limit,
        },
    )
    if has_more:
        resp.cursor = {
            "total_score": scores[-1].total_score,
        }
    return resp


@router.get(
    "/rooms/{room_id}/playlist/{playlist_id}/scores/{score_id}",
    name="Get Room Item Single Score",
    description="Get details for single score in room playlist item.",
    tags=["Scores"],
    responses={
        200: api_doc(
            "Room item single score details.",
            ScoreModel,
            [*Score.MULTIPLAYER_BASE_INCLUDES, "position", "scores_around"],
        )
    },
)
async def show_playlist_score(
    session: Database,
    room_id: int,
    playlist_id: int,
    score_id: int,
    current_user: ClientUser,
    redis: Redis,
):
    room = await session.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    start_time = time.time()
    score_record = None
    is_playlist = room.category != RoomCategory.REALTIME
    completed = is_playlist

    while time.time() - start_time < READ_SCORE_TIMEOUT:
        if score_record is None:
            score_record = (
                await session.exec(
                    select(PlaylistBestScore).where(
                        PlaylistBestScore.score_id == score_id,
                        PlaylistBestScore.playlist_id == playlist_id,
                        PlaylistBestScore.room_id == room_id,
                        ~User.is_restricted_query(col(PlaylistBestScore.user_id)),
                    )
                )
            ).first()

        # solo usamos "completed" para decidir si agregar scores_around,
        # pero NO bloqueamos la respuesta esperando a que sea true.
        if not completed:
            if completed_players := await redis.get(f"multiplayer:{room_id}:gameplay:players"):
                completed = completed_players == "0"

        # ✅ clave: si el score ya existe, respondemos ya.
        if score_record:
            break

        await asyncio.sleep(0.05)

    if not score_record:
        raise HTTPException(status_code=404, detail="Score not found")

    includes = [
        *Score.MULTIPLAYER_BASE_INCLUDES,
        "position",
    ]
    if completed:
        includes.append("scores_around")

    resp = await ScoreModel.transform(
        score_record.score,
        includes=includes,
        playlist_id=playlist_id,
        room_id=room_id,
        is_playlist=is_playlist,
    )
    return resp


@router.get(
    "/rooms/{room_id}/playlist/{playlist_id}/scores/users/{user_id}",
    responses={
        200: api_doc(
            "Room item single score details.",
            ScoreModel,
            [*Score.MULTIPLAYER_BASE_INCLUDES, "position", "scores_around"],
        )
    },
    name="Get Room Item User Scores",
    description="Get scores for specified user in room playlist item.",
    tags=["Scores"],
)
async def get_user_playlist_score(
    session: Database,
    room_id: int,
    playlist_id: int,
    user_id: int,
    current_user: ClientUser,
):
    score_record = None
    start_time = time.time()
    while time.time() - start_time < READ_SCORE_TIMEOUT:
        score_record = (
            await session.exec(
                select(PlaylistBestScore).where(
                    PlaylistBestScore.user_id == user_id,
                    PlaylistBestScore.playlist_id == playlist_id,
                    PlaylistBestScore.room_id == room_id,
                    ~User.is_restricted_query(col(PlaylistBestScore.user_id)),
                )
            )
        ).first()
        if score_record:
            break
    if not score_record:
        raise HTTPException(status_code=404, detail="Score not found")

    # scores_around necesita saber la sala y si es playlist; sin estos kwargs el
    # resolver revienta con TypeError y el 500 hace que el cliente no pueda
    # mostrar el resultado: el score SI estaba guardado, pero el jugador veia
    # "not submitted". El otro endpoint (show_playlist_score) ya los pasaba;
    # este quedo con la llamada vieja cuando se le agregaron los parametros.
    room = await session.get(Room, room_id)
    is_playlist = room is not None and room.category != RoomCategory.REALTIME

    resp = await ScoreModel.transform(
        score_record.score,
        includes=[
            *Score.MULTIPLAYER_BASE_INCLUDES,
            "position",
            "scores_around",
        ],
        playlist_id=playlist_id,
        room_id=room_id,
        is_playlist=is_playlist,
    )
    return resp


@router.put(
    "/score-pins/{score_id}",
    status_code=204,
    name="Pin Score",
    description="\nPin specified score to user profile (ordered).",
    tags=["Scores"],
)
async def pin_score(
    db: Database,
    current_user: ClientUser,
    user_cache_service: UserCacheService,
    score_id: Annotated[int, Path(description="Score ID")],
):
    # Fetch user ID immediately to avoid lazy-loading issues
    user_id = current_user.id

    score_record = (
        await db.exec(
            select(Score).where(
                Score.id == score_id,
                Score.user_id == user_id,
                col(Score.passed).is_(True),
            )
        )
    ).first()
    if not score_record:
        raise HTTPException(status_code=404, detail="Score not found")

    if score_record.pinned_order > 0:
        return

    next_order = (
        (
            await db.exec(
                select(func.max(Score.pinned_order)).where(
                    Score.user_id == current_user.id,
                    Score.gamemode == score_record.gamemode,
                )
            )
        ).first()
        or 0
    ) + 1
    score_record.pinned_order = next_order
    await user_cache_service.invalidate_user_scores_cache(user_id, score_record.gamemode)
    await db.commit()


@router.delete(
    "/score-pins/{score_id}",
    status_code=204,
    name="Unpin Score",
    description="\nUnpin specified score.",
    tags=["Scores"],
)
async def unpin_score(
    db: Database,
    user_cache_service: UserCacheService,
    score_id: Annotated[int, Path(description="Score ID")],
    current_user: ClientUser,
):
    # Fetch user ID immediately to avoid lazy-loading issues
    user_id = current_user.id

    score_record = (await db.exec(select(Score).where(Score.id == score_id, Score.user_id == user_id))).first()
    if not score_record:
        raise HTTPException(status_code=404, detail="Score not found")

    if score_record.pinned_order == 0:
        return
    changed_score = (
        await db.exec(
            select(Score).where(
                Score.user_id == user_id,
                Score.pinned_order > score_record.pinned_order,
                Score.gamemode == score_record.gamemode,
            )
        )
    ).all()
    for s in changed_score:
        s.pinned_order -= 1
    score_record.pinned_order = 0
    await user_cache_service.invalidate_user_scores_cache(user_id, score_record.gamemode)
    await db.commit()


@router.post(
    "/score-pins/{score_id}/reorder",
    status_code=204,
    name="Reorder Pinned Scores",
    description=("\nReorder pinned scores display order. Provide only one of after_score_id or before_score_id."),
    tags=["Scores"],
)
async def reorder_score_pin(
    db: Database,
    user_cache_service: UserCacheService,
    current_user: ClientUser,
    score_id: Annotated[int, Path(description="Score ID")],
    after_score_id: Annotated[int | None, Body(description="Place after this score")] = None,
    before_score_id: Annotated[int | None, Body(description="Place before this score")] = None,
):
    # Fetch user ID immediately to avoid lazy-loading issues
    user_id = current_user.id

    score_record = (await db.exec(select(Score).where(Score.id == score_id, Score.user_id == user_id))).first()
    if not score_record:
        raise HTTPException(status_code=404, detail="Score not found")

    if score_record.pinned_order == 0:
        raise HTTPException(status_code=400, detail="Score is not pinned")

    if (after_score_id is None) == (before_score_id is None):
        raise HTTPException(
            status_code=400,
            detail="Either after_score_id or before_score_id must be provided (but not both)",
        )

    all_pinned_scores = (
        await db.exec(
            select(Score)
            .where(
                Score.user_id == current_user.id,
                Score.pinned_order > 0,
                Score.gamemode == score_record.gamemode,
            )
            .order_by(col(Score.pinned_order))
        )
    ).all()

    target_order = None
    reference_score_id = after_score_id or before_score_id

    reference_score = next((s for s in all_pinned_scores if s.id == reference_score_id), None)
    if not reference_score:
        detail = "After score not found" if after_score_id else "Before score not found"
        raise HTTPException(status_code=404, detail=detail)

    target_order = reference_score.pinned_order + 1 if after_score_id else reference_score.pinned_order

    current_order = score_record.pinned_order

    if current_order == target_order:
        return

    updates = []

    if current_order < target_order:
        for s in all_pinned_scores:
            if current_order < s.pinned_order <= target_order and s.id != score_id:
                updates.append((s.id, s.pinned_order - 1))
        if after_score_id:
            final_target = target_order - 1 if target_order > current_order else target_order
        else:
            final_target = target_order
    else:
        for s in all_pinned_scores:
            if target_order <= s.pinned_order < current_order and s.id != score_id:
                updates.append((s.id, s.pinned_order + 1))
        final_target = target_order

    for score_id, new_order in updates:
        await db.exec(select(Score).where(Score.id == score_id))
        score_to_update = (await db.exec(select(Score).where(Score.id == score_id))).first()
        if score_to_update:
            score_to_update.pinned_order = new_order

    score_record.pinned_order = final_target
    await user_cache_service.invalidate_user_scores_cache(user_id, score_record.gamemode)
    await db.commit()


@router.get(
    "/scores/{score_id}/download",
    name="Download Score Replay",
    description="Download replay file for specified score.",
    tags=["Scores"],
    dependencies=[Depends(RateLimiter(times=10, minutes=1))],
)
async def download_score_replay(
    score_id: int,
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    storage_service: StorageService,
):
    # Fetch user ID immediately to avoid lazy-loading issues
    user_id = current_user.id

    score = (await db.exec(select(Score).where(Score.id == score_id))).first()
    if not score:
        raise HTTPException(status_code=404, detail="Score not found")

    filepath = score.replay_filename

    if not await storage_service.is_exists(filepath):
        raise HTTPException(status_code=404, detail="Replay file not found")

    is_friend = (
        score.user_id == user_id
        or (
            await db.exec(
                select(exists()).where(
                    Relationship.user_id == user_id,
                    Relationship.target_id == score.user_id,
                    Relationship.type == RelationshipType.FOLLOW,
                )
            )
        ).first()
    )
    if not is_friend:
        replay_watched_count = (
            await db.exec(
                select(ReplayWatchedCount).where(
                    ReplayWatchedCount.user_id == score.user_id,
                    ReplayWatchedCount.year == date.today().year,
                    ReplayWatchedCount.month == date.today().month,
                )
            )
        ).first()
        if replay_watched_count is None:
            replay_watched_count = ReplayWatchedCount(
                user_id=score.user_id, year=date.today().year, month=date.today().month
            )
            db.add(replay_watched_count)
        replay_watched_count.count += 1
        await db.commit()
    return RedirectResponse(
        await storage_service.get_file_url(filepath), 301, headers={"Content-Type": "application/x-osu-replay"}
    )

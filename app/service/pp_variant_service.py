from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from app.calculator import (
    calculate_pp_weight,
    calculate_weighted_acc,
    calculate_weighted_pp,
    get_calculator,
    pre_fetch_and_calculate_pp,
)
from app.database import User, UserStatistics
from app.database.best_scores import BestScore
from app.database.score import LegacyScoreResp, Score, _RELAX_AP_MODES
from app.database.statistics import active_cutoff
from app.log import log
from app.models.mods import mods_can_get_pp
from app.models.score import GameMode
from app.service.cache_build_service import cache_build_lock
from app.utils import safe_json_dumps

from redis.asyncio import Redis
from sqlmodel import col, exists, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

if False:  # TYPE_CHECKING
    from app.fetcher import Fetcher  # pragma: no cover

PpVariant = Literal["stable", "pp_dev"]

logger = log("PpVariant")

SCORE_PP_DEV_CACHE_TTL_SECONDS = 60 * 60 * 24
# 60s reintentaba demasiado seguido: con el perf-server a media maquina (fallando
# algunas y no las suficientes como para abrir el cortacircuitos) el churn seguia.
# 5 min no es "sticky stable" y baja los reintentos 5 veces.
SCORE_PP_DEV_FALLBACK_CACHE_TTL_SECONDS = 300
USER_PP_DEV_CACHE_TTL_SECONDS = 60 * 30       # user stats: 30 min (was 10)
USER_PP_DEV_PROFILE_CACHE_TTL_SECONDS = 60 * 30  # full profile: 30 min
RANKING_PP_DEV_CACHE_TTL_SECONDS = 60 * 10    # ranking snapshot: 10 min (was 5)
SCORE_PP_DEV_CALC_TIMEOUT_SECONDS = 2.5

# ── cortacircuitos del pp-dev ────────────────────────────────────────────────
# pp-dev usa el MISMO perf-server que el envio de scores en vivo (get_calculator()
# es uno solo). Cuando el perf-server se pone lento, esto pasaba:
#
#   se vence el calculo -> se cachea el fallback 60s -> un minuto despues alguien
#   abre un perfil y se reintenta -> mas carga -> se vence otra vez -> ...
#
# y no sale nunca solo. Medido el 8-ago-2026: 13.611 timeouts en una hora, picos
# de 790 por minuto, cada score reintentado unas 30 veces. El perf-server quedaba
# tapado y los scores que la gente estaba jugando se guardaban con 0pp, con el PUT
# del cliente colgado hasta que cortaba a los 30s ("Score was not submitted").
#
# Un perfil pide hasta PP_DEV_RECALC_TOP_SCORE_LIMIT scores, asi que una sola
# visita son 100 llamadas: el amplificador.
#
# Con esto, si el perf-server viene fallando se deja de pedir pp-dev un rato y se
# muestra pp stable. Recalcular pp para una vista de perfil es cosmetico; que a
# alguien le entre el score que acaba de jugar no lo es.
_BREAKER_KEY = "ppdev:breaker"
_BREAKER_FAILS_KEY = "ppdev:fails"
_BREAKER_FAIL_WINDOW_SECONDS = 60
_BREAKER_TRIP_AFTER_FAILS = 20
_BREAKER_COOLDOWN_SECONDS = 300


async def _breaker_abierto(redis: Redis) -> bool:
    try:
        return await redis.exists(_BREAKER_KEY) > 0
    except Exception:
        # si redis no contesta no vamos a empeorarlo con mas trabajo: se asume abierto.
        return True


async def _breaker_anotar_falla(redis: Redis) -> None:
    try:
        fallas = await redis.incr(_BREAKER_FAILS_KEY)
        if fallas == 1:
            await redis.expire(_BREAKER_FAILS_KEY, _BREAKER_FAIL_WINDOW_SECONDS)
        if fallas >= _BREAKER_TRIP_AFTER_FAILS:
            await redis.set(_BREAKER_KEY, "1", ex=_BREAKER_COOLDOWN_SECONDS)
            await redis.delete(_BREAKER_FAILS_KEY)
            logger.warning(
                f"pp-dev cortado por {_BREAKER_COOLDOWN_SECONDS}s: {fallas} calculos fallados "
                f"en {_BREAKER_FAIL_WINDOW_SECONDS}s. Se sirve pp stable para dejarle el "
                f"perf-server a los scores en vivo."
            )
    except Exception:
        pass


async def _breaker_anotar_exito(redis: Redis) -> None:
    try:
        await redis.delete(_BREAKER_FAILS_KEY)
    except Exception:
        pass
PP_DEV_RECALC_TOP_SCORE_LIMIT = 100
PP_DEV_RANKING_RECALC_USER_LIMIT = 50
_snapshot_rebuild_tasks: dict[GameMode, asyncio.Task[None]] = {}
PP_DEV_CALC_BATCH_CONCURRENCY = 3
PP_DEV_PROFILE_SNAPSHOT_TIMEOUT_SECONDS = 6.0


def normalize_pp_variant(value: str | None) -> PpVariant:
    if value is None:
        return "stable"
    normalized = value.strip().lower()
    if normalized in {"pp_dev", "ppdev", "dev", "alpha_ppdev"}:
        return "pp_dev"
    return "stable"


def is_pp_dev_variant(pp_variant: PpVariant) -> bool:
    return pp_variant == "pp_dev"


def _score_pp_dev_cache_key(score_id: int) -> str:
    return f"score:{score_id}:pp_variant:pp_dev"


def _user_pp_dev_stats_cache_key(user_id: int, mode: GameMode) -> str:
    return f"user:{user_id}:pp_variant:pp_dev:stats:{mode.value}"


def _ranking_pp_dev_snapshot_cache_key(mode: GameMode) -> str:
    return f"ranking:pp_variant:pp_dev:snapshot:{mode.value}"


async def invalidate_pp_variant_caches_for_user(
    *,
    redis: Redis,
    user_id: int,
    mode: GameMode,
) -> None:
    keys = [
        _user_pp_dev_stats_cache_key(user_id, mode),
        _ranking_pp_dev_snapshot_cache_key(mode),
    ]
    await redis.delete(*keys)


async def get_score_pp_variant(
    *,
    session: AsyncSession,
    score: Score,
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    recalculate_if_missing: bool = True,
) -> float:
    if not is_pp_dev_variant(pp_variant):
        return float(score.pp or 0.0)

    cache_key = _score_pp_dev_cache_key(score.id)
    cached = await redis.get(cache_key)
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass

    if not recalculate_if_missing:
        return float(score.pp or 0.0)

    if not (score.passed and score.ranked):
        await redis.set(cache_key, "0", ex=SCORE_PP_DEV_CACHE_TTL_SECONDS)
        return 0.0

    # Legacy score rows can miss mod settings that ranked_mods validation expects.
    # If the score already has stable PP, still include it in pp-dev recalculation.
    if not mods_can_get_pp(int(score.gamemode), score.mods) and float(score.pp or 0.0) <= 0:
        await redis.set(cache_key, "0", ex=SCORE_PP_DEV_CACHE_TTL_SECONDS)
        return 0.0

    # Accuracy floor for relax / autopilot modes: < 75% acc → 0 pp.
    if score.gamemode in _RELAX_AP_MODES and score.accuracy < 0.75:
        await redis.set(cache_key, "0", ex=SCORE_PP_DEV_CACHE_TTL_SECONDS)
        return 0.0

    # Cortacircuitos abierto: se devuelve stable y NO se cachea, asi cuando el
    # perf-server se recupera vuelve a intentar en la proxima visita en vez de
    # quedar pegado al TTL del fallback.
    if await _breaker_abierto(redis):
        return float(score.pp or 0.0)

    # pp-dev is now the primary calculator — use it directly.
    pp_dev_calculator = get_calculator()

    pp_value = float(score.pp or 0.0)
    calc_succeeded = False
    try:
        # Each pp-dev calc gets its OWN session (avoids "concurrent operations"
        # when batch tasks share the caller's session).  We use wait_for so
        # timed-out tasks are cancelled — the `async with` ensures the session
        # is closed even on CancelledError, preventing connection-pool exhaustion.
        from app.dependencies.database import engine as _engine
        from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

        async def _calc_with_own_session():
            async with _AsyncSession(_engine) as own_session:
                return await pre_fetch_and_calculate_pp(
                    score,
                    own_session,
                    redis,
                    fetcher,
                    calculator_override=pp_dev_calculator,
                )

        calculated, success = await asyncio.wait_for(
            _calc_with_own_session(),
            timeout=SCORE_PP_DEV_CALC_TIMEOUT_SECONDS,
        )
        if success:
            pp_value = float(calculated)
            calc_succeeded = True
            await _breaker_anotar_exito(redis)
    except TimeoutError:
        await _breaker_anotar_falla(redis)
        logger.warning(
            f"pp-dev calculation timeout for score {score.id} "
            f"after {SCORE_PP_DEV_CALC_TIMEOUT_SECONDS}s; using stable pp fallback"
        )
    except Exception as e:
        await _breaker_anotar_falla(redis)
        logger.warning(f"Failed to calculate pp-dev for score {score.id}, using stable pp: {e}")

    # Avoid "sticky stable" results: cache fallback briefly so we retry pp-dev soon.
    ttl = SCORE_PP_DEV_CACHE_TTL_SECONDS if calc_succeeded else SCORE_PP_DEV_FALLBACK_CACHE_TTL_SECONDS
    await redis.set(cache_key, str(pp_value), ex=ttl)
    return pp_value


async def get_score_pp_variant_batch(
    *,
    session: AsyncSession,
    scores: list[Score],
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    recalc_top_n: int = 0,
) -> dict[int, float]:
    if not scores:
        return {}

    if not is_pp_dev_variant(pp_variant):
        return {score.id: float(score.pp or 0.0) for score in scores}

    semaphore = asyncio.Semaphore(PP_DEV_CALC_BATCH_CONCURRENCY)
    result: dict[int, float] = {}

    async def compute(index: int, score: Score) -> tuple[int, float]:
        async with semaphore:
            try:
                value = await get_score_pp_variant(
                    session=session,
                    score=score,
                    pp_variant=pp_variant,
                    redis=redis,
                    fetcher=fetcher,
                    recalculate_if_missing=index < recalc_top_n,
                )
            except Exception as e:
                logger.warning(f"pp-dev batch calculation failed for score {score.id}, using stable pp: {e}")
                value = float(score.pp or 0.0)
            return score.id, float(value)

    tasks = [asyncio.create_task(compute(index, score)) for index, score in enumerate(scores)]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for score, outcome in zip(scores, outcomes, strict=False):
        if isinstance(outcome, BaseException):
            logger.warning(f"pp-dev batch task failed for score {score.id}, using stable pp: {outcome}")
            result[score.id] = float(score.pp or 0.0)
            continue

        score_id, value = outcome
        result[score_id] = value

    return result


async def get_user_pp_variant_statistics(
    *,
    session: AsyncSession,
    user_id: int,
    mode: GameMode,
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    recalculate_top_scores: bool = True,
) -> tuple[float, float]:
    if not is_pp_dev_variant(pp_variant):
        stat = (
            await session.exec(
                select(UserStatistics).where(
                    UserStatistics.user_id == user_id,
                    UserStatistics.mode == mode,
                )
            )
        ).first()
        if stat is None:
            return 0.0, 0.0
        return float(stat.pp or 0.0), float(stat.hit_accuracy or 0.0)

    cache_key = _user_pp_dev_stats_cache_key(user_id, mode)
    cached = await redis.get(cache_key)
    if cached:
        try:
            payload = json.loads(cached)
            return float(payload.get("pp", 0.0)), float(payload.get("hit_accuracy", 0.0))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    # Use canonical best-score rows (already one per beatmap in stable) as the pp-dev mirror base.
    # This keeps the variant responsive while still recalculating PP values via pp-dev formula.
    scores = (
        await session.exec(
            select(Score).where(
                Score.user_id == user_id,
                Score.gamemode == mode,
                col(Score.passed).is_(True),
                col(Score.ranked).is_(True),
                exists().where(col(BestScore.score_id) == Score.id),
            )
        )
    ).all()

    ordered_scores = sorted(scores, key=lambda s: (float(s.pp or 0.0), s.id), reverse=True)
    pp_by_score_id = await get_score_pp_variant_batch(
        session=session,
        scores=ordered_scores,
        pp_variant=pp_variant,
        redis=redis,
        fetcher=fetcher,
        recalc_top_n=PP_DEV_RECALC_TOP_SCORE_LIMIT if recalculate_top_scores else 0,
    )

    best_by_beatmap: dict[int, tuple[float, float, int]] = {}
    for score in ordered_scores:
        # Same compatibility rule as get_score_pp_variant():
        # keep historically valid ranked scores that already have stable PP.
        if not mods_can_get_pp(int(score.gamemode), score.mods) and float(score.pp or 0.0) <= 0:
            continue

        pp_value = float(pp_by_score_id.get(score.id, float(score.pp or 0.0)))
        if pp_value <= 0:
            continue

        previous = best_by_beatmap.get(score.beatmap_id)
        if previous is None or pp_value > previous[0] or (pp_value == previous[0] and score.id > previous[2]):
            best_by_beatmap[score.beatmap_id] = (pp_value, float(score.accuracy), score.id)

    best_scores = sorted(best_by_beatmap.values(), key=lambda item: (item[0], item[2]), reverse=True)

    pp_sum = 0.0
    acc_sum = 0.0
    for index, (pp_value, acc_value, _score_id) in enumerate(best_scores):
        pp_sum += calculate_weighted_pp(pp_value, index)
        acc_sum += calculate_weighted_acc(acc_value, index)

    if best_scores:
        acc_sum *= 100 / (20 * (1 - pow(0.95, len(best_scores))))
        acc_sum = max(0.0, min(100.0, acc_sum))

    payload = {
        "pp": pp_sum,
        "hit_accuracy": acc_sum,
    }
    await redis.set(cache_key, safe_json_dumps(payload), ex=USER_PP_DEV_CACHE_TTL_SECONDS)
    return pp_sum, acc_sum


async def get_pp_dev_ranking_snapshot(
    *,
    session: AsyncSession,
    ruleset: GameMode,
    redis: Redis,
    fetcher: "Fetcher",
) -> list[dict[str, Any]]:
    cache_key = _ranking_pp_dev_snapshot_cache_key(ruleset)
    cached = await redis.get(cache_key)
    if cached:
        try:
            payload = json.loads(cached)
            if isinstance(payload, list):
                return payload
        except json.JSONDecodeError:
            pass

    async with cache_build_lock(redis, cache_key):
        cached = await redis.get(cache_key)
        if cached:
            try:
                payload = json.loads(cached)
                if isinstance(payload, list):
                    return payload
            except json.JSONDecodeError:
                pass

        rows = (
            await session.exec(
                select(UserStatistics.user_id, UserStatistics.ranked_score, UserStatistics.pp, User.country_code)
                .join(User, col(User.id) == col(UserStatistics.user_id))
                .where(
                    col(UserStatistics.mode) == ruleset,
                    col(UserStatistics.is_ranked).is_(True),
                    ~User.is_restricted_query(col(UserStatistics.user_id)),
                    # Active-only ranking: 30d+ inactive drop out of the pp_dev
                    # snapshot so the array-index rank renumbers densely.
                    col(UserStatistics.last_played) >= active_cutoff(),
                )
            )
        ).all()

        rows = sorted(rows, key=lambda row: (float(row[2] or 0.0), int(row[1] or 0), int(row[0])), reverse=True)

        snapshot: list[dict[str, Any]] = []
        for index, (user_id, ranked_score, _stable_pp, country_code) in enumerate(rows):
            pp_value, hit_accuracy = await get_user_pp_variant_statistics(
                session=session,
                user_id=int(user_id),
                mode=ruleset,
                pp_variant="pp_dev",
                redis=redis,
                fetcher=fetcher,
                recalculate_top_scores=index < PP_DEV_RANKING_RECALC_USER_LIMIT,
            )
            if pp_value <= 0:
                continue
            snapshot.append(
                {
                    "user_id": int(user_id),
                    "pp": float(pp_value),
                    "hit_accuracy": float(hit_accuracy),
                    "ranked_score": int(ranked_score or 0),
                    "country_code": str(country_code or "").upper(),
                }
            )

        snapshot.sort(key=lambda row: (row["pp"], row["ranked_score"], row["user_id"]), reverse=True)
        await redis.set(cache_key, safe_json_dumps(snapshot), ex=RANKING_PP_DEV_CACHE_TTL_SECONDS)
        return snapshot


def apply_pp_variant_to_score_responses(
    *,
    scores: list[Score],
    score_responses: list[dict[str, Any] | LegacyScoreResp],
    pp_by_score_id: dict[int, float],
    add_weight: bool,
    rank_by_score_id: dict[int, int] | None = None,
) -> None:
    if not score_responses:
        return

    rank_map = rank_by_score_id
    if add_weight and rank_map is None:
        ranked = sorted(scores, key=lambda s: (pp_by_score_id.get(s.id, float(s.pp or 0.0)), s.id), reverse=True)
        rank_map = {score.id: index + 1 for index, score in enumerate(ranked)}

    for score, score_resp in zip(scores, score_responses):
        score_pp = float(pp_by_score_id.get(score.id, float(score.pp or 0.0)))

        if isinstance(score_resp, dict):
            score_resp["pp"] = score_pp
        else:
            score_resp.pp = score_pp

        if add_weight:
            rank = rank_map.get(score.id) if rank_map is not None else None
            if isinstance(score_resp, dict):
                score_resp["weight"] = calculate_pp_weight(rank - 1) if rank is not None else 0.0


def get_user_ranks_from_snapshot(
    *,
    snapshot: list[dict[str, Any]],
    user_id: int,
    country_code: str | None = None,
) -> tuple[int | None, int | None]:
    global_rank = None
    country_rank = None
    normalized_country = (country_code or "").upper()

    for idx, row in enumerate(snapshot, start=1):
        if int(row.get("user_id") or 0) == user_id:
            global_rank = idx
            break

    if normalized_country:
        rank_in_country = 0
        for row in snapshot:
            if str(row.get("country_code") or "").upper() != normalized_country:
                continue
            rank_in_country += 1
            if int(row.get("user_id") or 0) == user_id:
                country_rank = rank_in_country
                break

    return global_rank, country_rank


async def apply_pp_variant_to_user_response(
    *,
    session: AsyncSession,
    user_resp: dict[str, Any],
    user_id: int,
    mode: GameMode,
    pp_variant: PpVariant,
    redis: Redis,
    fetcher: "Fetcher",
    country_code: str | None,
) -> None:
    if not is_pp_dev_variant(pp_variant):
        return

    pp_value, acc_value = await get_user_pp_variant_statistics(
        session=session,
        user_id=user_id,
        mode=mode,
        pp_variant=pp_variant,
        redis=redis,
        fetcher=fetcher,
    )
    statistics = user_resp.get("statistics")
    fallback_global_rank = statistics.get("global_rank") if isinstance(statistics, dict) else None
    fallback_country_rank = statistics.get("country_rank") if isinstance(statistics, dict) else None
    global_rank = fallback_global_rank
    country_rank = fallback_country_rank

    # Prefer exact pp-dev ranking from cached snapshot. Building one is O(N
    # users) and recalculates pp for the top 50 on demand, which routinely
    # exceeded the 6 s wait_for budget and timed out — leaving the profile
    # response with a stale fallback rank anyway.
    #
    # New strategy: only ever READ the snapshot for the profile response. If
    # it's missing, schedule a background build (so the next request hits a
    # warm cache) and fall through to the fast pp/ranked_score approximation
    # below. The profile request returns immediately either way.
    snapshot: list[dict[str, Any]] | None = None
    try:
        cached_raw = await redis.get(_ranking_pp_dev_snapshot_cache_key(mode))
        if cached_raw:
            try:
                payload = json.loads(cached_raw)
                if isinstance(payload, list):
                    snapshot = payload
            except json.JSONDecodeError:
                snapshot = None
    except Exception as e:
        logger.warning(f"pp-dev snapshot cache read failed (user_id={user_id}): {e}")

    if snapshot is None:
        # Spawn background rebuild so subsequent requests are fast. Use its own
        # session and don't await — this must NEVER block the profile response.
        async def _rebuild_snapshot_bg() -> None:
            try:
                from app.dependencies.database import engine as _engine
                from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

                async with _AsyncSession(_engine) as own_session:
                    await get_pp_dev_ranking_snapshot(
                        session=own_session,
                        ruleset=mode,
                        redis=redis,
                        fetcher=fetcher,
                    )
            except Exception as bg_err:
                logger.debug(
                    f"Background pp-dev snapshot rebuild failed for {mode.value}: {bg_err}"
                )

        task = _snapshot_rebuild_tasks.get(mode)
        if task is None or task.done():
            task = asyncio.create_task(_rebuild_snapshot_bg())
            _snapshot_rebuild_tasks[mode] = task

            def clear_rebuild(finished: asyncio.Task[None]) -> None:
                if _snapshot_rebuild_tasks.get(mode) is finished:
                    _snapshot_rebuild_tasks.pop(mode, None)

            task.add_done_callback(clear_rebuild)

    if snapshot:
        global_rank, country_rank = get_user_ranks_from_snapshot(
            snapshot=snapshot,
            user_id=user_id,
            country_code=country_code,
        )

    # Fallback: fast approximation when snapshot is unavailable.
    normalized_country = (country_code or "").upper()
    needs_country_rank = bool(normalized_country) and country_rank is None
    if pp_value > 0 and (global_rank is None or needs_country_rank):
        global_higher = (
            await session.exec(
                select(func.count())
                .select_from(UserStatistics)
                .where(
                    col(UserStatistics.mode) == mode,
                    col(UserStatistics.is_ranked).is_(True),
                    col(UserStatistics.pp) > pp_value,
                    ~User.is_restricted_query(col(UserStatistics.user_id)),
                )
            )
        ).one()
        global_rank = int(global_higher or 0) + 1

        if normalized_country:
            country_higher = (
                await session.exec(
                    select(func.count())
                    .select_from(UserStatistics)
                    .join(User, col(User.id) == col(UserStatistics.user_id))
                    .where(
                        col(UserStatistics.mode) == mode,
                        col(UserStatistics.is_ranked).is_(True),
                        col(UserStatistics.pp) > pp_value,
                        col(User.country_code) == normalized_country,
                        ~User.is_restricted_query(col(UserStatistics.user_id)),
                    )
                )
            ).one()
            country_rank = int(country_higher or 0) + 1

    statistics = user_resp.get("statistics")
    if isinstance(statistics, dict):
        statistics["pp"] = pp_value
        statistics["hit_accuracy"] = acc_value
        statistics["global_rank"] = global_rank
        statistics["country_rank"] = country_rank
        user_resp["statistics"] = statistics

    statistics_rulesets = user_resp.get("statistics_rulesets")
    if isinstance(statistics_rulesets, dict):
        mode_key = mode.value
        mode_stats = statistics_rulesets.get(mode_key)
        if isinstance(mode_stats, dict):
            mode_stats["pp"] = pp_value
            mode_stats["hit_accuracy"] = acc_value
            mode_stats["global_rank"] = global_rank
            mode_stats["country_rank"] = country_rank
            statistics_rulesets[mode_key] = mode_stats
            user_resp["statistics_rulesets"] = statistics_rulesets

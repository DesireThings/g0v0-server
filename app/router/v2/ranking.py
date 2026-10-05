from copy import deepcopy
from typing import Annotated, Any, Literal

from app.config import settings
from app.database import Team, TeamMember, User, UserStatistics
from app.database.user import UserModel
from app.database.statistics import UserStatisticsModel, active_cutoff
from app.dependencies.database import Database, get_redis
from app.dependencies.fetcher import get_fetcher
from app.dependencies.user import get_current_user
from app.models.score import GameMode
from app.service.pp_variant_service import (
    get_pp_dev_ranking_snapshot,
    normalize_pp_variant,
)
from app.service.ranking_cache_service import get_country_ranking_totals, get_ranking_cache_service
from app.utils import api_doc

from .router import router

from fastapi import BackgroundTasks, Path, Query, Security
from pydantic import BaseModel, Field
from sqlmodel import col, func, select


def _looks_like_default_cover_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    if normalized == UserModel.DEFAULT_COVER_URL.lower():
        return True
    # Asset proxy may rewrite domain, keep path marker based detection.
    return "/user-profile-covers/default.jpeg" in normalized or "/user-profile-covers/default.jpg" in normalized


async def _apply_nsfw_policy_to_rankings(
    session: Database,
    ranking_rows: list[dict[str, Any]],
    show_nsfw_media: bool,
    *,
    canonical: bool = False,
) -> list[dict[str, Any]]:
    users_to_hydrate: set[int] = set()
    for row in ranking_rows:
        user = row.get("user")
        if not isinstance(user, dict):
            continue
        user_id = user.get("id")
        if not isinstance(user_id, int):
            continue

        missing_flags = "avatar_nsfw" not in user or "cover_nsfw" not in user
        likely_sanitized = (
            user.get("avatar_url") == UserModel.DEFAULT_AVATAR_URL
            or _looks_like_default_cover_url(user.get("cover_url"))
            or (
                isinstance(user.get("cover"), dict)
                and (
                    _looks_like_default_cover_url(user["cover"].get("url"))
                    or _looks_like_default_cover_url(user["cover"].get("custom_url"))
                )
            )
        )
        if missing_flags or (likely_sanitized and not canonical):
            users_to_hydrate.add(user_id)

    hydrate_by_user: dict[int, tuple[str, dict | None, bool, bool]] = {}
    if users_to_hydrate:
        rows = (
            await session.exec(
                select(User.id, User.avatar_url, User.cover, User.avatar_nsfw, User.cover_nsfw).where(
                    col(User.id).in_(users_to_hydrate)
                )
            )
        ).all()
        hydrate_by_user = {
            uid: (
                avatar_url or UserModel.DEFAULT_AVATAR_URL,
                cover if isinstance(cover, dict) else None,
                bool(avatar_nsfw),
                bool(cover_nsfw),
            )
            for uid, avatar_url, cover, avatar_nsfw, cover_nsfw in rows
        }

    for row in ranking_rows:
        user = row.get("user")
        if not isinstance(user, dict):
            continue
        user_id = user.get("id")
        if isinstance(user_id, int) and user_id in hydrate_by_user:
            avatar_url, cover, avatar_nsfw, cover_nsfw = hydrate_by_user[user_id]
            user["avatar_url"] = avatar_url
            if cover:
                user["cover"] = dict(cover)
                cover_url = str(cover.get("url") or "")
                cover_custom_url = str(cover.get("custom_url") or "")
                # Prefer non-default cover URL when both keys exist.
                preferred_cover_url = (
                    cover_url
                    if cover_url and not _looks_like_default_cover_url(cover_url)
                    else cover_custom_url
                )
                user["cover_url"] = str(
                    preferred_cover_url
                    or user.get("cover_url")
                    or ""
                )
            user["avatar_nsfw"] = avatar_nsfw
            user["cover_nsfw"] = cover_nsfw

        # Fail-closed policy:
        # If the viewer does not allow NSFW media and flags are missing for any reason
        # (stale cache entry, transform mismatch, DB hydration edge case), mask media by default.
        if not show_nsfw_media:
            if "avatar_nsfw" not in user:
                user["avatar_url"] = UserModel.DEFAULT_AVATAR_URL
                user["avatar_nsfw"] = True
            if "cover_nsfw" not in user:
                user["cover_url"] = UserModel.DEFAULT_COVER_URL
                user["cover"] = UserModel._masked_cover(user.get("cover"))
                user["cover_nsfw"] = True

        # Always route through the policy so the Torii default-avatar swap runs
        # for every viewer (apply_nsfw_media_policy no-ops the masking when
        # show_nsfw_media is true).
        row["user"] = UserModel.apply_nsfw_media_policy(user, show_nsfw_media)

    return ranking_rows


class TeamStatistics(BaseModel):
    team_id: int
    ruleset_id: int
    play_count: int
    ranked_score: int
    performance: int

    team: Team
    member_count: int


class TeamResponse(BaseModel):
    ranking: list[TeamStatistics]
    total: int = Field(0, description="æˆ˜é˜Ÿæ€»æ•°")


SortType = Literal["performance", "score"]


@router.get(
    "/rankings/{ruleset}/team",
    name="èŽ·å–æˆ˜é˜ŸæŽ’è¡Œæ¦œ",
    description="èŽ·å–åœ¨æŒ‡å®šæ¨¡å¼ä¸‹æŒ‰ç…§ pp æŽ’åºçš„æˆ˜é˜ŸæŽ’è¡Œæ¦œ",
    tags=["æŽ’è¡Œæ¦œ"],
    response_model=TeamResponse,
)
async def get_team_ranking_pp(
    session: Database,
    background_tasks: BackgroundTasks,
    ruleset: Annotated[GameMode, Path(..., description="æŒ‡å®š ruleset")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    page: Annotated[int, Query(ge=1, description="é¡µç ")] = 1,
):
    return await get_team_ranking(session, background_tasks, "performance", ruleset, current_user, page)


@router.get(
    "/rankings/{ruleset}/team/{sort}",
    response_model=TeamResponse,
    name="èŽ·å–æˆ˜é˜ŸæŽ’è¡Œæ¦œ",
    description="èŽ·å–åœ¨æŒ‡å®šæ¨¡å¼ä¸‹çš„æˆ˜é˜ŸæŽ’è¡Œæ¦œ",
    tags=["æŽ’è¡Œæ¦œ"],
)
async def get_team_ranking(
    session: Database,
    background_tasks: BackgroundTasks,
    sort: Annotated[
        SortType,
        Path(
            ...,
            description="Sort type: performance points / ranked score total "
            "**This parameter is a server extension and is not part of v2 API.**",
        ),
    ],
    ruleset: Annotated[GameMode, Path(..., description="Target ruleset")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
):
    # Keep current auth contract for compatibility.
    _ = current_user

    redis = get_redis()
    cache_service = get_ranking_cache_service(redis)

    cached_data = await cache_service.get_cached_team_ranking(ruleset, sort, page)
    cached_stats = await cache_service.get_cached_team_stats(ruleset, sort)

    if cached_data is not None and cached_stats is not None:
        return TeamResponse(
            ranking=[TeamStatistics.model_validate(item) for item in cached_data],
            total=cached_stats.get("total", 0),
        )

    response = TeamResponse(ranking=[], total=0)
    teams = (await session.exec(select(Team))).all()

    team_memberships = (
        await session.exec(
            select(TeamMember.team_id, TeamMember.user_id).where(
                ~User.is_restricted_query(col(TeamMember.user_id))
            )
        )
    ).all()

    members_by_team: dict[int, set[int]] = {}
    for team_id, user_id in team_memberships:
        members_by_team.setdefault(team_id, set()).add(user_id)

    statistics_rows = (
        await session.exec(
            select(UserStatistics).where(
                UserStatistics.mode == ruleset,
                ~User.is_restricted_query(col(UserStatistics.user_id)),
            )
        )
    ).all()
    stats_by_user = {stat.user_id: stat for stat in statistics_rows}

    ranked_teams: list[TeamStatistics] = []
    for team in teams:
        member_user_ids = members_by_team.get(team.id, set())
        member_count = len(member_user_ids)

        total_pp = 0.0
        total_ranked_score = 0
        total_play_count = 0

        for user_id in member_user_ids:
            stat = stats_by_user.get(user_id)
            if stat is None:
                continue
            total_pp += stat.pp
            total_ranked_score += stat.ranked_score
            total_play_count += stat.play_count

        ranked_teams.append(
            TeamStatistics(
                team_id=team.id,
                ruleset_id=int(ruleset),
                play_count=total_play_count,
                ranked_score=total_ranked_score,
                performance=round(total_pp),
                team=team,
                member_count=member_count,
            )
        )

    if sort == "performance":
        ranked_teams.sort(
            key=lambda x: (x.performance, x.ranked_score, x.member_count, x.team.id),
            reverse=True,
        )
    else:
        ranked_teams.sort(
            key=lambda x: (x.ranked_score, x.performance, x.member_count, x.team.id),
            reverse=True,
        )

    total_count = len(ranked_teams)
    page_size = 50
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    current_page_data = ranked_teams[start_idx:end_idx]

    cache_data = [item.model_dump() for item in current_page_data]
    stats_data = {"total": total_count}

    background_tasks.add_task(
        cache_service.cache_team_ranking,
        ruleset,
        sort,
        cache_data,
        page,
        ttl=settings.ranking_cache_expire_minutes * 60,
    )

    background_tasks.add_task(
        cache_service.cache_team_stats,
        ruleset,
        sort,
        stats_data,
        ttl=settings.ranking_cache_expire_minutes * 60,
    )

    response.ranking = current_page_data
    response.total = total_count
    return response

class CountryStatistics(BaseModel):
    code: str
    active_users: int
    play_count: int
    ranked_score: int
    performance: int


class CountryResponse(BaseModel):
    ranking: list[CountryStatistics]


@router.get(
    "/rankings/{ruleset}/country",
    name="èŽ·å–åœ°åŒºæŽ’è¡Œæ¦œ",
    description="èŽ·å–åœ¨æŒ‡å®šæ¨¡å¼ä¸‹æŒ‰ç…§ pp æŽ’åºçš„åœ°åŒºæŽ’è¡Œæ¦œ",
    tags=["æŽ’è¡Œæ¦œ"],
    response_model=CountryResponse,
)
async def get_country_ranking_pp(
    session: Database,
    background_tasks: BackgroundTasks,
    ruleset: Annotated[GameMode, Path(..., description="æŒ‡å®š ruleset")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    page: Annotated[int, Query(ge=1, description="é¡µç ")] = 1,
):
    return await get_country_ranking(session, background_tasks, ruleset, "performance", current_user, page)


@router.get(
    "/rankings/{ruleset}/country/{sort}",
    response_model=CountryResponse,
    name="èŽ·å–åœ°åŒºæŽ’è¡Œæ¦œ",
    description="èŽ·å–åœ¨æŒ‡å®šæ¨¡å¼ä¸‹çš„åœ°åŒºæŽ’è¡Œæ¦œ",
    tags=["æŽ’è¡Œæ¦œ"],
)
async def get_country_ranking(
    session: Database,
    background_tasks: BackgroundTasks,
    ruleset: Annotated[GameMode, Path(..., description="æŒ‡å®š ruleset")],
    sort: Annotated[
        SortType,
        Path(
            ...,
            description="æŽ’åç±»åž‹ï¼šperformance è¡¨çŽ°åˆ† / score è®¡åˆ†æˆç»©æ€»åˆ† "
            "**è¿™ä¸ªå‚æ•°æ˜¯æœ¬æœåŠ¡å™¨é¢å¤–æ·»åŠ çš„ï¼Œä¸å±žäºŽ v2 API çš„ä¸€éƒ¨åˆ†**",
        ),
    ],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    page: Annotated[int, Query(ge=1, description="é¡µç ")] = 1,
):
    # èŽ·å– Redis è¿žæŽ¥å’Œç¼“å­˜æœåŠ¡
    redis = get_redis()
    cache_service = get_ranking_cache_service(redis)

    # å°è¯•ä»Žç¼“å­˜èŽ·å–æ•°æ®
    cached_data = await cache_service.get_cached_country_ranking(ruleset, page, sort=sort)

    if cached_data:
        # ä»Žç¼“å­˜è¿”å›žæ•°æ®
        return CountryResponse(ranking=[CountryStatistics.model_validate(item) for item in cached_data])

    # ç¼“å­˜æœªå‘½ä¸­ï¼Œä»Žæ•°æ®åº“æŸ¥è¯¢
    response = CountryResponse(ranking=[])
    response.ranking = [
        CountryStatistics.model_validate(item)
        for item in await get_country_ranking_totals(session, ruleset)
    ]

    if sort == "performance":
        response.ranking.sort(key=lambda x: x.performance, reverse=True)
    else:
        response.ranking.sort(key=lambda x: x.ranked_score, reverse=True)

    # åˆ†é¡µå¤„ç†
    page_size = 50
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size

    # èŽ·å–å½“å‰é¡µçš„æ•°æ®
    current_page_data = response.ranking[start_idx:end_idx]

    # å¼‚æ­¥ç¼“å­˜æ•°æ®ï¼ˆä¸ç­‰å¾…å®Œæˆï¼‰
    cache_data = [item.model_dump() for item in current_page_data]

    # åˆ›å»ºåŽå°ä»»åŠ¡æ¥ç¼“å­˜æ•°æ®
    background_tasks.add_task(
        cache_service.cache_country_ranking,
        ruleset,
        cache_data,
        page,
        ttl=settings.ranking_cache_expire_minutes * 60,
        sort=sort,
    )

    # è¿”å›žå½“å‰é¡µçš„ç»“æžœ
    response.ranking = current_page_data
    return response


@router.get(
    "/rankings/{ruleset}/{sort}",
    responses={
        200: api_doc(
            "ç”¨æˆ·æŽ’è¡Œæ¦œ",
            {"ranking": list[UserStatisticsModel], "total": int},
            ["user.country", "user.cover"],
            name="TopUsersResponse",
        )
    },
    name="èŽ·å–ç”¨æˆ·æŽ’è¡Œæ¦œ",
    description="èŽ·å–åœ¨æŒ‡å®šæ¨¡å¼ä¸‹çš„ç”¨æˆ·æŽ’è¡Œæ¦œ",
    tags=["æŽ’è¡Œæ¦œ"],
)
async def get_user_ranking(
    session: Database,
    background_tasks: BackgroundTasks,
    ruleset: Annotated[GameMode, Path(..., description="æŒ‡å®š ruleset")],
    sort: Annotated[SortType, Path(..., description="æŽ’åç±»åž‹ï¼šperformance è¡¨çŽ°åˆ† / score è®¡åˆ†æˆç»©æ€»åˆ†")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    country: Annotated[str | None, Query(description="å›½å®¶ä»£ç ")] = None,
    page: Annotated[int, Query(ge=1, description="é¡µç ")] = 1,
    pp_variant: Annotated[str | None, Query(description="pp variant: stable / pp_dev")] = None,
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    resolved_pp_variant = normalize_pp_variant(pp_variant)
    rank_population: dict[GameMode, int] = {}

    if resolved_pp_variant == "pp_dev":
        redis = get_redis()
        fetcher = await get_fetcher()
        snapshot_full = await get_pp_dev_ranking_snapshot(
            session=session,
            ruleset=ruleset,
            redis=redis,
            fetcher=fetcher,
        )

        global_rank_by_user_id = {int(row["user_id"]): index + 1 for index, row in enumerate(snapshot_full)}

        country_rank_by_user_id: dict[int, int] = {}
        country_counters: dict[str, int] = {}
        for row in snapshot_full:
            row_country = str(row.get("country_code") or "").upper()
            if not row_country:
                continue
            country_counters[row_country] = country_counters.get(row_country, 0) + 1
            country_rank_by_user_id[int(row["user_id"])] = country_counters[row_country]

        snapshot_filtered = snapshot_full
        if country:
            wanted_country = country.upper()
            snapshot_filtered = [row for row in snapshot_full if str(row.get("country_code") or "").upper() == wanted_country]

        if sort == "score":
            snapshot_filtered = sorted(
                snapshot_filtered,
                key=lambda row: (int(row.get("ranked_score") or 0), float(row.get("pp") or 0.0), int(row.get("user_id") or 0)),
                reverse=True,
            )

        total_count = len(snapshot_filtered)
        page_size = 50
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_rows = snapshot_filtered[start_idx:end_idx]

        include = UserStatistics.RANKING_INCLUDES.copy()
        if sort == "performance":
            include.append("rank_change_since_30_days")
        if country:
            include.append("country_rank")

        ranking_data: list[dict[str, Any]] = []
        if page_rows:
            page_user_ids = [int(row["user_id"]) for row in page_rows]
            stats_rows = (
                await session.exec(
                    select(UserStatistics)
                    .where(
                        UserStatistics.mode == ruleset,
                        col(UserStatistics.user_id).in_(page_user_ids),
                        ~User.is_restricted_query(col(UserStatistics.user_id)),
                    )
                )
            ).all()
            stats_by_user_id = {int(stat.user_id): stat for stat in stats_rows}

            for row in page_rows:
                row_user_id = int(row["user_id"])
                statistics = stats_by_user_id.get(row_user_id)
                if statistics is None:
                    continue

                user_stats_resp = await UserStatisticsModel.transform(
                    statistics,
                    includes=include,
                    user_country=current_user.country_code,
                    show_nsfw_media=True,
                    rank_population=rank_population,
                )
                user_stats_resp["pp"] = float(row.get("pp") or 0.0)
                user_stats_resp["hit_accuracy"] = float(row.get("hit_accuracy") or 0.0)
                user_stats_resp["global_rank"] = global_rank_by_user_id.get(row_user_id)

                if country:
                    user_stats_resp["country_rank"] = country_rank_by_user_id.get(row_user_id)

                ranking_data.append(user_stats_resp)

        ranking_data = await _apply_nsfw_policy_to_rankings(session, ranking_data, show_nsfw_media, canonical=True)
        return {
            "ranking": ranking_data,
            "total": total_count,
        }

    # èŽ·å– Redis è¿žæŽ¥å’Œç¼“å­˜æœåŠ¡
    redis = get_redis()
    cache_service = get_ranking_cache_service(redis)

    # å°è¯•ä»Žç¼“å­˜èŽ·å–æ•°æ®
    cached_data = await cache_service.get_cached_ranking(ruleset, sort, country, page)
    cached_stats = await cache_service.get_cached_stats(ruleset, sort, country)

    if cached_data and cached_stats:
        cached_data = await _apply_nsfw_policy_to_rankings(session, cached_data, show_nsfw_media, canonical=True)
        # ä»Žç¼“å­˜è¿”å›žæ•°æ®
        return {
            "ranking": cached_data,
            "total": cached_stats.get("total", 0),
        }

    # ç¼“å­˜æœªå‘½ä¸­ï¼Œä»Žæ•°æ®åº“æŸ¥è¯¢
    wheres = [
        col(UserStatistics.mode) == ruleset,
        col(UserStatistics.pp) > 0,
        col(UserStatistics.is_ranked),
        col(UserStatistics.user).has(is_active=True),
        ~User.is_restricted_query(col(UserStatistics.user_id)),
        # Active-only ranking: 30d+ inactive drop off the board entirely (this
        # filters both total_count and the page slice). Page rank numbers come
        # from get_rank() which applies the same cutoff, so they agree.
        col(UserStatistics.last_played) >= active_cutoff(),
    ]
    include = UserStatistics.RANKING_INCLUDES.copy()
    # Desempate por user_id, el mismo que usa get_rank(): sin esto los pp empatados
    # salen en cualquier orden y contradicen los numeros de las mismas filas.
    if sort == "performance":
        order_by = (col(UserStatistics.pp).desc(), col(UserStatistics.user_id).asc())
        include.append("rank_change_since_30_days")
    else:
        order_by = (col(UserStatistics.ranked_score).desc(), col(UserStatistics.user_id).asc())
    if country:
        wheres.append(col(UserStatistics.user).has(country_code=country.upper()))
        include.append("country_rank")

    # æŸ¥è¯¢æ€»æ•°
    count_query = select(func.count()).select_from(UserStatistics).where(*wheres)
    total_count_result = await session.exec(count_query)
    total_count = total_count_result.one()

    statistics_list = await session.exec(
        select(UserStatistics)
        .where(
            *wheres,
        )
        .order_by(*order_by)
        .limit(50)
        .offset(50 * (page - 1))
    )

    # è½¬æ¢ä¸ºå“åº”æ ¼å¼
    ranking_data = []
    for statistics in statistics_list:
        # OJO: aca NO va user_country=current_user.country_code. Ese era el pais del QUE
        # MIRA, no del dueño de la fila, y este payload se cachea canonico y se le sirve
        # a todo el mundo: el country_rank de cada fila quedaba calculado contra el pais
        # del primer visitante que pisaba el miss. El country_rank correcto se stampea
        # posicional abajo.
        user_stats_resp = await UserStatisticsModel.transform(
            statistics,
            includes=include,
            # Cache canonical (unsanitized) payload; apply viewer policy right before response.
            show_nsfw_media=True,
            rank_population=rank_population,
        )
        ranking_data.append(user_stats_resp)

    # Puesto POSICIONAL del orden de esta misma query (ver el comentario gemelo en
    # ranking_cache_service.refresh_ranking_cache): la pagina queda auto-consistente por
    # construccion, en vez de depender de un get_rank por fila que corre con caches de
    # otros momentos. Solo performance; en las paginas por score la posicion es por score.
    if sort == "performance":
        for idx, user_stats_resp in enumerate(ranking_data):
            pos = 50 * (page - 1) + idx + 1
            if country:
                user_stats_resp["country_rank"] = pos
            else:
                user_stats_resp["global_rank"] = pos

    # å¼‚æ­¥ç¼“å­˜æ•°æ®ï¼ˆä¸ç­‰å¾…å®Œæˆï¼‰
    # ä½¿ç”¨é…ç½®æ–‡ä»¶ä¸­çš„TTLè®¾ç½®
    cache_data = deepcopy(ranking_data)
    stats_data = {"total": total_count}

    # åˆ›å»ºåŽå°ä»»åŠ¡æ¥ç¼“å­˜æ•°æ®
    background_tasks.add_task(
        cache_service.cache_ranking,
        ruleset,
        sort,
        cache_data,
        country,
        page,
        ttl=settings.ranking_cache_expire_minutes * 60,
    )

    # ç¼“å­˜ç»Ÿè®¡ä¿¡æ¯
    background_tasks.add_task(
        cache_service.cache_stats,
        ruleset,
        sort,
        stats_data,
        country,
        ttl=settings.ranking_cache_expire_minutes * 60,
    )

    ranking_data = await _apply_nsfw_policy_to_rankings(session, ranking_data, show_nsfw_media, canonical=True)

    return {
        "ranking": ranking_data,
        "total": total_count,
    }




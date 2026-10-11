from datetime import UTC, datetime
import copy
from typing import Annotated, Any

from app.config import settings
from app.const import BANCHOBOT_ID
from app.database import Team, TeamMember, User
from app.database.user import UserModel
from app.dependencies.database import Database, Redis
from app.dependencies.user import get_optional_user

from .router import router

from fastapi import HTTPException, Query, Security
from pydantic import BaseModel
from sqlmodel import case, col, func, or_, select


class Background(BaseModel):
    """Seasonal background item.
    - url: Image URL."""

    url: str


class BackgroundsResp(BaseModel):
    """Seasonal backgrounds response model.
    - ends_at: Expiration time (far future represents long-term validity).
    - backgrounds: List of backgrounds."""

    ends_at: datetime = datetime(year=9999, month=12, day=31, tzinfo=UTC)
    backgrounds: list[Background]


class SearchUserResult(BaseModel):
    id: int
    username: str
    avatar_url: str
    country_code: str
    is_online: bool
    team_id: int | None = None


class SearchTeamResult(BaseModel):
    id: int
    name: str
    short_name: str
    flag_url: str | None = None
    member_count: int = 0


class NavbarSearchResp(BaseModel):
    query: str
    users: list[SearchUserResult] = []
    teams: list[SearchTeamResult] = []


async def _viewer_allows_nsfw_media(current_user: User | None) -> bool:
    if current_user is None:
        return False
    await current_user.awaitable_attrs.user_preference
    return bool(current_user.user_preference and current_user.user_preference.profile_media_show_nsfw)


@router.get(
    "/seasonal-backgrounds",
    response_model=BackgroundsResp,
    tags=["Misc"],
    name="Get Seasonal Backgrounds List",
    description="Get current seasonal backgrounds list.",
)
async def get_seasonal_backgrounds():
    return BackgroundsResp(backgrounds=[Background(url=url) for url in settings.seasonal_backgrounds])


@router.get(
    "/search",
    tags=["Misc"],
    name="Global Search (navbar + lazer client)",
    description=(
        "Two callers share this endpoint:\n\n"
        "  • Torii navbar quick-search overlay — passes `q` and gets back a "
        "{query, users, teams} payload tuned for the dropdown.\n"
        "  • osu! lazer client (Dashboard → User Search tab) — passes "
        "`mode=user&query=...` and expects the official osu! API shape "
        "{user: {data: [...], total: N}}. Anything other than `mode=user` "
        "currently 400s.\n\n"
        "The two callers are discriminated by the `mode` query parameter: "
        "if it is set we serve the lazer-compatible payload, otherwise we "
        "serve the navbar payload."
    ),
)
async def navbar_search(
    session: Database,
    q: Annotated[str | None, Query(min_length=1, max_length=64, description="Navbar search query")] = None,
    mode: Annotated[str | None, Query(description="Lazer-client search mode (currently only 'user')")] = None,
    query: Annotated[str | None, Query(min_length=1, max_length=64, description="Lazer-client search query")] = None,
    users_limit: Annotated[int, Query(ge=0, le=20, description="Max users to return (navbar only)")] = 6,
    teams_limit: Annotated[int, Query(ge=0, le=20, description="Max teams to return (navbar only)")] = 6,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
) -> Any:
    # ── Lazer-client branch: return the official osu! API shape so the
    # upstream `SearchUsersRequest` / `SearchUsersResponse` round-trip
    # works without any client-side patching.
    if mode is not None:
        if mode != "user":
            # Official osu! API also supports mode=wiki_page; we don't
            # have a wiki, so reject loudly rather than silently returning
            # an empty user list (which would look like "no matches").
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported search mode '{mode}'. Only 'user' is implemented.",
            )
        if not query:
            raise HTTPException(
                status_code=400,
                detail="`query` parameter is required when `mode` is set.",
            )
        keyword = query.strip()
        if not keyword:
            return {"user": {"data": [], "total": 0}, "total": 0}

        keyword_like = f"%{keyword}%"
        keyword_lower = keyword.lower()
        keyword_prefix = f"{keyword_lower}%"
        show_nsfw_media = await _viewer_allows_nsfw_media(current_user)

        users_stmt = (
            select(User)
            .where(
                col(User.id) != BANCHOBOT_ID,
                ~User.is_restricted_query(col(User.id)),
                col(User.username).ilike(keyword_like),
            )
            .order_by(
                case(
                    (func.lower(col(User.username)) == keyword_lower, 0),
                    (func.lower(col(User.username)).like(keyword_prefix), 1),
                    else_=2,
                ),
                func.length(col(User.username)),
                col(User.id).desc(),
            )
            .limit(50)  # match the official osu! API per-page limit
        )
        matched = (await session.exec(users_stmt)).all()
        user_payloads: list[dict[str, Any]] = []
        for user in matched:
            canonical = await UserModel.transform(
                user,
                includes=User.CARD_INCLUDES,
                show_nsfw_media=True,
            )
            safe = UserModel.apply_nsfw_media_policy(copy.deepcopy(canonical), show_nsfw_media)
            user_payloads.append(safe)

        return {
            # The lazer SearchUsersResponse class reads top-level `total`
            # (legacy / unused by UI) AND `user.data` (the actual list).
            # We mirror both for forward compatibility.
            "total": len(user_payloads),
            "user": {"data": user_payloads, "total": len(user_payloads)},
        }

    # ── Navbar branch: original behaviour, requires `q`.
    if q is None:
        raise HTTPException(
            status_code=400,
            detail="Either `q` (navbar) or `mode=user&query=...` (lazer client) must be provided.",
        )
    keyword = q.strip()
    if not keyword:
        return NavbarSearchResp(query="", users=[], teams=[])

    keyword_like = f"%{keyword}%"
    keyword_lower = keyword.lower()
    keyword_prefix = f"{keyword_lower}%"
    show_nsfw_media = await _viewer_allows_nsfw_media(current_user)

    users: list[SearchUserResult] = []
    teams: list[SearchTeamResult] = []

    if users_limit > 0:
        users_stmt = (
            select(User)
            .where(
                col(User.id) != BANCHOBOT_ID,
                ~User.is_restricted_query(col(User.id)),
                col(User.username).ilike(keyword_like),
            )
            .order_by(
                case(
                    (func.lower(col(User.username)) == keyword_lower, 0),
                    (func.lower(col(User.username)).like(keyword_prefix), 1),
                    else_=2,
                ),
                func.length(col(User.username)),
                col(User.id).desc(),
            )
            .limit(users_limit)
        )
        matched_users = (await session.exec(users_stmt)).all()
        for user in matched_users:
            canonical_user = await UserModel.transform(
                user,
                includes=User.CARD_INCLUDES,
                show_nsfw_media=True,
            )
            safe_user = UserModel.apply_nsfw_media_policy(copy.deepcopy(canonical_user), show_nsfw_media)
            team_data = safe_user.get("team")
            users.append(
                SearchUserResult(
                    id=safe_user["id"],
                    username=safe_user["username"],
                    avatar_url=safe_user.get("avatar_url") or UserModel.DEFAULT_AVATAR_URL,
                    country_code=safe_user.get("country_code") or "XX",
                    is_online=bool(safe_user.get("is_online")),
                    team_id=team_data.get("id") if isinstance(team_data, dict) else None,
                )
            )

    if teams_limit > 0:
        teams_stmt = (
            select(Team)
            .where(
                or_(
                    col(Team.name).ilike(keyword_like),
                    col(Team.short_name).ilike(keyword_like),
                )
            )
            .order_by(
                case(
                    (func.lower(col(Team.name)) == keyword_lower, 0),
                    (func.lower(col(Team.name)).like(keyword_prefix), 1),
                    (func.lower(col(Team.short_name)).like(keyword_prefix), 1),
                    else_=2,
                ),
                func.length(col(Team.name)),
                col(Team.id).desc(),
            )
            .limit(teams_limit)
        )
        matched_teams = (await session.exec(teams_stmt)).all()
        team_ids = [team.id for team in matched_teams]
        member_counts: dict[int, int] = {}
        if team_ids:
            count_rows = (
                await session.exec(
                    select(TeamMember.team_id, func.count(col(TeamMember.user_id)))
                    .where(col(TeamMember.team_id).in_(team_ids))
                    .group_by(TeamMember.team_id)
                )
            ).all()
            member_counts = {team_id: int(count or 0) for team_id, count in count_rows}

        teams = [
            SearchTeamResult(
                id=team.id,
                name=team.name,
                short_name=team.short_name,
                flag_url=team.flag_url,
                member_count=member_counts.get(team.id, 0),
            )
            for team in matched_teams
        ]

    return NavbarSearchResp(query=keyword, users=users, teams=teams)


# ─────────────────────────────────────────────────────────────────────
# Server status — minimal public endpoint that the frontend polls (or
# calls at boot) to show a maintenance banner. Returns just enough for
# a banner: a boolean and an optional message. We deliberately do NOT
# expose the actor identity or timestamp here — those are admin-tier
# audit info and live behind the /api/private/admin/maintenance route.
# Cheap (single Redis HGET inside is_active() / get_state()) so it's
# safe to hit from the splash page on every navigation.
# ─────────────────────────────────────────────────────────────────────


class ServerStatusResp(BaseModel):
    """Public server-status payload. Stable contract for clients
    rendering banners — fields will be added but never renamed."""

    maintenance: bool
    message: str | None = None


@router.get(
    "/server/status",
    name="Server Status",
    description="Lightweight public status endpoint used by clients to surface maintenance banners.",
    response_model=ServerStatusResp,
)
async def get_server_status(redis: Redis):
    from app.service.maintenance_mode import get_state, to_public_dict
    state = await get_state(redis)
    return ServerStatusResp(**to_public_dict(state))

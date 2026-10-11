from datetime import datetime
from typing import Annotated, Any

from app.auth import validate_username
from app.database import User
from app.database.user_preference import (
    DEFAULT_ORDER,
    BeatmapCardSize,
    BeatmapDownload,
    ScoringMode,
    UserListFilter,
    UserListSort,
    UserListView,
    UserPreference,
)
from app.database.username_change_request import STATUS_PENDING, UsernameChangeRequest
from app.dependencies.cache import UserCacheService
from app.dependencies.database import Database
from app.dependencies.user import ClientUser
from app.models.score import GameMode
from app.utils import utcnow
from app.models.user import Page
from app.models.userpage import (
    UpdateUserpageRequest,
    UpdateUserpageResponse,
    UserpageError,
    ValidateBBCodeRequest,
    ValidateBBCodeResponse,
)
from app.service.bbcode_service import bbcode_service
from app.utils import hex_to_hue

from .router import router

from fastapi import Body, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import col, exists, select


class UsernameChangeRequestResp(BaseModel):
    id: int
    current_username: str
    requested_username: str
    status: str
    created_at: datetime
    reject_reason: str | None = None


def _username_request_to_resp(request: UsernameChangeRequest) -> UsernameChangeRequestResp:
    return UsernameChangeRequestResp(
        id=request.id or 0,
        current_username=request.current_username,
        requested_username=request.requested_username,
        status=request.status,
        created_at=request.created_at,
        reject_reason=request.reject_reason,
    )


@router.post(
    "/rename",
    name="Request Username Change",
    tags=["Users", "g0v0 API"],
    response_model=UsernameChangeRequestResp,
)
async def user_rename(
    session: Database,
    new_name: Annotated[str, Body(..., description="New username")],
    current_user: ClientUser,
):
    """Request Username Change

    Does not immediately change username, but creates a pending request for admin review. Admins can approve or
    reject the request in the web admin panel.

    Errors:
    - 403: Account is banned, or new username failed validation
    - 409: New username already taken, same as current username, or pending request already exists
    """
    if await current_user.is_restricted(session):
        # https://github.com/ppy/osu-web/blob/cae2fdf03cfb8c30c8e332cfb142e03188ceffef/app/Libraries/ChangeUsername.php#L48-L49
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")

    new_name = new_name.strip()
    if new_name == current_user.username:
        raise HTTPException(409, "That is already your username.")

    errors = validate_username(new_name)
    if errors:
        raise HTTPException(403, "\n".join(errors))

    samename_user = (await session.exec(select(exists()).where(col(User.username) == new_name))).first()
    if samename_user:
        raise HTTPException(409, "Username Exists")

    existing_pending = (
        await session.exec(
            select(UsernameChangeRequest).where(
                col(UsernameChangeRequest.user_id) == current_user.id,
                col(UsernameChangeRequest.status) == STATUS_PENDING,
            )
        )
    ).first()
    if existing_pending is not None:
        raise HTTPException(409, "You already have a pending username change request.")

    request = UsernameChangeRequest(
        user_id=current_user.id,
        current_username=current_user.username,
        requested_username=new_name,
        status=STATUS_PENDING,
    )
    session.add(request)
    await session.commit()
    await session.refresh(request)

    return _username_request_to_resp(request)


@router.get(
    "/username-change-request",
    name="Get Pending Username Change Requests",
    tags=["Users", "g0v0 API"],
    response_model=UsernameChangeRequestResp | None,
)
async def get_my_username_change_request(
    session: Database,
    current_user: ClientUser,
):
    """La pendiente si hay, si no la ultima resuelta que el usuario todavia no vio."""
    request = (
        await session.exec(
            select(UsernameChangeRequest)
            .where(
                col(UsernameChangeRequest.user_id) == current_user.id,
                col(UsernameChangeRequest.status) == STATUS_PENDING,
            )
            .order_by(col(UsernameChangeRequest.created_at).desc())
        )
    ).first()

    if request is None:
        request = (
            await session.exec(
                select(UsernameChangeRequest)
                .where(
                    col(UsernameChangeRequest.user_id) == current_user.id,
                    col(UsernameChangeRequest.status) != STATUS_PENDING,
                    col(UsernameChangeRequest.acknowledged_at).is_(None),
                )
                .order_by(col(UsernameChangeRequest.created_at).desc())
            )
        ).first()

    if request is None:
        return None
    return _username_request_to_resp(request)


@router.post(
    "/username-change-request/acknowledge",
    name="marcar como visto el resultado del cambio de nombre",
    tags=["Users", "g0v0 API"],
)
async def acknowledge_username_change_request(
    session: Database,
    current_user: ClientUser,
):
    pendientes = (
        await session.exec(
            select(UsernameChangeRequest).where(
                col(UsernameChangeRequest.user_id) == current_user.id,
                col(UsernameChangeRequest.status) != STATUS_PENDING,
                col(UsernameChangeRequest.acknowledged_at).is_(None),
            )
        )
    ).all()

    for r in pendientes:
        r.acknowledged_at = utcnow()
        session.add(r)
    await session.commit()

    return {"acknowledged": len(pendientes)}


class UserSelfUpdate(BaseModel):
    country_code: str | None = None
    # Add other fields here if needed in the future, matching Admin's flexibility
    # username: str | None = None


@router.patch("/me", name="Update Profile", tags=["Users", "g0v0 API"])
@router.patch("/user/country", include_in_schema=False) # Keep legacy path for now but redirect logic
async def update_user_profile(
    session: Database,
    request: UserSelfUpdate,
    current_user: ClientUser,
    cache_service: UserCacheService,
):
    """Update user profile (Self) - Logic mirrored from Admin Panel"""

    if await current_user.is_restricted(session):
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")

    if request.country_code is not None:
        # Match Admin Panel logic: Direct assignment
        # Admin logic: user.country_code = user_data.country_code

        # We still need basic validation because unlike admin, user input is untrusted
        country_code = request.country_code.upper()
        if not country_code or len(country_code) != 2:
             raise HTTPException(400, "Invalid country code format.")

        from app.database.user import COUNTRIES
        if country_code not in COUNTRIES:
            raise HTTPException(400, f"Invalid country code: {country_code}")

        current_user.country_code = country_code

    # Match Admin logic: Commit and Refresh
    await session.commit()
    await session.refresh(current_user)

    # Invalidate cache (Good practice, even if Admin doesn't explicit it in the snippet,
    # self-update should reflect immediately)
    await cache_service.invalidate_user_cache(current_user.id)
    await cache_service.invalidate_v1_user_cache(current_user.id)

    # Return full user model for Frontend state
    me_includes = [*User.USER_INCLUDES, "session_verified", "session_verification_method", "user_preferences"]
    user_resp = await User.transform(current_user, session=session, includes=me_includes)
    return user_resp


@router.put(
    "/user/page",
    response_model=UpdateUserpageResponse,
    name="Update User Page",
    description="Update profile page content for the specified user (supports BBCode). Matches official osu-web API format.",
    tags=["Users", "g0v0 API"],
)
async def update_userpage(
    request: UpdateUserpageRequest,
    session: Database,
    current_user: ClientUser,
    cache_service: UserCacheService,
):
    """Update user page content"""
    if await current_user.is_restricted(session):
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")

    try:
        # Process BBCode content
        processed_page = bbcode_service.process_userpage_content(request.body)

        # Update database - directly update user object
        current_user.page = Page(html=processed_page["html"], raw=processed_page["raw"])
        session.add(current_user)
        await session.commit()
        await session.refresh(current_user)

        await cache_service.invalidate_user_cache(current_user.id)

        # Return official format response: only contains html
        return UpdateUserpageResponse(html=processed_page["html"])

    except UserpageError as e:
        # Use official format error response: {'error': message}
        raise HTTPException(status_code=422, detail={"error": e.message})
    except Exception:
        raise HTTPException(status_code=500, detail={"error": "Failed to update user page"})


@router.post(
    "/user/validate-bbcode",
    response_model=ValidateBBCodeResponse,
    name="Validate BBCode",
    description="Validate BBCode syntax and return preview.",
    tags=["Users", "g0v0 API"],
)
async def validate_bbcode(
    request: ValidateBBCodeRequest,
):
    """Validate BBCode syntax"""
    try:
        # Validate BBCode syntax
        errors = bbcode_service.validate_bbcode(request.content)

        # Generate preview if there are no errors
        if len(errors) == 0:
            preview = bbcode_service.process_userpage_content(request.content)
        else:
            preview = {"raw": request.content, "html": ""}

        return ValidateBBCodeResponse(valid=len(errors) == 0, errors=errors, preview=preview)

    except UserpageError as e:
        return ValidateBBCodeResponse(valid=False, errors=[e.message], preview={"raw": request.content, "html": ""})
    except Exception:
        raise HTTPException(status_code=500, detail={"error": "Failed to validate BBCode"})


class Preferences(BaseModel):
    theme: str | None = None
    language: str | None = None

    audio_autoplay: bool | None = None
    audio_muted: bool | None = None
    audio_volume: float | None = Field(None, ge=0.0, le=1.0)
    beatmapset_card_size: BeatmapCardSize | None = None
    beatmap_download: BeatmapDownload | None = None
    beatmapset_show_nsfw: bool | None = None
    profile_media_show_nsfw: bool | None = None
    profile_order: list[str] | None = None
    legacy_score_only: bool | None = None
    profile_cover_expanded: bool | None = None
    scoring_mode: ScoringMode | None = None
    user_list_filter: UserListFilter | None = None
    user_list_sort: UserListSort | None = None
    user_list_view: UserListView | None = None
    extra: dict[str, Any] | None = None

    # in User
    playmode: GameMode | None = None
    interests: str | None = None
    location: str | None = None
    occupation: str | None = None
    twitter: str | None = None
    website: str | None = None
    discord: str | None = None
    profile_colour: str | None = None

    @staticmethod
    async def clear(current_user: User, fields: list[str]):
        await current_user.awaitable_attrs.user_preference
        user_pref: UserPreference | None = current_user.user_preference
        if user_pref is None:
            return
        if len(fields) == 0:
            fields = [
                *PREFERENCE_FIELDS,
                *USER_PROFILE_FIELDS_WITH_WEBSITE,
                "profile_order",
                "extra",
                "playmode",
                "profile_colour",
            ]

        for field in fields:
            if field in PREFERENCE_FIELDS:
                setattr(user_pref, field, UserPreference.model_fields[field].default)
            elif field == "profile_order":
                user_pref.extras_order = DEFAULT_ORDER
            elif field == "extra":
                user_pref.extra = {}

        for field in fields:
            if field in USER_PROFILE_FIELDS_WITH_WEBSITE:
                setattr(current_user, field, None)
            elif field == "playmode":
                current_user.playmode = GameMode.OSU
                current_user.g0v0_playmode = GameMode.OSU
            elif field == "profile_colour":
                current_user.profile_colour = None
                current_user.profile_hue = None


PREFERENCE_FIELDS = {
    "theme",
    "language",
    "audio_autoplay",
    "audio_muted",
    "audio_volume",
    "beatmapset_card_size",
    "beatmap_download",
    "beatmapset_show_nsfw",
    "profile_media_show_nsfw",
    "legacy_score_only",
    "profile_cover_expanded",
    "scoring_mode",
    "user_list_filter",
    "user_list_sort",
    "user_list_view",
}

USER_PROFILE_FIELDS = {
    "interests",
    "location",
    "occupation",
    "twitter",
    "discord",
}

USER_PROFILE_FIELDS_WITH_WEBSITE = USER_PROFILE_FIELDS | {"website"}


@router.get(
    "/user/preferences",
    name="Get User Preferences",
    description="Get preferences for current logged-in user",
    tags=["Users", "g0v0 API"],
    response_model=Preferences,
)
async def get_user_preference(
    current_user: ClientUser,
):
    await current_user.awaitable_attrs.user_preference
    user_pref: UserPreference | None = current_user.user_preference
    if user_pref is None:
        user_pref = UserPreference(user_id=current_user.id)

    return Preferences(
        theme=user_pref.theme,
        language=user_pref.language,
        audio_autoplay=user_pref.audio_autoplay,
        audio_muted=user_pref.audio_muted,
        audio_volume=user_pref.audio_volume,
        beatmapset_card_size=user_pref.beatmapset_card_size,
        beatmap_download=user_pref.beatmap_download,
        beatmapset_show_nsfw=user_pref.beatmapset_show_nsfw,
        profile_media_show_nsfw=user_pref.profile_media_show_nsfw,
        profile_order=user_pref.extras_order or DEFAULT_ORDER,
        legacy_score_only=user_pref.legacy_score_only,
        profile_cover_expanded=user_pref.profile_cover_expanded,
        scoring_mode=user_pref.scoring_mode,
        user_list_filter=user_pref.user_list_filter,
        user_list_sort=user_pref.user_list_sort,
        user_list_view=user_pref.user_list_view,
        extra=user_pref.extra or {},
        playmode=current_user.g0v0_playmode,
        interests=current_user.interests,
        location=current_user.location,
        occupation=current_user.occupation,
        twitter=current_user.twitter,
        website=current_user.website,
        discord=current_user.discord,
        profile_colour="#" + current_user.profile_colour if current_user.profile_colour else None,
    )


@router.patch(
    "/user/preferences",
    name="Modify User Preferences",
    description="Modify preferences for current logged-in user",
    tags=["Users", "g0v0 API"],
    status_code=204,
)
async def change_user_preference(
    request: Preferences,
    session: Database,
    current_user: ClientUser,
    cache_service: UserCacheService,
):
    if await current_user.is_restricted(session):
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")

    await current_user.awaitable_attrs.user_preference
    user_pref: UserPreference | None = current_user.user_preference
    if user_pref is None:
        user_pref = UserPreference(user_id=current_user.id)
        session.add(user_pref)

    for field, value in request.model_dump(include=PREFERENCE_FIELDS, exclude_none=True).items():
        setattr(user_pref, field, value)

    if request.profile_order is not None:
        if set(request.profile_order) != set(DEFAULT_ORDER):
            raise HTTPException(400, "Invalid profile order")
        user_pref.extras_order = request.profile_order

    if request.extra is not None:
        user_pref.extra = (user_pref.extra or {}) | request.extra

    if request.playmode is not None:
        if request.playmode.is_official():
            current_user.playmode = request.playmode.to_base_ruleset()
        current_user.g0v0_playmode = request.playmode

    for field, value in request.model_dump(include=USER_PROFILE_FIELDS, exclude_none=True).items():
        setattr(current_user, field, value or None)

    if request.website is not None:
        if request.website == "":
            current_user.website = None
        elif not (request.website.startswith("http://") or request.website.startswith("https://")):
            current_user.website = "https://" + request.website

    if request.profile_colour is not None:
        current_user.profile_colour = request.profile_colour.removeprefix("#")
        try:
            current_user.profile_hue = hex_to_hue(request.profile_colour)
        except ValueError:
            raise HTTPException(400, "Invalid profile colour hex value")

    await cache_service.invalidate_user_cache(current_user.id)
    await session.commit()


@router.put(
    "/user/preferences",
    name="Overwrite User Preferences",
    description="Completely overwrite preferences for current logged-in user with provided data; omitted fields reset to defaults.",
    tags=["Users", "g0v0 API"],
    status_code=204,
)
async def overwrite_user_preference(
    request: Preferences,
    session: Database,
    current_user: ClientUser,
    cache_service: UserCacheService,
):
    if await current_user.is_restricted(session):
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")

    await Preferences.clear(current_user, [])
    await change_user_preference(request, session, current_user, cache_service)

    await cache_service.invalidate_user_cache(current_user.id)
    await session.commit()


@router.delete(
    "/user/preferences",
    name="Delete User Preferences",
    description="Delete preferences for current logged-in user and reset to defaults.\n\nIf no fields specified, deletes all deletable preferences.",
    tags=["Users", "g0v0 API"],
    status_code=204,
)
async def delete_user_preference(
    session: Database,
    current_user: ClientUser,
    fields: list[str],
    cache_service: UserCacheService,
):
    if await current_user.is_restricted(session):
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")

    await Preferences.clear(current_user, fields)

    await cache_service.invalidate_user_cache(current_user.id)
    await session.commit()

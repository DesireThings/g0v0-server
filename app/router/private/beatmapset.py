from typing import Annotated

from app.database.beatmap import Beatmap
from app.database.beatmapset import Beatmapset
from app.database.beatmapset_ratings import BeatmapRating
from app.database.score import Score
from app.dependencies.database import Database
from app.dependencies.user import ClientUser
from app.service.beatmapset_update_service import get_beatmapset_update_service

from .router import router

from fastapi import Body, Depends, HTTPException, Path, Query
from fastapi_limiter.depends import RateLimiter
from sqlmodel import col, exists, select


@router.get(
    "/beatmapsets/{beatmapset_id}/can_rate",
    name="Check If User Can Rate Beatmapset",
    response_model=bool,
    tags=["Beatmapsets", "g0v0 API"],
)
async def can_rate_beatmapset(
    beatmapset_id: int,
    session: Database,
    current_user: ClientUser,
):
    """Check if user can rate beatmapset

    Checks whether the current user is eligible to rate the specified beatmapset.

    Parameters:
    - beatmapset_id: Beatmapset ID

    Error cases:
    - 404: Beatmapset not found

    Returns:
    - bool: Whether the user can rate the beatmapset
    """
    if await current_user.is_restricted(session):
        return False

    user_id = current_user.id
    prev_ratings = (await session.exec(select(BeatmapRating).where(BeatmapRating.user_id == user_id))).first()
    if prev_ratings is not None:
        return False
    query = select(exists()).where(
        Score.user_id == user_id,
        col(Score.beatmap).has(col(Beatmap.beatmapset_id) == beatmapset_id),
        col(Score.passed).is_(True),
    )
    return (await session.exec(query)).first() or False


@router.post(
    "/beatmapsets/{beatmapset_id}/ratings", name="Submit Beatmapset Rating", status_code=201, tags=["Beatmapsets", "g0v0 API"]
)
async def rate_beatmaps(
    beatmapset_id: int,
    session: Database,
    rating: Annotated[int, Body(..., ge=0, le=10)],
    current_user: ClientUser,
):
    """Rate a beatmapset

    Adds a user rating for the specified beatmapset and updates its rating statistics.

    Parameters:
    - beatmapset_id: Beatmapset ID
    - rating: Rating value

    Error cases:
    - 404: Beatmapset not found

    Returns:
    - Success: None
    """
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    user_id = current_user.id
    current_beatmapset = (await session.exec(select(exists()).where(Beatmapset.id == beatmapset_id))).first()
    if not current_beatmapset:
        raise HTTPException(404, "Beatmapset Not Found")
    can_rating = await can_rate_beatmapset(beatmapset_id, session, current_user)
    if not can_rating:
        raise HTTPException(403, "User Cannot Rate This Beatmapset")
    new_rating: BeatmapRating = BeatmapRating(beatmapset_id=beatmapset_id, user_id=user_id, rating=rating)
    session.add(new_rating)
    await session.commit()


@router.post(
    "/beatmapsets/{beatmapset_id}/sync",
    name="Request Beatmapset Sync",
    status_code=202,
    tags=["Beatmapsets", "g0v0 API"],
    dependencies=[Depends(RateLimiter(times=50, hours=1))],
)
async def sync_beatmapset(
    beatmapset_id: Annotated[int, Path(..., description="Beatmapset ID")],
    session: Database,
    current_user: ClientUser,
    immediate: Annotated[bool, Query(description="Whether to sync immediately")] = False,
):
    """Request beatmapset synchronization

    Requests syncing the specified beatmapset from Bancho to the server.

    By default, the request joins the synchronization queue for automatic processing.
    If `immediate=true`, attempts to sync the beatmapset immediately.

    Rate limits:
    - Max 50 requests per hour per user

    Parameters:
    - beatmapset_id: Beatmapset ID
    - immediate: Whether to sync immediately (default false)

    Error cases:
    - 404: Beatmapset not found
    """
    current_beatmapset = (await session.exec(select(exists()).where(Beatmapset.id == beatmapset_id))).first()
    if not current_beatmapset:
        raise HTTPException(404, "Beatmapset Not Found")
    await get_beatmapset_update_service().add_missing_beatmapset(beatmapset_id, immediate)

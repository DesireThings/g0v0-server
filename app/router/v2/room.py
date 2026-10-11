from datetime import UTC
from typing import Annotated, Literal

from app.database.beatmap import (
    Beatmap,
    BeatmapModel,
)
from app.database.beatmapset import BeatmapsetModel
from app.database.item_attempts_count import ItemAttemptsCount, ItemAttemptsCountModel
from app.database.multiplayer_event import MultiplayerEvent, MultiplayerEventResp
from app.database.playlists import Playlist, PlaylistModel
from app.database.room import APIUploadedRoom, Room, RoomModel
from app.database.room_participated_user import RoomParticipatedUser
from app.database.score import Score
from app.database.user import User, UserModel
from app.dependencies.database import Database, Redis
from app.dependencies.user import ClientUser, get_current_user
from app.models.room import MatchType, RoomCategory, RoomStatus
from app.service.room import create_playlist_room_from_api
from app.utils import api_doc, utcnow

from .router import router

from fastapi import HTTPException, Path, Query, Security
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col, exists, select
from sqlmodel.ext.asyncio.session import AsyncSession


@router.get(
    "/rooms",
    tags=["Rooms"],
    responses={
        200: api_doc(
            "Room list",
            list[RoomModel],
            [
                "current_playlist_item.beatmap.beatmapset",
                "difficulty_range",
                "host.country",
                "playlist_item_stats",
                "recent_participants",
            ],
        )
    },
    name="Get Room List",
    description="Get room list. Supports filtering by status/mode",
)
async def get_all_rooms(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    mode: Annotated[
        Literal["open", "ended", "participated", "owned"] | None,
        Query(
            description=("Room mode: open (currently open) / ended (finished) / participated (previously joined) / owned (created by self)"),
        ),
    ] = "open",
    category: Annotated[
        RoomCategory,
        Query(
            description=("Room category: NORMAL (playlist room) / REALTIME (multiplayer room) / DAILY_CHALLENGE (daily challenge)"),
        ),
    ] = RoomCategory.NORMAL,
    status: Annotated[RoomStatus | None, Query(description="Room status (optional)")] = None,
):
    resp_list = []
    where_clauses: list[ColumnElement[bool]] = [col(Room.category) == category, col(Room.type) != MatchType.MATCHMAKING]
    now = utcnow()

    if status is not None:
        where_clauses.append(col(Room.status) == status)
    if mode == "open":
        where_clauses.extend(
            [
                col(Room.status).in_([RoomStatus.IDLE, RoomStatus.PLAYING]),
                col(Room.starts_at).is_not(None),
                col(Room.ends_at).is_(None) if category == RoomCategory.REALTIME else col(Room.ends_at) > now,
            ]
        )

    if mode == "participated":
        where_clauses.append(
            exists().where(
                col(RoomParticipatedUser.room_id) == Room.id,
                col(RoomParticipatedUser.user_id) == current_user.id,
            )
        )

    if mode == "owned":
        where_clauses.append(col(Room.host_id) == current_user.id)

    if mode == "ended":
        where_clauses.append((col(Room.ends_at).is_not(None)) & (col(Room.ends_at) < now.replace(tzinfo=UTC)))

    db_rooms = (
        (
            await db.exec(
                select(Room).where(
                    *where_clauses,
                )
            )
        )
        .unique()
        .all()
    )
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    for room in db_rooms:
        resp = await RoomModel.transform(
            room,
            includes=[
                "current_playlist_item.beatmap.beatmapset",
                "difficulty_range",
                "host.country",
                "playlist_item_stats",
                "recent_participants",
            ],
            show_nsfw_media=show_nsfw_media,
        )
        if category == RoomCategory.REALTIME:
            resp["category"] = RoomCategory.NORMAL

        resp_list.append(resp)

    return resp_list


async def _participate_room(room_id: int, user_id: int, db_room: Room, session: AsyncSession, redis: Redis):
    participated_user = (
        await session.exec(
            select(RoomParticipatedUser).where(
                RoomParticipatedUser.room_id == room_id,
                RoomParticipatedUser.user_id == user_id,
            )
        )
    ).first()
    if participated_user is None:
        participated_user = RoomParticipatedUser(
            room_id=room_id,
            user_id=user_id,
            joined_at=utcnow(),
        )
        session.add(participated_user)
    else:
        participated_user.left_at = None
        participated_user.joined_at = utcnow()
    db_room.participant_count += 1

    await redis.publish("chat:room:joined", f"{db_room.channel_id}:{user_id}")


@router.post(
    "/rooms",
    tags=["Rooms"],
    name="Create Room",
    description="\nCreate a new room.",
    responses={
        200: api_doc(
            "Created room info",
            RoomModel,
            Room.SHOW_RESPONSE_INCLUDES,
        )
    },
)
async def create_room(
    db: Database,
    room: APIUploadedRoom,
    current_user: ClientUser,
    redis: Redis,
):
    if await current_user.is_restricted(db):
        raise HTTPException(status_code=403, detail="Your account is restricted from multiplayer.")
    user_id = current_user.id
    db_room = await create_playlist_room_from_api(db, room, user_id)

    # snapshot pre-commit para el #feed (expire_on_commit expira los objetos)
    feed_room_name = db_room.name
    feed_category = str(getattr(db_room.category, "value", db_room.category) or "")
    feed_map_count = len(db_room.playlist or [])
    feed_has_password = bool(db_room.password)
    feed_host_name = current_user.username

    await _participate_room(db_room.id, user_id, db_room, db, redis)
    await db.commit()

    # evento al #feed de discord: lobbies de multi y playlists creadas por users
    # (los rooms de sistema tipo daily challenge/matchmaking no pasan por aca o
    # quedan filtrados por categoria)
    try:
        from app.service.discord_feed import notify_room_created

        if feed_category in ("NORMAL", "REALTIME"):
            notify_room_created(
                host_username=feed_host_name,
                host_user_id=user_id,
                room_name=feed_room_name,
                is_realtime=feed_category == "REALTIME",
                map_count=feed_map_count,
                has_password=feed_has_password,
            )
    except Exception:
        pass
    await db.refresh(db_room)
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    created_room = await RoomModel.transform(db_room, includes=Room.SHOW_RESPONSE_INCLUDES, show_nsfw_media=show_nsfw_media)
    return created_room


@router.get(
    "/rooms/{room_id}",
    tags=["Rooms"],
    responses={
        200: api_doc(
            "Room details",
            RoomModel,
            Room.SHOW_RESPONSE_INCLUDES,
        )
    },
    name="Get Room Details",
    description="Get details for the specified room.",
)
async def get_room(
    db: Database,
    room_id: Annotated[int, Path(..., description="Room ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    category: Annotated[
        str,
        Query(
            description=("Room category: NORMAL (playlist room) / REALTIME (multiplayer room) / DAILY_CHALLENGE (daily challenge) (optional)"),
        ),
    ] = "",
):
    db_room = (await db.exec(select(Room).where(Room.id == room_id))).first()
    if db_room is None:
        raise HTTPException(404, "Room not found")
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    resp = await RoomModel.transform(db_room, includes=Room.SHOW_RESPONSE_INCLUDES, show_nsfw_media=show_nsfw_media)
    return resp


@router.delete(
    "/rooms/{room_id}",
    tags=["Rooms"],
    name="End Room",
    description="\nEnd playlist room.",
)
async def delete_room(
    db: Database,
    room_id: Annotated[int, Path(..., description="Room ID")],
    current_user: ClientUser,
):
    if await current_user.is_restricted(db):
        raise HTTPException(status_code=403, detail="Your account is restricted from multiplayer.")

    db_room = (await db.exec(select(Room).where(Room.id == room_id))).first()
    if db_room is None:
        raise HTTPException(404, "Room not found")
    else:
        db_room.ends_at = utcnow()
        await db.commit()
        return None


@router.put(
    "/rooms/{room_id}/users/{user_id}",
    tags=["Rooms"],
    name="Join Room",
    description="\nJoin specified playlist room.",
)
async def add_user_to_room(
    db: Database,
    room_id: Annotated[int, Path(..., description="Room ID")],
    user_id: Annotated[int, Path(..., description="User ID")],
    redis: Redis,
    current_user: ClientUser,
):
    if await current_user.is_restricted(db):
        raise HTTPException(status_code=403, detail="Your account is restricted from multiplayer.")

    db_room = (await db.exec(select(Room).where(Room.id == room_id))).first()
    if db_room is not None:
        await _participate_room(room_id, user_id, db_room, db, redis)
        await db.commit()
        await db.refresh(db_room)
        show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
        resp = await RoomModel.transform(db_room, includes=Room.SHOW_RESPONSE_INCLUDES, show_nsfw_media=show_nsfw_media)
        return resp
    else:
        raise HTTPException(404, "room not found")


@router.delete(
    "/rooms/{room_id}/users/{user_id}",
    tags=["Rooms"],
    name="Leave Room",
    description="\nLeave specified playlist room.",
)
async def remove_user_from_room(
    db: Database,
    room_id: Annotated[int, Path(..., description="Room ID")],
    user_id: Annotated[int, Path(..., description="User ID")],
    current_user: ClientUser,
    redis: Redis,
):
    if await current_user.is_restricted(db):
        raise HTTPException(status_code=403, detail="Your account is restricted from multiplayer.")

    db_room = (await db.exec(select(Room).where(Room.id == room_id))).first()
    if db_room is not None:
        participated_user = (
            await db.exec(
                select(RoomParticipatedUser).where(
                    RoomParticipatedUser.room_id == room_id,
                    RoomParticipatedUser.user_id == user_id,
                )
            )
        ).first()
        if participated_user is not None:
            participated_user.left_at = utcnow()
        if db_room.participant_count > 0:
            db_room.participant_count -= 1
        await redis.publish("chat:room:left", f"{db_room.channel_id}:{user_id}")
        await db.commit()
        return None
    else:
        raise HTTPException(404, "Room not found")


@router.get(
    "/rooms/{room_id}/leaderboard",
    tags=["Rooms"],
    name="Get Room Leaderboard",
    description="Get cumulative score leaderboard for the room.",
    responses={
        200: api_doc(
            "Room leaderboard",
            {
                "leaderboard": list[ItemAttemptsCountModel],
                "user_score": ItemAttemptsCountModel | None,
            },
            ["user.country", "position"],
            name="RoomLeaderboardResponse",
        )
    },
)
async def get_room_leaderboard(
    db: Database,
    room_id: Annotated[int, Path(..., description="Room ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
):
    db_room = (await db.exec(select(Room).where(Room.id == room_id))).first()
    if db_room is None:
        raise HTTPException(404, "Room not found")
    aggs = await db.exec(
        select(ItemAttemptsCount)
        .where(ItemAttemptsCount.room_id == room_id)
        .order_by(col(ItemAttemptsCount.total_score).desc())
    )
    aggs_resp = []
    user_agg = None
    for i, agg in enumerate(aggs):
        includes = ["user.country"]
        if agg.user_id == current_user.id:
            includes.append("position")
        resp = await ItemAttemptsCountModel.transform(agg, includes=includes)
        aggs_resp.append(resp)
        if agg.user_id == current_user.id:
            user_agg = resp

    return {
        "leaderboard": aggs_resp,
        "user_score": user_agg,
    }


@router.get(
    "/rooms/{room_id}/events",
    tags=["Rooms"],
    name="Get Room Events",
    description="Get list of room events (descending, filterable by after / before range).",
    responses={
        200: api_doc(
            "Room events",
            {
                "beatmaps": list[BeatmapModel],
                "beatmapsets": list[BeatmapsetModel],
                "current_playlist_item_id": int,
                "events": list[MultiplayerEventResp],
                "first_event_id": int,
                "last_event_id": int,
                "playlist_items": list[PlaylistModel],
                "room": RoomModel,
                "user": list[UserModel],
            },
            ["country", "details", "scores"],
            name="RoomEventsResponse",
        )
    },
)
async def get_room_events(
    db: Database,
    room_id: Annotated[int, Path(..., description="Room ID")],
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    limit: Annotated[int, Query(ge=1, le=1000, description="Return count (1-1000)")] = 100,
    after: Annotated[int | None, Query(ge=0, description="Only include events with ID greater than this value")] = None,
    before: Annotated[int | None, Query(ge=0, description="Only include events with ID less than this value")] = None,
):
    events = (
        await db.exec(
            select(MultiplayerEvent)
            .where(
                MultiplayerEvent.room_id == room_id,
                col(MultiplayerEvent.id) > after if after is not None else True,
                col(MultiplayerEvent.id) < before if before is not None else True,
            )
            .order_by(col(MultiplayerEvent.id).desc())
            .limit(limit)
        )
    ).all()

    user_ids = set()
    playlist_items = {}
    beatmap_ids = set()

    event_resps = []
    first_event_id = 0
    last_event_id = 0

    current_playlist_item_id = 0
    for event in events:
        event_resps.append(MultiplayerEventResp.from_db(event))
        if event.user_id:
            user_ids.add(event.user_id)
        if event.playlist_item_id is not None and (
            playitem := (
                await db.exec(
                    select(Playlist).where(
                        Playlist.id == event.playlist_item_id,
                        Playlist.room_id == room_id,
                    )
                )
            ).first()
        ):
            current_playlist_item_id = playitem.id
            playlist_items[event.playlist_item_id] = playitem
            beatmap_ids.add(playitem.beatmap_id)
            scores = await db.exec(
                select(Score).where(
                    Score.playlist_item_id == event.playlist_item_id,
                    Score.room_id == room_id,
                )
            )
            for score in scores:
                user_ids.add(score.user_id)
                beatmap_ids.add(score.beatmap_id)
        first_event_id = min(first_event_id, event.id)
        last_event_id = max(last_event_id, event.id)

    room = (await db.exec(select(Room).where(Room.id == room_id))).first()
    if room is None:
        raise HTTPException(404, "Room not found")
    room_resp = await RoomModel.transform(room, includes=["current_playlist_item"])
    if room.category == RoomCategory.REALTIME:
        current_playlist_item_id = (await Room.current_playlist_item(db, room))["id"]

    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    users = await db.exec(select(User).where(col(User.id).in_(user_ids)))
    user_resps = [await UserModel.transform(user, includes=["country"], show_nsfw_media=show_nsfw_media) for user in users]

    beatmaps = await db.exec(select(Beatmap).where(col(Beatmap.id).in_(beatmap_ids)))
    beatmap_resps = [
        await BeatmapModel.transform(
            beatmap,
        )
        for beatmap in beatmaps
    ]

    beatmapsets = []
    for beatmap in beatmaps:
        if beatmap.beatmapset_id not in beatmapsets:
            beatmapsets.append(beatmap.beatmapset)
    beatmapset_resps = [
        await BeatmapsetModel.transform(
            beatmapset,
        )
        for beatmapset in beatmapsets
    ]

    playlist_items_resps = [
        await PlaylistModel.transform(item, includes=["details", "scores"]) for item in playlist_items.values()
    ]

    return {
        "beatmaps": beatmap_resps,
        "beatmapsets": beatmapset_resps,
        "current_playlist_item_id": current_playlist_item_id,
        "events": event_resps,
        "first_event_id": first_event_id,
        "last_event_id": last_event_id,
        "playlist_items": playlist_items_resps,
        "room": room_resp,
        "user": user_resps,
    }

"""LIO (Legacy IO) router for osu-server-spectator compatibility."""

import base64
import json
from typing import Any

from app.const import BANCHOBOT_ID
from app.database.beatmap import Beatmap
from app.database.chat import ChannelType, ChatChannel, ChatMessage, MessageType
from app.database.playlists import Playlist as DBPlaylist
from app.database.room import Room
from app.database.room_participated_user import RoomParticipatedUser
from app.database.user import User
from app.dependencies.database import Database, Redis
from app.dependencies.fetcher import Fetcher
from app.dependencies.storage import StorageService
from app.log import log
from app.models.notification import ChannelMessage
from app.models.playlist import PlaylistItem
from app.models.room import MatchType, QueueMode, RoomCategory, RoomStatus
from app.models.score import RULESETS_VERSION_HASH, GameMode, VersionEntry
from app.service.redis_message_system import redis_message_system
from app.utils import camel_to_snake, utcnow

from .notification.server import server

from fastapi import APIRouter, HTTPException, Request, status, UploadFile
from pydantic import BaseModel, Field, AliasChoices
from sqlalchemy import update
from sqlmodel import col, select

router = APIRouter(prefix="/_lio", include_in_schema=False)
logger = log("LegacyIO")


async def _ensure_room_chat_channel(
    db: Database,
    room: Room,
    host_user_id: int,
) -> ChatChannel:
    """
    Create/ensure a chat channel exists for room, channel_id matches room.channel_id,
    named mp_{room.id} (customizable as needed).
    """
    # 1) Check if channel already exists by channel_id
    try:
        # Use db.execute instead of db.exec for better async compatibility
        result = await db.exec(select(ChatChannel).where(ChatChannel.channel_id == room.channel_id))
        ch = result.first()
    except Exception as e:
        logger.debug(f"Error querying ChatChannel: {e}")
        ch = None

    if ch is None:
        ch = ChatChannel(
            # IMPORTANT: the ChatChannel model maps property `channel_name`
            # to the DB column `name` (see app/database/chat.py:291). Using
            # the keyword `name=` here silently discards the value at the
            # Pydantic / SQLModel layer because there is no `name` field
            # on ChatChannel — the row gets inserted with `name = NULL`,
            # which then makes the channel anonymous in the lobby and
            # also breaks lookups like `mp_{room_id}` everywhere else
            # that resolves channels by name.
            channel_name=f"mp_{room.id}",
            description=f"Multiplayer room {room.id} chat",
            type=ChannelType.MULTIPLAYER,
        )
        db.add(ch)
        # Commit immediately to ensure the channel exists
        await db.commit()
        await db.refresh(ch)
        await db.refresh(room)
        # CRITICAL: the Room model defaults `channel_id` to 0 (int, not None).
        # The previous `if room.channel_id is None` check therefore never
        # fired on freshly-created rooms, leaving `room.channel_id = 0` —
        # an invalid channel reference that caused multiplayer chat
        # messages to route to whatever channel the client's fallback
        # picked (observed: #osu, the user's most-recent PM, etc.).
        # Use a falsy check so both `None` and `0` trigger the assignment.
        if not room.channel_id:
            room.channel_id = ch.channel_id
    else:
        room.channel_id = ch.channel_id

    return ch


class RoomCreateRequest(BaseModel):
    """Request model for creating a multiplayer room."""

    name: str
    user_id: int
    password: str | None = None
    match_type: str = "HeadToHead"
    queue_mode: str = "HostOnly"
    initial_playlist: list[dict[str, Any]] = []
    playlist: list[dict[str, Any]] = []


async def _validate_user_exists(db: Database, user_id: int) -> User:
    """Validate that a user exists in the database."""
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User with ID {user_id} not found")

    return user


async def _ensure_user_not_restricted(db: Database, user: User) -> None:
    if await user.is_restricted(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Restricted users cannot use Torii spectator or multiplayer services.",
        )


class InviteRequest(BaseModel):
    room_id: int
    room_name: str
    inviter_id: int


@router.post("/rooms/{room_id}/invite/{user_id}")
async def invite_user(
    room_id: int,
    user_id: int,
    req: InviteRequest,
    db: Database,
):
    inviter = await db.get(User, req.inviter_id)
    invited = await db.get(User, user_id)
    if not inviter or not invited:
        raise HTTPException(404, "User not found")
    await _ensure_user_not_restricted(db, inviter)
    await _ensure_user_not_restricted(db, invited)

    channel = await ChatChannel.get_pm_channel(req.inviter_id, user_id, db)
    if not channel:
        u1, u2 = sorted([req.inviter_id, user_id])
        channel = ChatChannel(
# channel_name y NO name: el modelo mapea la propiedad channel_name a la
# columna `name`, asi que un `name=` se descarta en silencio en la capa
# de SQLModel y la fila entra con name = NULL. Ahi el canal deja de
# encontrarse por nombre y cada intento crea uno nuevo.
            channel_name=f"pm_{u1}_{u2}",
            description="Private Message",
            type=ChannelType.PM,
        )
        db.add(channel)
        await db.commit()
        await db.refresh(channel)

    await server.batch_join_channel([invited, inviter], channel)

    actual_room_id = req.room_id or room_id
    content = f'Come join my multiplayer game "{req.room_name}": osu://room/{actual_room_id}'

    resp = await redis_message_system.send_message(
        channel_id=channel.channel_id,
        user=inviter,
        content=content,
        is_action=False,
    )

    await server.send_message_to_channel(resp, False)

    temp_msg = ChatMessage(
        message_id=resp["message_id"],
        channel_id=channel.channel_id,
        content=content,
        sender_id=req.inviter_id,
        type=MessageType.PLAIN,
    )

    user_ids = [req.inviter_id, user_id]
    await server.new_private_notification(
        ChannelMessage.init(temp_msg, inviter, user_ids, ChannelType.PM)
    )

    return {"status": "ok"}


def _parse_room_enums(match_type: str, queue_mode: str) -> tuple[MatchType, QueueMode]:
    """Parse and validate room type enums."""
    try:
        match_type_enum = MatchType(camel_to_snake(match_type))
    except ValueError:
        match_type_enum = MatchType.HEAD_TO_HEAD

    try:
        queue_mode_enum = QueueMode(camel_to_snake(queue_mode))
    except ValueError:
        queue_mode_enum = QueueMode.HOST_ONLY

    return match_type_enum, queue_mode_enum


def _coerce_playlist_item(item_data: dict[str, Any], default_order: int, host_user_id: int) -> dict[str, Any]:
    """
    Normalize playlist item data with default values.

    Args:
        item_data: Raw playlist item data
        default_order: Default playlist order
        host_user_id: Host user ID for default owner

    Returns:
        Dict with normalized playlist item data
    """
    # Use host_user_id if owner_id is 0 or not provided
    owner_id = item_data.get("owner_id", host_user_id)
    if owner_id == 0:
        owner_id = host_user_id

    return {
        "owner_id": owner_id,
        "ruleset_id": item_data.get("ruleset_id", 0),
        "beatmap_id": item_data.get("beatmap_id"),
        "required_mods": item_data.get("required_mods", []),
        "allowed_mods": item_data.get("allowed_mods", []),
        "expired": bool(item_data.get("expired", False)),
        "playlist_order": item_data.get("playlist_order", default_order),
        "played_at": item_data.get("played_at"),
        "freestyle": bool(item_data.get("freestyle", True)),
        "beatmap_checksum": item_data.get("beatmap_checksum", ""),
        "star_rating": item_data.get("star_rating", 0.0),
    }


def _validate_playlist_items(items: list[dict[str, Any]]) -> None:
    """Validate playlist items data."""
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="At least one playlist item is required to create a room"
        )

    for idx, item in enumerate(items):
        if item["beatmap_id"] is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Playlist item at index {idx} missing beatmap_id"
            )

        ruleset_id = item["ruleset_id"]
        if not isinstance(ruleset_id, int):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Playlist item at index {idx} has invalid ruleset_id {ruleset_id}",
            )


async def _create_room(db: Database, room_data: dict[str, Any]) -> tuple[Room, int]:
    host_user_id = room_data.get("user_id", BANCHOBOT_ID)
    # el spectator manda el tipo real en la key "type" (snake_case: "ranked_play",
    # "matchmaking", etc). "match_type" queda como fallback legacy. sin esto las salas
    # de ranked play se tipaban MATCHMAKING y el cliente crasheaba casteando el room state.
    match_type = room_data.get("type") or room_data.get("match_type") or ("HeadToHead" if host_user_id != BANCHOBOT_ID else "Matchmaking")
    room_name = room_data.get("name", f"{match_type} room: {utcnow().isoformat()}")
    password = room_data.get("password")
    queue_mode = room_data.get("queue_mode", "HostOnly")

    if not host_user_id or not isinstance(host_user_id, int):
        raise HTTPException(status_code=400, detail="Missing or invalid user_id")

    host_user = await _validate_user_exists(db, host_user_id)
    await _ensure_user_not_restricted(db, host_user)

    match_type_enum, queue_mode_enum = _parse_room_enums(match_type, queue_mode)

    # Create room
    room = Room(
        name=room_name,
        category=RoomCategory.REALTIME,
        host_id=host_user_id,
        password=password if password else None,
        type=match_type_enum,
        queue_mode=queue_mode_enum,
        status=RoomStatus.IDLE,
        participant_count=1,
        auto_skip=False,
        auto_start_duration=0,
    )

    db.add(room)
    await db.commit()
    await db.refresh(room)

    return room, host_user_id


async def _add_playlist_items(
    db: Database,
    fetcher: Fetcher,
    room_id: int,
    room_data: dict[str, Any],
    host_user_id: int,
) -> None:
    """Add playlist items to the room."""
    initial_playlist = room_data.get("initial_playlist", [])
    legacy_playlist = room_data.get("playlist", [])

    items_raw: list[dict[str, Any]] = []

    # Process initial playlist
    for i, item in enumerate(initial_playlist):
        if hasattr(item, "dict"):
            item = item.dict()
        items_raw.append(_coerce_playlist_item(item, i, host_user_id))

    # Process legacy playlist
    start_index = len(items_raw)
    for j, item in enumerate(legacy_playlist, start=start_index):
        items_raw.append(_coerce_playlist_item(item, j, host_user_id))

    # Validate playlist items
    _validate_playlist_items(items_raw)

    # Torii: spectator may pass a beatmap_id that hasn't been fetched into the
    # local `beatmaps` table yet (the user picked a map nobody on this server
    # has played before). Without this pre-fetch the INSERT below trips the
    # `room_playlists.beatmap_id -> beatmaps.id` FK and we get a 500 +
    # half-created zombie room. Resolve each beatmap (which auto-bootstraps the
    # beatmapset row too) before touching room_playlists.
    for item_data in items_raw:
        beatmap_id = item_data["beatmap_id"]
        # `not beatmap_id` no alcanza: -1 es truthy en python, y el spectator
        # manda -1 para mapas sin id online. Ver _ensure_beatmap_exists.
        if not beatmap_id or int(beatmap_id) <= 0:
            continue
        resolved = await Beatmap.get_or_fetch(db, fetcher, bid=beatmap_id)
        if resolved is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Beatmap {beatmap_id} could not be fetched from any mirror",
            )

    # Insert playlist items
    for item_data in items_raw:
        playlist_item = PlaylistItem(
            id=-1,  # Placeholder, will be assigned by add_to_db
            owner_id=item_data["owner_id"],
            ruleset_id=item_data["ruleset_id"],
            expired=item_data["expired"],
            playlist_order=item_data["playlist_order"],
            played_at=item_data["played_at"],
            allowed_mods=item_data["allowed_mods"],
            required_mods=item_data["required_mods"],
            beatmap_id=item_data["beatmap_id"],
            freestyle=item_data["freestyle"],
            beatmap_checksum=item_data["beatmap_checksum"],
            star_rating=item_data["star_rating"],
        )
        await DBPlaylist.add_to_db(playlist_item, room_id=room_id, session=db)


async def _add_host_as_participant(db: Database, room_id: int, host_user_id: int) -> None:
    """Add the host as a room participant and update participant count."""
    participant = RoomParticipatedUser(room_id=room_id, user_id=host_user_id)
    db.add(participant)

    await _update_room_participant_count(db, room_id)


async def _verify_room_password(db: Database, room_id: int, provided_password: str | None) -> None:
    """Verify room password if required."""
    room_result = await db.execute(select(Room).where(col(Room.id) == room_id))
    room = room_result.scalar_one_or_none()

    if room is None:
        logger.debug(f"Room {room_id} not found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

    logger.debug(f"Room {room_id} has password: {bool(room.password)}, provided: {bool(provided_password)}")

    # If room has password but none provided
    if room.password and not provided_password:
        logger.debug(f"Room {room_id} requires password but none provided")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password required")

    # If room has password and provided password doesn't match
    if room.password and provided_password and provided_password != room.password:
        logger.debug(f"Room {room_id} password mismatch")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid password")

    logger.debug(f"Room {room_id} password verification passed")


async def _add_or_update_participant(db: Database, room_id: int, user_id: int) -> None:
    """Add user as participant or update existing participation record."""
    # Check if user already has active participation record
    existing_result = await db.execute(
        select(RoomParticipatedUser.id).where(
            RoomParticipatedUser.room_id == room_id,
            RoomParticipatedUser.user_id == user_id,
            col(RoomParticipatedUser.left_at).is_(None),
        )
    )
    existing_ids = existing_result.scalars().all()  # Get all matching IDs

    if existing_ids:
        # If multiple records exist, clean up duplicates and keep only the latest
        if len(existing_ids) > 1:
            logger.debug(f"Warning: user {user_id} has {len(existing_ids)} active participation records in room {room_id}")

            # Mark all records except the first as left (clean up duplicate records)
            for extra_id in existing_ids[1:]:
                await db.execute(
                    update(RoomParticipatedUser)
                    .where(col(RoomParticipatedUser.id) == extra_id)
                    .values(left_at=utcnow())
                )

        # Update remaining active participation record (refresh joined time)
        await db.execute(
            update(RoomParticipatedUser)
            .where(col(RoomParticipatedUser.id) == existing_ids[0])
            .values(joined_at=utcnow())
        )
    else:
        # Create new participation record
        participant = RoomParticipatedUser(room_id=room_id, user_id=user_id)
        db.add(participant)


class BeatmapEnsureRequest(BaseModel):
    """Request model for ensuring beatmap exists."""

    beatmap_id: int


async def _ensure_beatmap_exists(db: Database, fetcher, redis, beatmap_id: int) -> dict[str, Any]:
    """
    Ensure beatmap exists (including metadata and raw file cache).

    Args:
        db: Database session
        fetcher: API fetcher
        redis: Redis connection
        beatmap_id: Beatmap ID

    Returns:
        Dict: Response containing status information
    """
    # Torii: el spectator manda beatmap_id=-1 cuando el cliente esta jugando un
    # mapa sin id online (una dificultad local, una copia hecha en el editor, o
    # metadata online sin resolver). Esos scores no submitean nunca, asi que no
    # tiene sentido salir a buscarlos: ppy devuelve 404 y nos llenaba el log de
    # warnings (16 en un par de horas). Cortamos temprano y sin ruido.
    if beatmap_id <= 0:
        return {"success": False, "error": "beatmap has no online id", "beatmap_id": beatmap_id}

    try:
        # 1. Ensure beatmap metadata exists in database
        from app.database.beatmap import Beatmap

        beatmap = await Beatmap.get_or_fetch(db, fetcher, bid=beatmap_id)

        if not beatmap:
            return {"success": False, "error": f"Beatmap {beatmap_id} not found", "beatmap_id": beatmap_id}

        # 2. Pre-cache raw beatmap file
        cache_key = f"beatmap:{beatmap_id}:raw"
        cached = await redis.exists(cache_key)

        if not cached:
            # Asynchronously preload raw file into cache
            try:
                await fetcher.get_or_fetch_beatmap_raw(redis, beatmap_id)
                logger.debug(f"Successfully cached raw beatmap file for {beatmap_id}")
            except Exception as e:
                logger.debug(f"Warning: Failed to cache raw beatmap {beatmap_id}: {e}")
                # Even if raw file cache fails, consider ensure operation successful (since metadata exists)

        return {
            "success": True,
            "beatmap_id": beatmap_id,
            "metadata_cached": True,
            "raw_file_cached": await redis.exists(cache_key),
            "beatmap_title": f"{beatmap.beatmapset.artist} - {beatmap.beatmapset.title} [{beatmap.version}]",
        }

    except Exception as e:
        logger.debug(f"Error ensuring beatmap {beatmap_id}: {e}")
        return {"success": False, "error": str(e), "beatmap_id": beatmap_id}


async def _update_room_participant_count(db: Database, room_id: int) -> int:
    """Update room participant count and return current count."""
    # Count active participants
    active_participants_result = await db.execute(
        select(RoomParticipatedUser.user_id).where(
            RoomParticipatedUser.room_id == room_id, col(RoomParticipatedUser.left_at).is_(None)
        )
    )
    active_participants = active_participants_result.all()
    count = len(active_participants)

    # Update room participant count
    await db.execute(update(Room).where(col(Room.id) == room_id).values(participant_count=count))

    return count


async def _end_room_if_empty(db: Database, room_id: int) -> bool:
    """If room is empty, mark room as ended. Returns whether room was ended."""
    # Check if room still has active participants
    participant_count = await _update_room_participant_count(db, room_id)

    if participant_count == 0:
        # Room is empty, mark as ended
        now = utcnow()
        await db.execute(
            update(Room)
            .where(col(Room.id) == room_id)
            .values(
                ends_at=now,
                status=RoomStatus.IDLE,  # Or use RoomStatus.ENDED if available
                participant_count=0,
            )
        )
        logger.debug(f"Room {room_id} ended automatically (no participants remaining)")
        return True

    return False


async def _transfer_ownership_or_end_room(db: Database, room_id: int, leaving_user_id: int) -> bool:
    """Handle host leaving logic: transfer host permissions or end room. Returns whether room was ended."""
    # Find other active participants to transfer host permissions
    remaining_result = await db.execute(
        select(RoomParticipatedUser.user_id)
        .where(
            col(RoomParticipatedUser.room_id) == room_id,
            col(RoomParticipatedUser.user_id) != leaving_user_id,
            col(RoomParticipatedUser.left_at).is_(None),
        )
        .order_by(col(RoomParticipatedUser.joined_at))  # Sort by join time
    )
    remaining_participants = remaining_result.all()

    if remaining_participants:
        # Transfer host permissions to earliest joined user
        new_owner_id = remaining_participants[0][0]  # Get user_id
        await db.execute(update(Room).where(col(Room.id) == room_id).values(host_id=new_owner_id))
        logger.debug(f"Room {room_id} ownership transferred from {leaving_user_id} to {new_owner_id}")
        return False  # Room continues to exist
    else:
        # No other participants, end room
        return await _end_room_if_empty(db, room_id)


# ===== API ENDPOINTS =====


@router.post("/multiplayer/rooms")
async def create_multiplayer_room(
    room_data: dict[str, Any],
    db: Database,
    fetcher: Fetcher,
) -> int:
    """Create a new multiplayer room with initial playlist."""
    try:
        # Parse room data if string
        if isinstance(room_data, str):
            room_data = json.loads(room_data)

        logger.debug(f"Creating room with data: {room_data}")

        # Create room
        room, host_user_id = await _create_room(db, room_data)
        room_id = room.id

        try:
            channel = await _ensure_room_chat_channel(db, room, host_user_id)

            # Add host to channel
            host_user = await db.get(User, host_user_id)
            if host_user:
                await server.batch_join_channel([host_user], channel)
            # las salas de matchmaking/ranked-play se crean SIN playlist: el spectator
            # escribe los items directo a la DB (AddPlaylistItemAsync) a medida que avanza
            # el match. si exigimos playlist aca la sala se rechaza y el cliente queda
            # colgado en "waiting for opponents". el spectator manda el tipo en la key
            # "type" (no "match_type"), y estas salas las crea BanchoBot, asi que
            # _create_room las tipa MATCHMAKING: gateamos sobre el room ya creado.
            is_matchmaking = room.type in (MatchType.MATCHMAKING, MatchType.RANKED_PLAY)
            has_playlist = bool(room_data.get("initial_playlist") or room_data.get("playlist"))
            if has_playlist or not is_matchmaking:
                await _add_playlist_items(db, fetcher, room_id, room_data, host_user_id)

            # Add host as participant
            # await _add_host_as_participant(db, room_id, host_user_id)

            await db.commit()
            return room_id

        # Torii: catch ANY failure during post-room-row setup (playlist FK
        # violations from unknown beatmap_ids, chat-channel hiccups, etc.) so
        # the partially-created room row gets rolled back. The previous
        # narrower `except HTTPException` let SQLAlchemy IntegrityError leak
        # through, leaving zombie rooms in the lounge that nobody could join.
        except Exception as exc:
            logger.debug(f"Room creation post-setup failed for room {room_id}: {exc!r}")
            try:
                await db.rollback()
                # Re-fetch in the fresh transaction so we can delete the row.
                stale_room = await db.get(Room, room_id)
                if stale_room is not None:
                    await db.delete(stale_room)
                    await db.commit()
            except Exception as cleanup_exc:
                logger.debug(f"Zombie-room cleanup for {room_id} failed: {cleanup_exc!r}")
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to finish creating room: {exc}",
            )

    except HTTPException:
        raise


@router.delete("/multiplayer/rooms/{room_id}/users/{user_id}")
async def remove_user_from_room(
    room_id: int,
    user_id: int,
    db: Database,
) -> dict[str, Any]:
    """Remove a user from a multiplayer room."""
    try:
        now = utcnow()

        # Check if room exists
        room_result = await db.execute(select(Room).where(col(Room.id) == room_id))
        room = room_result.scalar_one_or_none()

        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

        room_owner_id = room.host_id
        ends_at = room.ends_at
        channel_id = room.channel_id

        # If room has already ended, return directly
        if ends_at is not None:
            logger.debug(f"Room {room_id} is already ended")
            return {"success": True, "room_ended": True}

        # Check if user is in room
        participant_result = await db.execute(
            select(RoomParticipatedUser.id).where(
                col(RoomParticipatedUser.room_id) == room_id,
                col(RoomParticipatedUser.user_id) == user_id,
                col(RoomParticipatedUser.left_at).is_(None),
            )
        )
        participant_query = participant_result.first()

        if not participant_query:
            # User not in room, check if room needs to end (idempotent operation)
            room_ended = await _end_room_if_empty(db, room_id)
            await db.commit()

            try:
                if channel_id:
                    await server.leave_room_channel(int(channel_id), int(user_id))
                    if room_ended:
                        server.channels.pop(int(channel_id), None)
            except Exception as e:
                logger.debug(f"[warn] failed to leave user {user_id} from channel {channel_id}: {e}")

            return {"success": True, "room_ended": room_ended}

        # Mark user as left room
        await db.execute(
            update(RoomParticipatedUser)
            .where(
                col(RoomParticipatedUser.room_id) == room_id,
                col(RoomParticipatedUser.user_id) == user_id,
                col(RoomParticipatedUser.left_at).is_(None),
            )
            .values(left_at=now)
        )

        room_ended = False

        # Check if host left
        if user_id == room_owner_id:
            logger.debug(f"Host {user_id} is leaving room {room_id}")
            room_ended = await _transfer_ownership_or_end_room(db, room_id, user_id)
        else:
            # Not host leaving, only check if room is empty
            room_ended = await _end_room_if_empty(db, room_id)

        await db.commit()
        logger.debug(f"Successfully removed user {user_id} from room {room_id}, room_ended: {room_ended}")

        # After commit: remove user from chat channel; clean up in-memory channel if ended
        try:
            if channel_id:
                await server.leave_room_channel(int(channel_id), int(user_id))
                if room_ended:
                    server.channels.pop(int(channel_id), None)
        except Exception as e:
            logger.debug(f"[warn] failed to leave user {user_id} from channel {channel_id}: {e}")

        return {"success": True, "room_ended": room_ended}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.debug(f"Error removing user from room: {e!s}")
        raise


@router.put("/multiplayer/rooms/{room_id}/users/{user_id}")
async def add_user_to_room(
    request: Request,
    room_id: int,
    user_id: int,
    db: Database,
) -> dict[str, Any]:
    """Add a user to a multiplayer room."""
    logger.debug(f"Adding user {user_id} to room {room_id}")
    joining_user = await _validate_user_exists(db, user_id)
    await _ensure_user_not_restricted(db, joining_user)

    # Get request body and parse user_data
    body = await request.body()
    user_data = None
    if body:
        try:
            user_data = json.loads(body.decode("utf-8"))
            logger.debug(f"Parsed user_data: {user_data}")
        except json.JSONDecodeError:
            logger.debug("Failed to parse user_data from request body")
            user_data = None

    # Check if room has ended
    room_result = await db.exec(select(Room.ends_at, Room.channel_id, Room.host_id).where(col(Room.id) == room_id))
    room_row = room_result.first()
    if not room_row:
        raise HTTPException(status_code=404, detail="Room not found")

    ends_at, channel_id, host_user_id = room_row
    if ends_at is not None:
        logger.debug(f"User {user_id} attempted to join ended room {room_id}")
        raise HTTPException(status_code=410, detail="Room has ended and cannot accept new participants")

    # Verify room password
    provided_password = user_data.get("password") if user_data else None
    logger.debug(f"Verifying room {room_id} with password: {provided_password}")
    await _verify_room_password(db, room_id, provided_password)

    # Add or update participant
    await _add_or_update_participant(db, room_id, user_id)
    # Update participant count
    await _update_room_participant_count(db, room_id)

    # First commit DB state to ensure participation relationship is persisted
    await db.commit()
    logger.debug(f"Successfully added user {user_id} to room {room_id}")

    # Ensure chat channel exists and add user to it
    try:
        # If room does not have channel allocated/created, create and synchronize
        if not channel_id:
            room = await db.get(Room, room_id)
            if room is None:
                raise HTTPException(status_code=404, detail="Room not found")
            await _ensure_room_chat_channel(db, room, host_user_id)
            await db.refresh(room)
            channel_id = room.channel_id

        if channel_id:
            # Join chat channel -> in-memory registration + send chat.channel.join to online clients
            await server.join_room_channel(int(channel_id), int(user_id))
        else:
            # In theory this should not happen; log for diagnosis
            logger.debug(f"[warn] Room {room_id} has no channel_id after ensure.")
    except Exception as e:
        # Does not affect main room join flow, log only
        logger.debug(f"[warn] failed to join user {user_id} to channel of room {room_id}: {e}")

    return {"success": True}


@router.post("/beatmaps/ensure")
async def ensure_beatmap_present(
    beatmap_data: BeatmapEnsureRequest,
    db: Database,
    redis: Redis,
    fetcher: Fetcher,
) -> dict[str, Any]:
    """
    Ensure beatmap exists on server (including metadata and raw file cache).

    Used by osu-server-spectator to ensure beatmap files are available on server side,
    avoiding latency from on-demand fetching.
    """
    try:
        beatmap_id = beatmap_data.beatmap_id
        logger.debug(f"Ensuring beatmap {beatmap_id} is present")

        # Ensure beatmap exists
        result = await _ensure_beatmap_exists(db, fetcher, redis, beatmap_id)

        # Commit database changes
        await db.commit()

        logger.debug(f"Ensure beatmap {beatmap_id} result: {result}")
        return result

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.debug(f"Error ensuring beatmap: {e!s}")
        raise


class ReplayDataRequest(BaseModel):
    score_id: int = Field(validation_alias=AliasChoices("score_id", "scoreId", "s"))
    user_id: int = Field(validation_alias=AliasChoices("user_id", "userId", "u"))
    mreplay: str = Field(validation_alias=AliasChoices("mreplay", "replay", "data"))
    beatmap_id: int = Field(validation_alias=AliasChoices("beatmap_id", "beatmapId", "b"))


def _score_is_td_osu(score) -> bool:
    """True when this score is an osu! ruleset play carrying the TD mod —
    the only combination where the touchscreen classifier has anything
    meaningful to say."""
    if score.gamemode != GameMode.OSU:
        return False
    mods = score.mods or []
    return any(
        isinstance(m, dict) and str(m.get("acronym", "")).upper() == "TD"
        for m in mods
    )


async def _classify_and_store_td_play_style(db, score, replay_bytes: bytes) -> None:
    """Call the perf server's touchscreen classifier, persist the verdict,
    and (if it changes the pp picture) trigger a pp recalc for this score.

    Errors propagate to the caller, which is expected to swallow them —
    classification is best-effort, never a blocker on the replay upload
    path. The score keeps td_play_style = 0 (Unknown) on failure, which
    means the conservative TD penalty stays applied.
    """
    # Local imports to keep the cold-import surface of this router lean
    # — these are only paid when a TD osu! score actually uploads a replay.
    from app.calculators.performance.performance_server import (
        PerformanceServerPerformanceCalculator,
        td_play_style_from_wire,
        TD_PLAY_STYLE_DRAG,
    )
    from app.calculator import calculate_pp, get_calculator
    from app.dependencies.database import get_redis
    from app.dependencies.fetcher import get_fetcher

    calculator = get_calculator()
    if not isinstance(calculator, PerformanceServerPerformanceCalculator):
        # Other calculator backends (rosu-pp, no-calculator) don't know
        # how to ask a perf server for replay classification. Skip
        # silently — same conservative state as "perf server unreachable".
        return

    # Pull the .osu raw text. The classifier needs the hit-object timings
    # to know which intervals to look at (everything between consecutive
    # hits, excluding sliders and spinners). Use the shared fetcher
    # singleton — it already has a warm OAuth token, redis-cache wiring,
    # and the right shape for get_or_fetch_beatmap_raw.
    redis = get_redis()
    fetcher = await get_fetcher()
    beatmap_raw = await fetcher.get_or_fetch_beatmap_raw(redis, score.beatmap_id)
    if not beatmap_raw:
        logger.warning(
            "Touchscreen classifier: no beatmap_raw for beatmap {bm_id}, skipping",
            bm_id=score.beatmap_id,
        )
        return

    result = await calculator.classify_touchscreen(
        replay_bytes=replay_bytes,
        beatmap_raw=beatmap_raw,
        beatmap_id=score.beatmap_id,
        score_id=score.id,
    )

    new_style = td_play_style_from_wire(result["style"])
    prev_style = score.td_play_style or 0

    score.td_play_style = new_style
    score.td_classification_confidence = result["confidence"]
    await db.commit()
    await db.refresh(score)

    logger.info(
        "Touchscreen classifier: score={score_id} bm={bm_id} style={style} confidence={conf:.2f} prev={prev}",
        score_id=score.id,
        bm_id=score.beatmap_id,
        style=result["style"],
        conf=result["confidence"],
        prev=prev_style,
    )

    # Re-run pp for this score iff the verdict changed AND the change
    # actually affects the pp penalty (Unknown→Drag or Tap→Drag means
    # FairTouchScreen kicks in; Drag→anything-else means it should come
    # back off). No-op in the common case where the verdict was already
    # the same value (replay re-upload, idempotent re-classification).
    if new_style != prev_style and (new_style == TD_PLAY_STYLE_DRAG or prev_style == TD_PLAY_STYLE_DRAG):
        try:
            new_pp = await calculate_pp(score, beatmap_raw, db)
            score.pp = new_pp
            await db.commit()
            logger.info(
                "Touchscreen classifier: recomputed pp for score {score_id}: {pp:.2f} (style {prev} → {new})",
                score_id=score.id, pp=new_pp, prev=prev_style, new=new_style,
            )
        except Exception:
            logger.exception(
                "Touchscreen classifier: pp recalc failed for score {score_id}",
                score_id=score.id,
            )


@router.post("/scores/replay")
async def save_replay(
    req: ReplayDataRequest | None,
    storage_service: StorageService,
    db: Database,
    request: Request,
):
    try:
        content_type = request.headers.get("content-type", "")
        body_bytes = await request.body()

        score_id: int | None = None
        user_id: int | None = None
        beatmap_id: int | None = None
        data_bytes: bytes | None = None

        if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            def get_first(*names: str) -> str | UploadFile | None:
                for n in names:
                    v = form.get(n)
                    if v is not None:
                        return v
                return None
            def to_int(v: str | UploadFile | None) -> int | None:
                if isinstance(v, UploadFile) or v is None:
                    return None
                try:
                    return int(v)
                except Exception:
                    return None
            score_id = to_int(get_first("score_id", "scoreId", "s"))
            user_id = to_int(get_first("user_id", "userId", "u"))
            beatmap_id = to_int(get_first("beatmap_id", "beatmapId", "b"))
            file_field = get_first("file", "replay", "mreplay", "data")
            if isinstance(file_field, UploadFile):
                data_bytes = await file_field.read()
            elif isinstance(file_field, str):
                try:
                    data_bytes = base64.b64decode(file_field)
                except Exception:
                    data_bytes = None
        else:
            data_dict: dict[str, Any] | None = None
            try:
                data_dict = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                data_dict = None
            if data_dict is None and req is None:
                raise HTTPException(status_code=400, detail="Invalid replay payload")
            if req is None and data_dict is not None:
                req = ReplayDataRequest.model_validate(data_dict)
            if req is not None:
                score_id = req.score_id
                user_id = req.user_id
                beatmap_id = req.beatmap_id
                try:
                    data_bytes = base64.b64decode(req.mreplay)
                except Exception:
                    data_bytes = None

        if score_id is None or user_id is None or beatmap_id is None or data_bytes is None:
            raise HTTPException(status_code=400, detail="Missing required replay fields")

        replay_user = await db.get(User, user_id)
        if replay_user is None:
            raise HTTPException(status_code=404, detail="User not found")
        await _ensure_user_not_restricted(db, replay_user)

        from app.database.score import Score
        score = await db.get(Score, score_id)
        replay_path = score.replay_filename if score else f"replays/{score_id}_{beatmap_id}_{user_id}_lazer_replay.osr"
        await storage_service.write_file(replay_path, data_bytes, "application/x-osu-replay")
        if score:
            score.has_replay = True
            await db.commit()
            await db.refresh(score)

            # Now that the replay is on disk and has_replay is set,
            # kick the anti-cheat task. Fire-and-forget.
            try:
                import asyncio as _asyncio
                from app.database.score import _submit_to_anticheat_background
                from app.dependencies.database import engine as _engine
                _asyncio.create_task(_submit_to_anticheat_background(_engine, score_id))
            except Exception as ac_err:
                logger.warning(f"anticheat dispatch failed for score {score_id}: {ac_err}")
        logger.debug(f"Saved replay for score {score_id} to {replay_path}")

        # FairTouchScreen classifier hook. Runs for osu! ruleset scores
        # carrying the TD mod — the only combination where the verdict
        # affects pp. Policy:
        #   * Tap   (cursor teleports — could be multi-finger aim) → keep penalty
        #   * Drag  (cursor continuous — single-finger aim proven)  → FairTouchScreen
        #   * Mixed (gates failed or signals ambiguous)             → keep penalty
        # Failures are swallowed — the classifier is best-effort, never a
        # blocker on the upload path. Worst case the column stays 0
        # (Unknown), which means the score keeps the current TD penalty.
        if score is not None and _score_is_td_osu(score):
            try:
                await _classify_and_store_td_play_style(db, score, data_bytes)
            except Exception as exc:
                logger.warning(
                    "Touchscreen classification failed for score {score_id}: {exc}",
                    score_id=score_id,
                    exc=exc,
                )

        return {"success": True, "path": replay_path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False)
async def lio_fallback(path: str, request: Request):
    try:
        logger.debug(f"LIO fallback hit: {request.method} /_lio/{path} content-type={request.headers.get('content-type')}")
        return {"error": "not_found", "path": f"/_lio/{path}"}
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/ruleset-hashes", response_model=dict[GameMode, VersionEntry])
async def get_ruleset_version():
    return RULESETS_VERSION_HASH

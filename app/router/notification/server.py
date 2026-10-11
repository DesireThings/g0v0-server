import asyncio
from typing import Annotated, overload

from app.const import BANCHOBOT_ID
from app.database import ChatMessageDict
from app.database.chat import ChannelType, ChatChannel, ChatChannelDict, ChatChannelModel
from app.database.notification import Notification, UserNotification, insert_notification
from app.database.user import User, UserModel
from app.dependencies.database import (
    Redis,
    engine,
    redis_message_client,
    with_db,
)
from app.dependencies.user import get_current_user_and_token
from app.log import log
from app.models.chat import ChatEvent
from app.models.notification import NotificationDetail
from app.service.subscribers.chat import ChatSubscriber
from app.utils import bg_tasks, safe_json_dumps

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.security import SecurityScopes
from fastapi.websockets import WebSocketState
from sqlmodel import select, col, update
from sqlmodel.ext.asyncio.session import AsyncSession

logger = log("NotificationServer")


class ChatServer:
    def __init__(self):
        self.connect_client: dict[int, set[WebSocket]] = {} #to allow both browser and ingame messaging
        self.channels: dict[int, list[int]] = {}
        self.redis: Redis = redis_message_client

        self.tasks: set[asyncio.Task] = set()
        self.ChatSubscriber = ChatSubscriber()
        self.ChatSubscriber.chat_server = self
        self._subscribed = False

    def connect(self, user_id: int, client: WebSocket):
        if user_id not in self.connect_client:
            self.connect_client[user_id] = set()
        self.connect_client[user_id].add(client)


    def get_user_joined_channel(self, user_id: int) -> list[int]:
        return [channel_id for channel_id, users in self.channels.items() if user_id in users]

    async def disconnect(self, user: User, session: AsyncSession, client: WebSocket | None = None):
        user_id = user.id

        # remove only the disconnected websocket (if provided)
        if client is not None:
            ws_set = self.connect_client.get(user_id)
            if ws_set:
                ws_set.discard(client)
                if not ws_set:
                    self.connect_client.pop(user_id, None)
        else:
            # fallback: old behavior (only if you truly don't know which socket)
            self.connect_client.pop(user_id, None)

        # If user still has an active socket, DO NOT remove them from channels.
        if user_id in self.connect_client:
            return

        # --- original channel cleanup logic (only when last socket is gone) ---
        channel_ids_to_process = []
        for channel_id, channel in self.channels.items():
            if user_id in channel:
                channel_ids_to_process.append(channel_id)

        for channel_id in channel_ids_to_process:
            if channel_id in self.channels and user_id in self.channels[channel_id]:
                self.channels[channel_id].remove(user_id)
                db_channel = (
                    await session.exec(select(ChatChannel).where(ChatChannel.channel_id == channel_id))
                ).first()
                if db_channel:
                    await self.leave_channel(user, db_channel)


    @overload
    async def send_event(self, client: int, event: ChatEvent): ...

    @overload
    async def send_event(self, client: WebSocket, event: ChatEvent): ...

    async def send_event(self, client: WebSocket | int, event: ChatEvent):
        # resolve sockets
        if isinstance(client, int):
            user_id = client
            sockets = set(self.connect_client.get(user_id, set()))
            if not sockets:
                return
        else:
            # send to a single socket (rare path)
            user_id = None
            sockets = {client}

        dead: set[WebSocket] = set()

        payload = safe_json_dumps(event)

        for ws in sockets:
            if ws.client_state != WebSocketState.CONNECTED:
                dead.add(ws)
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)

        # cleanup dead sockets
        if isinstance(client, int) and dead:
            ws_set = self.connect_client.get(user_id)
            if ws_set:
                for ws in dead:
                    ws_set.discard(ws)
                if not ws_set:
                    self.connect_client.pop(user_id, None)



    async def broadcast(self, channel_id: int, event: ChatEvent):
        users_in_channel = list(self.channels.get(channel_id, []))
        logger.info(f"Broadcasting to channel {channel_id}, users: {users_in_channel}")

        # Tu debug de multiplayer, igual que antes
        if not users_in_channel:
            try:
                async with with_db() as session:
                    channel = await session.get(ChatChannel, channel_id)
                    if channel and channel.type == ChannelType.MULTIPLAYER:
                        logger.warning(
                            f"No users in multiplayer channel {channel_id}, message will not be delivered to anyone"
                        )
            except Exception as e:
                logger.error(f"Failed to check channel type for {channel_id}: {e}")
            return

        # Enviar a todos aislando fallos por usuario
        tasks = [self.send_event(user_id, event) for user_id in users_in_channel]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # send_event ya traga/loggea casi todo, pero por si algo raro burbujea:
        for user_id, res in zip(users_in_channel, results):
            if isinstance(res, Exception):
                logger.debug(f"broadcast exception for user {user_id}: {type(res).__name__}: {res}")

    async def mark_as_read(self, channel_id: int, user_id: int, message_id: int):
        await self.redis.set(f"chat:{channel_id}:last_read:{user_id}", message_id)

        try:
            async with with_db() as session:
                # Get notification IDs first to avoid complex subquery update issues
                notification_ids = (
                    await session.exec(
                        select(Notification.id).where(
                            col(Notification.object_type) == "channel",
                            col(Notification.object_id) == channel_id,
                        )
                    )
                ).all()

                if notification_ids:
                    statement = (
                        update(UserNotification)
                        .where(
                            col(UserNotification.user_id) == user_id,
                            col(UserNotification.is_read) == False,
                            col(UserNotification.notification_id).in_(notification_ids),
                        )
                        .values(is_read=True)
                    )

                    result = await session.exec(statement)
                    await session.commit()
                    logger.debug(
                        f"Marked {result.rowcount} notifications as read for channel {channel_id}, user {user_id}"
                    )
        except Exception as e:
            logger.error(
                f"Failed to mark notifications as read for channel {channel_id}, user {user_id}: {e}"
            )

    async def send_message_to_channel(self, message: ChatMessageDict, is_bot_command: bool = False):
        logger.info(
            f"Sending message to channel {message['channel_id']}, message_id: "
            f"{message['message_id']}, is_bot_command: {is_bot_command}"
        )

        event = ChatEvent(
            event="chat.message.new",
            data={"messages": [message], "users": [message["sender"]]},
        )

        if is_bot_command:
            logger.info(f"Sending bot command to user {message['sender_id']}")
            asyncio.create_task(self.send_event(message["sender_id"], event))
        else:
            logger.info(f"Broadcasting message to all users in channel {message['channel_id']}")
            asyncio.create_task(self.broadcast(message["channel_id"], event))

        if message["message_id"] and message["message_id"] > 0:
            try:
                await self.mark_as_read(message["channel_id"], message["sender_id"], message["message_id"])
                await self.redis.set(f"chat:{message['channel_id']}:last_msg", message["message_id"])
                logger.info(f"Updated last message ID for channel {message['channel_id']} to {message['message_id']}")
            except Exception:
                logger.exception("Failed to mark as read / update last_msg")

    async def batch_join_channel(self, users: list[User], channel: ChatChannel):
        channel_id = channel.channel_id

        not_joined = []

        if channel_id not in self.channels:
            self.channels[channel_id] = []
        for user in users:
            if user.id not in self.channels[channel_id]:
                self.channels[channel_id].append(user.id)
                not_joined.append(user)

        for user in not_joined:
            show_nsfw_media = await UserModel.viewer_allows_nsfw_media(user)
            channel_resp = await ChatChannelModel.transform(
                channel,
                user=user,
                server=server,
                includes=ChatChannel.LISTING_INCLUDES,
                show_nsfw_media=show_nsfw_media,
            )
            await self.send_event(
                user.id,
                ChatEvent(
                    event="chat.channel.join",
                    data=channel_resp,  # pyright: ignore[reportArgumentType]
                ),
            )

    async def join_channel(self, user: User, channel: ChatChannel) -> ChatChannelDict:
        user_id = user.id
        channel_id = channel.channel_id

        if channel_id not in self.channels:
            self.channels[channel_id] = []
        if user_id not in self.channels[channel_id]:
            self.channels[channel_id].append(user_id)

        show_nsfw_media = await UserModel.viewer_allows_nsfw_media(user)
        channel_resp: ChatChannelDict = await ChatChannelModel.transform(
            channel,
            user=user,
            server=server,
            includes=ChatChannel.LISTING_INCLUDES,
            show_nsfw_media=show_nsfw_media,
        )

        await self.send_event(
            user_id,
            ChatEvent(
                event="chat.channel.join",
                data=channel_resp,  # pyright: ignore[reportArgumentType]
            ),
        )

        return channel_resp

    async def leave_channel(self, user: User, channel: ChatChannel) -> None:
        user_id = user.id
        channel_id = channel.channel_id

        if channel_id in self.channels and user_id in self.channels[channel_id]:
            self.channels[channel_id].remove(user_id)

        if (c := self.channels.get(channel_id)) is not None and not c:
            del self.channels[channel_id]

        show_nsfw_media = await UserModel.viewer_allows_nsfw_media(user)
        channel_resp = await ChatChannelModel.transform(
            channel,
            user=user,
            server=server,
            includes=ChatChannel.LISTING_INCLUDES,
            show_nsfw_media=show_nsfw_media,
        )
        await self.send_event(
            user_id,
            ChatEvent(
                event="chat.channel.part",
                data=channel_resp,  # pyright: ignore[reportArgumentType]
            ),
        )

    async def join_room_channel(self, channel_id: int, user_id: int):
        async with with_db() as session:
            # Use explicit query to avoid lazy loading issues
            db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == channel_id))).first()
            if db_channel is None:
                logger.warning(f"Attempted to join non-existent channel {channel_id} by user {user_id}")
                return

            user = await session.get(User, user_id)
            if user is None:
                logger.warning(f"Attempted to join channel {channel_id} by non-existent user {user_id}")
                return

            logger.info(f"User {user_id} joining channel {channel_id} (type: {db_channel.type.value})")
            await self.join_channel(user, db_channel)

    async def leave_room_channel(self, channel_id: int, user_id: int):
        async with with_db() as session:
            # Use explicit query to avoid lazy loading issues
            db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == channel_id))).first()
            if db_channel is None:
                logger.warning(f"Attempted to leave non-existent channel {channel_id} by user {user_id}")
                return

            user = await session.get(User, user_id)
            if user is None:
                logger.warning(f"Attempted to leave channel {channel_id} by non-existent user {user_id}")
                return

            logger.info(f"User {user_id} leaving channel {channel_id} (type: {db_channel.type.value})")
            await self.leave_channel(user, db_channel)

    async def new_private_notification(self, detail: NotificationDetail):
        async with with_db() as session:
            id = await insert_notification(session, detail)
            users = (await session.exec(select(UserNotification).where(UserNotification.notification_id == id))).all()
            for user_notification in users:
                data = user_notification.notification.model_dump()
                if data.get("source_user_id") == BANCHOBOT_ID:
                    data["source_user_id"] = 0
                data["is_read"] = user_notification.is_read
                data["details"] = user_notification.notification.details
                await server.send_event(
                    user_notification.user_id,
                    ChatEvent(
                        event="new",
                        data=data,
                    ),
                )


server = ChatServer()

chat_router = APIRouter(include_in_schema=False)


async def _listen_stop(ws: WebSocket, user_id: int):
    while True:
        try:
            packets = await ws.receive_json()
            if packets.get("event") == "chat.end":
                await ws.close(code=1000)
                break
        except WebSocketDisconnect:
            logger.info(f"Client {user_id} closed the connection.")
            break
        except RuntimeError as e:
            if "disconnect message" in str(e) or "WebSocket is not connected" in str(e):
                logger.info(f"Client {user_id} closed the connection.")
            else:
                logger.exception(f"RuntimeError in client {user_id}: {e}")
            break
        except Exception:
            logger.exception(f"Error in client {user_id}")
            break

@chat_router.websocket("/notification-server")
async def chat_websocket(
    websocket: WebSocket,
    token: Annotated[str | None, Query(description="Authentication token, supports URL parameter")] = None,
    access_token: Annotated[str | None, Query(description="Access token, supports URL parameter")] = None,
    authorization: Annotated[str | None, Header(description="Bearer authentication header")] = None,
):
    if not server._subscribed:
        server._subscribed = True
        await server.ChatSubscriber.start_subscribe()

    user: User | None = None
    user_id: int | None = None

    try:
        # --- SHORT-LIVED SESSION: only for auth + initial channel setup ---
        # The session is closed before the long _listen_stop loop so it does
        # NOT hold a pool connection for the entire WebSocket lifetime.
        auth_token = token or access_token
        if not auth_token and authorization:
            auth_token = authorization.removeprefix("Bearer ")

        if not auth_token:
            await websocket.close(code=1008, reason="Missing authentication token")
            logger.info("WebSocket rejected: missing authentication token")
            return

        async with AsyncSession(engine) as session:
            try:
                # Keep websocket auth permissive on scope for compatibility with
                # older/custom clients that still use "*" password-grant tokens.
                user_and_token = await get_current_user_and_token(
                    session, SecurityScopes(scopes=[]), token_pw=auth_token
                )
            except HTTPException as auth_error:
                await websocket.close(code=1008, reason="Invalid or expired token")
                logger.info(
                    f"WebSocket rejected: authentication failed "
                    f"(status={auth_error.status_code}, detail={auth_error.detail})"
                )
                return

            await websocket.accept()

            user = user_and_token[0]
            user_id = user.id

            server.connect(user_id, websocket)

            db_channel = (await session.exec(select(ChatChannel).where(ChatChannel.channel_id == 1))).first()
            if db_channel is not None:
                await server.join_channel(user, db_channel)

            pm_channels = (
                await session.exec(
                    select(ChatChannel).where(
                        ChatChannel.type == ChannelType.PM,
                        col(ChatChannel.channel_name).like(f"pm\\_{user_id}\\_%", escape="\\")
                        | col(ChatChannel.channel_name).like(f"pm\\_%\\_{user_id}", escape="\\"),
                    )
                )
            ).all()
            for channel in pm_channels:
                await server.join_channel(user, channel)

            # If the user is restricted, fire a ToriiHalo PM into their
            # bot-PM channel right after their WS comes up. The message
            # lands in the bot-PM channel so they see it in their chat
            # tray every time they reconnect — paired with the frontend
            # banner this is what "you're restricted" surfaces as.
            # Failure here is swallowed: a missing/locked PM channel
            # shouldn't tear down the user's whole WS session.
            try:
                if await user.is_restricted(session):
                    from app.router.notification.banchobot import bot as toriihalo
                    from app.router.auth import RESTRICTED_LOGIN_MESSAGE
                    bot_channel = await toriihalo._ensure_pm_channel(user, session)
                    if bot_channel is not None:
                        await toriihalo._send_message(
                            bot_channel,
                            RESTRICTED_LOGIN_MESSAGE,
                            session,
                        )
            except Exception:
                logger.exception(f"Failed to send restriction PM for user {user_id} on WS connect")
        # --- SESSION CLOSED HERE — pool connection returned before the loop ---

        await _listen_stop(websocket, user_id)
        return  # importante: salimos del handler cuando termina

    except WebSocketDisconnect as e:
        # ✅ desconexión normal: NO traceback
        logger.info(
            f"Client {user_id or 'unknown'} disconnected: "
            f"{e.code}, {getattr(e, 'reason', '')}"
        )
        return
    except Exception:
        logger.exception(f"Websocket crashed for client {user_id}")
        return
    finally:
        if user is not None:
            try:
                # Fresh session for cleanup so the pool is not double-held.
                async with AsyncSession(engine) as cleanup_session:
                    await server.disconnect(user, cleanup_session, websocket)
            except Exception:
                logger.exception(f"Cleanup failed for client {user_id}")

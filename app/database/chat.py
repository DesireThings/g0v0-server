from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, NotRequired, TypedDict

from app.models.model import UTCBaseModel
from app.utils import utcnow

from ._base import DatabaseModel, included, ondemand
from .user import User, UserDict, UserModel

from pydantic import BaseModel
from sqlmodel import (
    VARCHAR,
    BigInteger,
    Column,
    DateTime,
    Field,
    ForeignKey,
    Relationship,
    SQLModel,
    col,
    select,
)
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from app.router.notification.server import ChatServer
# ChatChannel


class ChatUserAttributes(BaseModel):
    can_message: bool
    can_message_error: str | None = None
    last_read_id: int


class ChannelType(str, Enum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    MULTIPLAYER = "MULTIPLAYER"
    SPECTATOR = "SPECTATOR"
    TEMPORARY = "TEMPORARY"
    PM = "PM"
    GROUP = "GROUP"
    SYSTEM = "SYSTEM"
    ANNOUNCE = "ANNOUNCE"
    TEAM = "TEAM"


class MessageType(str, Enum):
    ACTION = "action"
    MARKDOWN = "markdown"
    PLAIN = "plain"


class ChatChannelDict(TypedDict):
    channel_id: int
    description: str
    name: str
    icon: str | None
    type: ChannelType
    uuid: NotRequired[str | None]
    message_length_limit: NotRequired[int]
    moderated: NotRequired[bool]
    current_user_attributes: NotRequired[ChatUserAttributes]
    last_read_id: NotRequired[int | None]
    last_message_id: NotRequired[int | None]
    recent_messages: NotRequired[list["ChatMessageDict"]]
    users: NotRequired[list[int]]


class ChatChannelModel(DatabaseModel[ChatChannelDict]):
    CONVERSATION_INCLUDES: ClassVar[list[str]] = [
        "last_message_id",
        "users",
    ]
    LISTING_INCLUDES: ClassVar[list[str]] = [
        *CONVERSATION_INCLUDES,
        "current_user_attributes",
        "last_read_id",
    ]

    channel_id: int = Field(primary_key=True, index=True, default=None)
    description: str = Field(sa_column=Column(VARCHAR(255), index=True))
    icon: str | None = Field(default=None)
    type: ChannelType = Field(index=True)

    @staticmethod
    def _pm_user_ids_from_channel_name(channel_name: str | None) -> tuple[int, int] | None:
        if not channel_name or not channel_name.startswith("pm_"):
            return None

        parts = channel_name.split("_", maxsplit=2)
        if len(parts) != 3:
            return None

        try:
            user1_id = int(parts[1])
            user2_id = int(parts[2])
        except ValueError:
            return None

        return (user1_id, user2_id)

    @staticmethod
    def _pm_target_user_id_from_channel_name(channel_name: str | None, current_user_id: int) -> int | None:
        user_ids = ChatChannelModel._pm_user_ids_from_channel_name(channel_name)
        if user_ids is None:
            return None

        user1_id, user2_id = user_ids

        if current_user_id == user1_id:
            return user2_id
        if current_user_id == user2_id:
            return user1_id

        return None

    @staticmethod
    async def _pm_target_user_id_from_recent_messages(
        session: AsyncSession,
        channel_id: int,
        current_user_id: int,
    ) -> int | None:
        recent_sender_ids = (
            await session.exec(
                select(ChatMessage.sender_id)
                .where(ChatMessage.channel_id == channel_id)
                .order_by(col(ChatMessage.message_id).desc())
                .limit(100)
            )
        ).all()

        for sender_id in recent_sender_ids:
            if sender_id != current_user_id:
                return int(sender_id)

        return None

    @included
    @staticmethod
    async def name(session: AsyncSession, channel: "ChatChannel", user: User, server: "ChatServer") -> str:
        if channel.type == ChannelType.PM:
            users = server.channels.get(channel.channel_id, [])
            target_user_id = next((u for u in users if u != user.id), None)
            if target_user_id is None:
                target_user_id = ChatChannelModel._pm_target_user_id_from_channel_name(channel.channel_name, user.id)
            if target_user_id is None:
                target_user_id = await ChatChannelModel._pm_target_user_id_from_recent_messages(
                    session,
                    channel.channel_id,
                    user.id,
                )

            if target_user_id is not None:
                target_name = await session.exec(select(User.username).where(User.id == target_user_id))
                username = target_name.first()
                if username:
                    return username

        return channel.channel_name or f"pm_{channel.channel_id}"

    @included
    @staticmethod
    async def moderated(session: AsyncSession, channel: "ChatChannel", user: User) -> bool:
        silence = (
            await session.exec(
                select(SilenceUser).where(
                    SilenceUser.channel_id == channel.channel_id,
                    SilenceUser.user_id == user.id,
                )
            )
        ).first()

        return silence is not None

    @ondemand
    @staticmethod
    async def current_user_attributes(
        session: AsyncSession,
        channel: "ChatChannel",
        user: User,
    ) -> ChatUserAttributes:
        from app.dependencies.database import redis_message_client

        silence = (
            await session.exec(
                select(SilenceUser).where(
                    SilenceUser.channel_id == channel.channel_id,
                    SilenceUser.user_id == user.id,
                )
            )
        ).first()
        can_message = silence is None
        can_message_error = "You are silenced in this channel" if not can_message else None

        # las escribe el notification server, que usa redis_message_client (db 1)
        redis = redis_message_client
        last_read_id_raw = await redis.get(f"chat:{channel.channel_id}:last_read:{user.id}")
        last_msg_raw = await redis.get(f"chat:{channel.channel_id}:last_msg")
        last_msg = int(last_msg_raw) if last_msg_raw and last_msg_raw.isdigit() else None
        last_read_id = int(last_read_id_raw) if last_read_id_raw and last_read_id_raw.isdigit() else (last_msg or 0)

        return ChatUserAttributes(
            can_message=can_message,
            can_message_error=can_message_error,
            last_read_id=last_read_id,
        )

    @ondemand
    @staticmethod
    async def last_read_id(_session: AsyncSession, channel: "ChatChannel", user: User) -> int | None:
        from app.dependencies.database import redis_message_client

        redis = redis_message_client
        last_read_id_raw = await redis.get(f"chat:{channel.channel_id}:last_read:{user.id}")
        last_msg_raw = await redis.get(f"chat:{channel.channel_id}:last_msg")
        last_msg = int(last_msg_raw) if last_msg_raw and last_msg_raw.isdigit() else None
        return int(last_read_id_raw) if last_read_id_raw and last_read_id_raw.isdigit() else last_msg

    @ondemand
    @staticmethod
    async def last_message_id(_session: AsyncSession, channel: "ChatChannel") -> int | None:
        from app.dependencies.database import redis_message_client

        redis = redis_message_client
        last_msg_raw = await redis.get(f"chat:{channel.channel_id}:last_msg")
        return int(last_msg_raw) if last_msg_raw and last_msg_raw.isdigit() else None

    @ondemand
    @staticmethod
    async def recent_messages(
        session: AsyncSession,
        channel: "ChatChannel",
        show_nsfw_media: bool = False,
    ) -> list["ChatMessageDict"]:
        messages = (
            await session.exec(
                select(ChatMessage)
                .where(ChatMessage.channel_id == channel.channel_id)
                .order_by(col(ChatMessage.message_id).desc())
                .limit(50)
            )
        ).all()
        result = [
            await ChatMessageModel.transform(
                msg,
                show_nsfw_media=show_nsfw_media,
            )
            for msg in reversed(messages)
        ]
        return result

    @ondemand
    @staticmethod
    async def users(
        session: AsyncSession,
        channel: "ChatChannel",
        server: "ChatServer",
        user: User,
    ) -> list[int]:
        if channel.type == ChannelType.PUBLIC:
            return []
        users = server.channels.get(channel.channel_id, []).copy()
        if channel.type == ChannelType.PM:
            target_user_id = next((u for u in users if u != user.id), None)
            if target_user_id is None:
                target_user_id = ChatChannelModel._pm_target_user_id_from_channel_name(channel.channel_name, user.id)
            if target_user_id is None:
                target_user_id = await ChatChannelModel._pm_target_user_id_from_recent_messages(
                    session,
                    channel.channel_id,
                    user.id,
                )
            if target_user_id is not None:
                users = [target_user_id, user.id]
            elif not users:
                # osu! clients assume PM channels always have at least one avatar target.
                users = [user.id]
        return users

    @included
    @staticmethod
    async def message_length_limit(_session: AsyncSession, _channel: "ChatChannel") -> int:
        return 1000


class ChatChannel(ChatChannelModel, table=True):
    __tablename__: str = "chat_channels"

    channel_name: str = Field(sa_column=Column(name="name", type_=VARCHAR(50), index=True))

    @classmethod
    async def get(cls, channel: str | int, session: AsyncSession) -> "ChatChannel | None":
        if isinstance(channel, int) or channel.isdigit():
            # Use query instead of get() to ensure the object is fully loaded
            result = await session.exec(select(ChatChannel).where(ChatChannel.channel_id == int(channel)))
            channel_ = result.first()
            if channel_ is not None:
                return channel_
        result = await session.exec(select(ChatChannel).where(ChatChannel.channel_name == channel))
        return result.first()

    @classmethod
    async def get_pm_channel(cls, user1: int, user2: int, session: AsyncSession) -> "ChatChannel | None":
        channel = await cls.get(f"pm_{user1}_{user2}", session)
        if channel is None:
            channel = await cls.get(f"pm_{user2}_{user1}", session)
        return channel


# ChatMessage
class ChatMessageDict(TypedDict):
    channel_id: int
    content: str
    message_id: int
    sender_id: int
    timestamp: datetime
    type: MessageType
    uuid: str | None
    is_action: NotRequired[bool]
    sender: NotRequired[UserDict]


class ChatMessageModel(DatabaseModel[ChatMessageDict]):
    channel_id: int = Field(index=True, foreign_key="chat_channels.channel_id")
    content: str = Field(sa_column=Column(VARCHAR(1000)))
    message_id: int = Field(index=True, primary_key=True, default=None)
    sender_id: int = Field(sa_column=Column(BigInteger, ForeignKey("lazer_users.id"), index=True))
    timestamp: datetime = Field(sa_column=Column(DateTime, index=True), default_factory=utcnow)
    type: MessageType = Field(default=MessageType.PLAIN, index=True, exclude=True)
    uuid: str | None = Field(default=None)

    @included
    @staticmethod
    async def is_action(_session: AsyncSession, db_message: "ChatMessage") -> bool:
        return db_message.type == MessageType.ACTION

    @ondemand
    @staticmethod
    async def sender(
        _session: AsyncSession,
        db_message: "ChatMessage",
        show_nsfw_media: bool = False,
    ) -> UserDict:
        # Build canonical payload first, then apply viewer policy.
        # torii: con LIST_INCLUDES el sender trae equipped_aura + equipped_name_colour + groups, igual
        # que el path live via Redis (redis_message_system usa LIST_INCLUDES). Sin esto el history
        # servido por este DB-fallback (Redis miss) y los mensajes de admin/banchobot llegaban
        # "stripped" y el aura/color de OTROS usuarios no se veia en el chat.
        user_resp = await UserModel.transform(db_message.user, includes=UserModel.LIST_INCLUDES, show_nsfw_media=True)
        return UserModel.apply_nsfw_media_policy(user_resp, show_nsfw_media)


class ChatMessage(ChatMessageModel, table=True):
    __tablename__: str = "chat_messages"
    user: User = Relationship(sa_relationship_kwargs={"lazy": "joined"})
    channel: "ChatChannel" = Relationship()


class SilenceUser(UTCBaseModel, SQLModel, table=True):
    __tablename__: str = "chat_silence_users"
    id: int = Field(primary_key=True, default=None, index=True)
    user_id: int = Field(sa_column=Column(BigInteger, ForeignKey("lazer_users.id"), index=True))
    channel_id: int = Field(foreign_key="chat_channels.channel_id", index=True)
    until: datetime | None = Field(sa_column=Column(DateTime, index=True), default=None)
    reason: str | None = Field(default=None, sa_column=Column(VARCHAR(255), index=True))
    banned_at: datetime = Field(sa_column=Column(DateTime, index=True), default_factory=utcnow)


class UserSilenceResp(SQLModel):
    id: int
    user_id: int

    @classmethod
    def from_db(cls, db_silence: SilenceUser) -> "UserSilenceResp":
        return cls(
            id=db_silence.id,
            user_id=db_silence.user_id,
        )

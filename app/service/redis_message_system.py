"""
Redis-based real-time messaging system.
- Messages are immediately stored in Redis and returned in real time
- Periodically batch stored to the database
- Supports message status synchronization and disaster recovery
"""

import asyncio
from datetime import datetime
import json
from typing import Any

from app.database import ChatMessageDict
from app.database.chat import ChatMessage, ChatMessageModel, MessageType
from app.database.user import User, UserModel
from app.dependencies.database import get_redis_message, with_db
from app.log import logger
from app.utils import bg_tasks, safe_json_dumps
from app.utils import utcnow


class RedisMessageSystem:
    """Redis message system."""

    def __init__(self):
        self.redis: Any = get_redis_message()
        self._batch_timer: asyncio.Task | None = None
        self._running = False
        self.batch_interval = 5.0  # Batch persist once every 5 seconds
        self.max_batch_size = 100  # Process up to 100 messages per batch

    async def send_message(
        self,
        channel_id: int,
        user: User,
        content: str,
        is_action: bool = False,
        user_uuid: str | None = None,
    ) -> "ChatMessageDict":
        """
        Send message - immediately store in Redis and return.

        Args:
            channel_id: Channel ID.
            user: Sending user.
            content: Message content.
            is_action: Whether message is an action.
            user_uuid: User UUID.

        Returns:
            ChatMessageDict: Message response object.
        """
        # Generate message ID and timestamp
        message_id = await self._generate_message_id(channel_id)
        #timestamp = datetime.now()
        timestamp = utcnow()

        # Ensure user ID exists
        if not user.id:
            raise ValueError("User ID is required")

        # Prepare message data
        message_data: "ChatMessageDict" = {
            "message_id": message_id,
            "channel_id": channel_id,
            "sender_id": user.id,
            "content": content,
            "timestamp": timestamp,
            "type": MessageType.ACTION if is_action else MessageType.PLAIN,
            "uuid": user_uuid or "",
            "is_action": is_action,
        }

        # Immediately store in Redis
        await self._store_to_redis(message_id, channel_id, message_data)

        # Create response object
        async with with_db() as session:
            user_resp = await UserModel.transform(user, session=session, includes=User.LIST_INCLUDES)
            message_data["sender"] = user_resp

        logger.info(f"Message {message_id} sent to Redis cache for channel {channel_id}")
        return message_data

    async def get_messages(self, channel_id: int, limit: int = 50, since: int = 0) -> list[ChatMessageDict]:
        """
        Get channel messages - prioritize getting latest messages from Redis.

        Args:
            channel_id: Channel ID.
            limit: Message count limit.
            since: Starting message ID.

        Returns:
            list[ChatMessageDict]: Message list.
        """
        messages: list["ChatMessageDict"] = []

        try:
            # Get latest messages from Redis
            redis_messages = await self._get_from_redis(channel_id, limit, since)

            # Build response object for each message
            async with with_db() as session:
                for msg_data in redis_messages:
                    # Get sender info
                    sender = await session.get(User, msg_data["sender_id"])
                    if sender:
                        user_resp = await UserModel.transform(sender, includes=User.LIST_INCLUDES)

                        from app.database.chat import ChatMessageDict

                        ts_raw = msg_data.get("timestamp")
                        if isinstance(ts_raw, str):
                            if ts_raw.endswith("Z"):
                                ts_parsed = datetime.fromisoformat(ts_raw[:-1] + "+00:00")
                            else:
                                ts_parsed = datetime.fromisoformat(ts_raw)
                        elif isinstance(ts_raw, datetime):
                            ts_parsed = ts_raw
                        else:
                            # assume seconds epoch if integer-like, else fallback
                            try:
                                ts_parsed = datetime.fromtimestamp(int(ts_raw))
                            except Exception:
                                ts_parsed = datetime.now()

                        type_raw = msg_data.get("type")
                        is_action = bool(msg_data.get("is_action")) or str(type_raw or "") == MessageType.ACTION.value
                        try:
                            type_enum = MessageType(type_raw) if isinstance(type_raw, str) else (MessageType.ACTION if is_action else MessageType.PLAIN)
                        except Exception:
                            type_enum = MessageType.ACTION if is_action else MessageType.PLAIN

                        message_resp: ChatMessageDict = {
                            "message_id": msg_data["message_id"],
                            "channel_id": msg_data["channel_id"],
                            "content": msg_data.get("content", ""),
                            "timestamp": ts_parsed,
                            "sender_id": msg_data["sender_id"],
                            "sender": user_resp,
                            "is_action": is_action,
                            "uuid": msg_data.get("uuid") or None,
                            "type": type_enum,
                        }
                        messages.append(message_resp)

            # If not enough Redis messages, backfill from database
            if len(messages) < limit and since == 0:
                await self._backfill_from_database(channel_id, messages, limit)

        except Exception as e:
            logger.error(f"Failed to get messages from Redis: {e}")
            # Fallback to database query
            messages = await self._get_from_database_only(channel_id, limit, since)

        return messages[:limit]

    async def _generate_message_id(self, channel_id: int) -> int:
        """Generate unique message ID - ensure globally unique and strictly increasing."""
        # Use global counter to ensure message IDs across all channels are strictly increasing
        message_id = await self.redis.incr("global_message_id_counter")

        # Also update channel's last message ID for client status synchronization
        await self.redis.set(f"channel:{channel_id}:last_msg_id", message_id)

        return message_id

    async def _store_to_redis(self, message_id: int, channel_id: int, message_data: ChatMessageDict):
        """Store message in Redis."""
        try:
            # Store message data as JSON string
            await self.redis.set(
                f"msg:{channel_id}:{message_id}",
                safe_json_dumps(message_data),
                ex=604800,  # 7 days expiry
            )

            # Add to channel message list (sorted by time)
            channel_messages_key = f"channel:{channel_id}:messages"

            # Check and clean up keys with wrong type
            try:
                key_type = await self.redis.type(channel_messages_key)
                if key_type not in ("none", "zset"):
                    logger.warning(f"Deleting Redis key {channel_messages_key} with wrong type: {key_type}")
                    await self.redis.delete(channel_messages_key)
            except Exception as type_check_error:
                logger.warning(f"Failed to check key type for {channel_messages_key}: {type_check_error}")
                await self.redis.delete(channel_messages_key)

            # Add to channel message list (sorted set)
            await self.redis.zadd(
                channel_messages_key,
                mapping={f"msg:{channel_id}:{message_id}": message_id},
            )

            # Maintain channel message list size (up to 1000 messages)
            await self.redis.zremrangebyrank(channel_messages_key, 0, -1001)

            await self.redis.lpush("pending_messages", f"{channel_id}:{message_id}")
            logger.debug(f"Message {message_id} added to persistence queue")

        except Exception as e:
            logger.error(f"Failed to store message to Redis: {e}")
            raise

    async def _get_from_redis(self, channel_id: int, limit: int = 50, since: int = 0) -> list[ChatMessageDict]:
        """Get messages from Redis."""
        try:
            channel_messages_key = f"channel:{channel_id}:messages"
            try:
                key_type = await self.redis.type(channel_messages_key)
                if key_type not in ("none", "zset"):
                    await self.redis.delete(channel_messages_key)
            except Exception:
                await self.redis.delete(channel_messages_key)
            # Get list of message keys, sorted by message ID
            if since > 0:
                # Get messages after specified ID (ascending order)
                message_keys = await self.redis.zrangebyscore(
                    channel_messages_key,
                    since + 1,
                    "+inf",
                    start=0,
                    num=limit,
                )
            else:
                # Get latest messages (fetch descending, then reverse)
                message_keys = await self.redis.zrevrange(channel_messages_key, 0, limit - 1)

            messages = []
            for key in message_keys:
                try:
                    key_type = await self.redis.type(key)
                    if key_type not in ("none", "string"):
                        logger.warning(f"Removing wrong-type Redis key {key}: {key_type}")
                        await self.redis.zrem(channel_messages_key, key)
                        await self.redis.delete(key)
                        continue
                except Exception as type_err:
                    logger.warning(f"Failed to check type for {key}: {type_err}")
                    await self.redis.zrem(channel_messages_key, key)
                    continue

                raw_data = await self.redis.get(key)
                if raw_data:
                    try:
                        # Parse JSON string into dictionary
                        message_data = json.loads(raw_data)
                        messages.append(message_data)
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode message JSON from {key}: {e}")
                        continue

            # Ensure messages are sorted by ID in ascending order (chronological order)
            messages.sort(key=lambda x: x.get("message_id", 0))

            # If fetching latest messages (since=0), keep descending order (newest first)
            if since == 0:
                messages.reverse()

            return messages

        except Exception as e:
            logger.error(f"Failed to get messages from Redis: {e}")
            return []

    async def _backfill_from_database(self, channel_id: int, existing_messages: list[ChatMessageDict], limit: int):
        """Backfill historical messages from database."""
        try:
            # Find minimum message ID
            min_id = float("inf")
            if existing_messages:
                for msg in existing_messages:
                    if msg["message_id"] is not None and msg["message_id"] < min_id:
                        min_id = msg["message_id"]

            needed = limit - len(existing_messages)

            if needed <= 0:
                return

            async with with_db() as session:
                from sqlmodel import col, select

                query = select(ChatMessage).where(ChatMessage.channel_id == channel_id)

                if min_id != float("inf"):
                    query = query.where(col(ChatMessage.message_id) < min_id)

                query = query.order_by(col(ChatMessage.message_id).desc()).limit(needed)

                db_messages = (await session.exec(query)).all()

                for msg in reversed(db_messages):  # Insert in ascending chronological order
                    msg_resp = await ChatMessageModel.transform(msg, includes=["sender"])
                    existing_messages.insert(0, msg_resp)

        except Exception as e:
            logger.error(f"Failed to backfill from database: {e}")

    async def _get_from_database_only(self, channel_id: int, limit: int, since: int) -> list[ChatMessageDict]:
        """Get messages from database only (fallback mechanism)."""
        try:
            async with with_db() as session:
                from sqlmodel import col, select

                query = select(ChatMessage).where(ChatMessage.channel_id == channel_id)

                if since > 0:
                    # Get messages after specified ID, sorted by ID ascending
                    query = query.where(col(ChatMessage.message_id) > since)
                    query = query.order_by(col(ChatMessage.message_id).asc()).limit(limit)
                else:
                    # Get latest messages, sorted by ID descending (newest first)
                    query = query.order_by(col(ChatMessage.message_id).desc()).limit(limit)

                messages = (await session.exec(query)).all()

                results = await ChatMessageModel.transform_many(messages, includes=["sender"])

                # If since > 0, maintain ascending order; otherwise reverse to ascending chronological order
                if since == 0:
                    results.reverse()

                return results

        except Exception as e:
            logger.error(f"Failed to get messages from database: {e}")
            return []

    async def _batch_persist_to_database(self):
        """Batch persist messages to database."""
        logger.info("Starting batch persistence to database")

        while self._running:
            try:
                # Get pending messages
                message_keys = []
                for _ in range(self.max_batch_size):
                    key = await self.redis.brpop("pending_messages", timeout=1)
                    if key:
                        # key is a (queue_name, value) tuple
                        _, value = key
                        message_keys.append(value)
                    else:
                        break

                if message_keys:
                    await self._process_message_batch(message_keys)
                else:
                    await asyncio.sleep(self.batch_interval)

            except Exception as e:
                logger.error(f"Error in batch persistence: {e}")
                await asyncio.sleep(1)

        logger.info("Stopped batch persistence to database")

    async def _process_message_batch(self, message_keys: list[str]):
        """Process message batch."""
        async with with_db() as session:
            for key in message_keys:
                try:
                    # Parse channel ID and message ID
                    channel_id, message_id = map(int, key.split(":"))

                    # Get message data (JSON string) from Redis
                    raw_data = await self.redis.get(f"msg:{channel_id}:{message_id}")

                    if not raw_data:
                        continue

                    # Parse JSON string into dictionary
                    try:
                        message_data = json.loads(raw_data)
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to decode message JSON for {channel_id}:{message_id}: {e}")
                        continue

                    # Check if message already exists in database
                    existing = await session.get(ChatMessage, int(message_id))
                    if existing:
                        continue

                    # Create database message - using positive ID generated by Redis
                    db_message = ChatMessage(
                        message_id=int(message_id),  # Use positive ID generated by Redis system
                        channel_id=int(message_data["channel_id"]),
                        sender_id=int(message_data["sender_id"]),
                        content=message_data["content"],
                        timestamp=datetime.fromisoformat(message_data["timestamp"]),
                        type=MessageType(message_data["type"]),
                        uuid=message_data.get("uuid") or None,
                    )

                    session.add(db_message)

                    logger.debug(f"Message {message_id} persisted to database")

                except Exception as e:
                    logger.error(f"Failed to process message {key}: {e}")

            # Commit batch
            try:
                await session.commit()
                logger.info(f"Batch of {len(message_keys)} messages committed to database")
            except Exception as e:
                logger.error(f"Failed to commit message batch: {e}")
                await session.rollback()

    def start(self):
        """Start system."""
        if not self._running:
            self._running = True
            self._batch_timer = asyncio.create_task(self._batch_persist_to_database())
            # Initialize message ID counter on startup
            bg_tasks.add_task(self._initialize_message_counter)
            # Start periodic cleanup task
            bg_tasks.add_task(self._periodic_cleanup)
            logger.info("Redis message system started")

    async def _initialize_message_counter(self):
        """Initialize global message ID counter, ensuring starting from database maximum ID."""
        try:
            # Clean up potentially problematic keys
            await self._cleanup_redis_keys()

            async with with_db() as session:
                from sqlmodel import func, select

                # Get maximum message ID in database
                result = await session.exec(select(func.max(ChatMessage.message_id)))
                max_id = result.one() or 0

                # Check counter value in Redis
                current_counter = await self.redis.get("global_message_id_counter")
                current_counter = int(current_counter) if current_counter else 0

                # Set counter to maximum of the two
                initial_counter = max(max_id, current_counter)
                await self.redis.set("global_message_id_counter", initial_counter)

                logger.info(f"Initialized global message ID counter to {initial_counter}")

        except Exception as e:
            logger.error(f"Failed to initialize message counter: {e}")
            # If initialization fails, set a safe starting value
            await self.redis.setnx("global_message_id_counter", 1000000)

    async def _cleanup_redis_keys(self):
        """Clean Redis keys used by chat message transport."""
        try:
            fixed_count = 0

            channel_keys = await self.redis.keys("channel:*:messages")
            for key in channel_keys:
                try:
                    key_type = await self.redis.type(key)
                    if key_type not in ("none", "zset"):
                        logger.warning(f"Cleaning up Redis key {key} with wrong type: {key_type}")
                        await self.redis.delete(key)
                        verify_type = await self.redis.type(key)
                        if verify_type != "none":
                            logger.error(f"Failed to delete problematic key {key}, trying unlink...")
                            await self.redis.unlink(key)
                        fixed_count += 1
                except Exception as cleanup_error:
                    logger.warning(f"Failed to cleanup key {key}: {cleanup_error}")
                    try:
                        await self.redis.delete(key)
                        fixed_count += 1
                    except Exception:
                        try:
                            await self.redis.unlink(key)
                            fixed_count += 1
                        except Exception as final_error:
                            logger.error(f"Critical: Unable to clear problematic key {key}: {final_error}")

            message_keys = await self.redis.keys("msg:*:*")
            for key in message_keys:
                try:
                    key_type = await self.redis.type(key)
                    if key_type not in ("none", "string"):
                        logger.warning(f"Cleaning wrong-type message key {key}: {key_type}")
                        await self.redis.delete(key)
                        verify_type = await self.redis.type(key)
                        if verify_type != "none":
                            logger.error(f"Failed to delete problematic key {key}, trying unlink...")
                            await self.redis.unlink(key)
                        fixed_count += 1
                except Exception as cleanup_error:
                    logger.warning(f"Failed to cleanup key {key}: {cleanup_error}")
                    try:
                        await self.redis.delete(key)
                        fixed_count += 1
                    except Exception:
                        try:
                            await self.redis.unlink(key)
                            fixed_count += 1
                        except Exception as final_error:
                            logger.error(f"Critical: Unable to clear problematic key {key}: {final_error}")

            if fixed_count > 0:
                logger.info(f"Redis keys cleanup completed, fixed {fixed_count} keys")
            else:
                logger.debug("Redis keys cleanup completed, no issues found")

        except Exception as e:
            logger.error(f"Failed to cleanup Redis keys: {e}")

    async def _periodic_cleanup(self):
        """Periodic cleanup task."""
        while self._running:
            try:
                # Run cleanup every 5 minutes
                await asyncio.sleep(300)
                if not self._running:
                    break

                logger.debug("Running periodic Redis keys cleanup...")
                await self._cleanup_redis_keys()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Periodic cleanup error: {e}")
                # Wait 1 minute after error before retrying
                await asyncio.sleep(60)

    def stop(self):
        """Stop system."""
        if self._running:
            self._running = False
            if self._batch_timer:
                self._batch_timer.cancel()
                self._batch_timer = None
            logger.info("Redis message system stopped")


# Global message system instance
redis_message_system = RedisMessageSystem()


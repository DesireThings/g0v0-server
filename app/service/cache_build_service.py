import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from weakref import WeakValueDictionary

from app.log import logger

from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import RedisError

_local_locks: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = WeakValueDictionary()
_LEASE_SECONDS = 15


async def _renew_lease(lock: Lock) -> None:
    try:
        while True:
            await asyncio.sleep(_LEASE_SECONDS / 3)
            await lock.extend(_LEASE_SECONDS, replace_ttl=True)
    except RedisError as error:
        logger.debug(f"Cache build lease renewal failed: {error}")


@asynccontextmanager
async def cache_build_lock(redis: Redis, cache_key: str) -> AsyncIterator[None]:
    key = (asyncio.get_running_loop(), cache_key)
    local_lock = _local_locks.get(key)
    if local_lock is None:
        local_lock = asyncio.Lock()
        _local_locks[key] = local_lock

    async with local_lock:
        lease = redis.lock(f"{cache_key}:build", timeout=_LEASE_SECONDS, thread_local=False)
        try:
            acquired = await lease.acquire()
        except RedisError:
            acquired = False

        if not acquired:
            yield
            return

        renewal = asyncio.create_task(_renew_lease(lease))
        try:
            yield
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            try:
                await lease.release()
            except RedisError as error:
                logger.debug(f"Cache build lease release failed: {error}")

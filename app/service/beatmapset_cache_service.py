"""
Beatmapset cache service.
Used to cache beatmapset data and reduce database query frequency.
"""

import hashlib
import json
from typing import TYPE_CHECKING

from app.config import settings
from app.database import BeatmapsetDict
from app.log import logger
from app.utils import safe_json_dumps

from redis.asyncio import Redis

if TYPE_CHECKING:
    pass


def generate_hash(data) -> str:
    """Generate MD5 hash value of data."""
    content = data if isinstance(data, str) else safe_json_dumps(data)
    return hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()


def _has_missing_beatmap_checksums(payload) -> bool:
    """Detect cached responses produced before the BeatConnect fetcher reorder.

    BeatConnect's set payload returns ``checksum: null`` for every difficulty,
    and that value flows straight through into our previous Redis cache. The
    lazer client then reads ``checksum`` from the response, writes ``""`` into
    ``OnlineMD5Hash``, and trips ``MatchesOnlineVersion`` so neither the status
    pill nor the leaderboard refresh after Torii promotes a graveyard map. We
    detect that shape so the cache layer can self-purge without operator
    intervention.
    """
    if not isinstance(payload, dict):
        return False

    beatmaps = payload.get("beatmaps")
    if not isinstance(beatmaps, list) or not beatmaps:
        return False

    # Locally uploaded sets have no upstream checksum to preserve, so don't
    # treat them as poisoned.
    if payload.get("is_local"):
        return False

    for bm in beatmaps:
        if not isinstance(bm, dict):
            continue
        if bm.get("is_local"):
            continue
        if not (bm.get("checksum") or ""):
            return True
    return False


class BeatmapsetCacheService:
    """Beatmapset cache service."""

    def __init__(self, redis: Redis):
        self.redis = redis
        self._default_ttl = settings.beatmapset_cache_expire_seconds

    def _status_cache_scope(self) -> str:
        all_lb = int(bool(settings.enable_all_beatmap_leaderboard))
        all_pp = int(bool(settings.enable_all_beatmap_pp))
        return f"alllb{all_lb}:allpp{all_pp}"

    def _get_beatmapset_cache_key(self, beatmapset_id: int) -> str:
        return f"beatmapset:{self._status_cache_scope()}:{beatmapset_id}"

    def _get_beatmap_lookup_cache_key(self, beatmap_id: int) -> str:
        """Generate beatmap lookup cache key."""
        return f"beatmap_lookup:{self._status_cache_scope()}:{beatmap_id}:beatmapset"

    def _get_search_cache_key(self, query_hash: str, cursor_hash: str) -> str:
        """Generate search result cache key."""
        return f"beatmapset_search:{query_hash}:{cursor_hash}"

    async def get_beatmapset_from_cache(self, beatmapset_id: int) -> BeatmapsetDict | None:
        """Get beatmapset information from cache."""
        try:
            cache_key = self._get_beatmapset_cache_key(beatmapset_id)
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                payload = json.loads(cached_data)
                if _has_missing_beatmap_checksums(payload):
                    # The cache may contain payloads from before the fetcher
                    # reorder fix — those have `checksum: null` for every
                    # difficulty because BeatConnect was being preferred over
                    # osu! API. Returning that to the lazer client breaks the
                    # `MatchesOnlineVersion` gate downstream and causes stuck
                    # "Leaderboards not available" / stale status. Drop the
                    # entry so the next request hits the database (which now
                    # heals itself via Beatmapset.get_or_fetch).
                    logger.info(
                        f"Discarding cached beatmapset {beatmapset_id}: missing per-difficulty checksums"
                    )
                    await self.redis.delete(cache_key)
                    return None
                logger.debug(f"Beatmapset cache hit for {beatmapset_id}")
                return payload
            return None
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error getting beatmapset from cache: {e}")
            return None

    async def cache_beatmapset(
        self,
        beatmapset_resp: BeatmapsetDict,
        expire_seconds: int | None = None,
    ):
        """Cache beatmapset information."""
        try:
            if expire_seconds is None:
                expire_seconds = self._default_ttl
            cache_key = self._get_beatmapset_cache_key(beatmapset_resp["id"])
            cached_data = safe_json_dumps(beatmapset_resp)
            await self.redis.setex(cache_key, expire_seconds, cached_data)  # type: ignore
            logger.debug(f"Cached beatmapset {beatmapset_resp['id']} for {expire_seconds}s")
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error caching beatmapset: {e}")

    async def get_beatmap_lookup_from_cache(self, beatmap_id: int) -> BeatmapsetDict | None:
        """Get beatmapset information looked up by beatmap ID from cache."""
        try:
            cache_key = self._get_beatmap_lookup_cache_key(beatmap_id)
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                data = json.loads(cached_data)
                if _has_missing_beatmap_checksums(data):
                    # See `get_beatmapset_from_cache`: drop legacy entries that
                    # have null per-difficulty checksums so the next request
                    # heals itself.
                    logger.info(
                        f"Discarding cached beatmap-lookup {beatmap_id}: missing per-difficulty checksums"
                    )
                    await self.redis.delete(cache_key)
                    return None
                logger.debug(f"Beatmap lookup cache hit for {beatmap_id}")
                return data
            return None
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error getting beatmap lookup from cache: {e}")
            return None

    async def cache_beatmap_lookup(
        self,
        beatmap_id: int,
        beatmapset_resp: BeatmapsetDict,
        expire_seconds: int | None = None,
    ):
        """Cache beatmapset information looked up by beatmap ID."""
        try:
            if expire_seconds is None:
                expire_seconds = self._default_ttl
            cache_key = self._get_beatmap_lookup_cache_key(beatmap_id)
            cached_data = safe_json_dumps(beatmapset_resp)
            await self.redis.setex(cache_key, expire_seconds, cached_data)  # type: ignore
            logger.debug(f"Cached beatmap lookup {beatmap_id} for {expire_seconds}s")
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error caching beatmap lookup: {e}")

    async def get_search_from_cache(self, query_hash: str, cursor_hash: str) -> dict | None:
        """Get search results from cache."""
        try:
            cache_key = self._get_search_cache_key(query_hash, cursor_hash)
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                logger.debug(f"Search cache hit for {query_hash[:8]}...{cursor_hash[:8]}")
                return json.loads(cached_data)
            return None
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error getting search from cache: {e}")
            return None

    async def cache_search_result(
        self,
        query_hash: str,
        cursor_hash: str,
        search_result: dict,
        expire_seconds: int | None = None,
    ):
        """Cache search results."""
        try:
            if expire_seconds is None:
                expire_seconds = min(self._default_ttl, 300)  # Short cache time for search results, at most 5 minutes
            cache_key = self._get_search_cache_key(query_hash, cursor_hash)
            cached_data = safe_json_dumps(search_result)
            await self.redis.setex(cache_key, expire_seconds, cached_data)  # type: ignore
            logger.debug(f"Cached search result for {expire_seconds}s")
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error caching search result: {e}")

    async def invalidate_beatmapset_cache(self, beatmapset_id: int):
        """Invalidate beatmapset cache."""
        try:
            cache_key = self._get_beatmapset_cache_key(beatmapset_id)
            await self.redis.delete(cache_key)
            logger.debug(f"Invalidated beatmapset cache for {beatmapset_id}")
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error invalidating beatmapset cache: {e}")

    async def invalidate_beatmap_lookup_cache(self, beatmap_id: int):
        """Invalidate beatmap lookup cache."""
        try:
            cache_key = self._get_beatmap_lookup_cache_key(beatmap_id)
            await self.redis.delete(cache_key)
            logger.debug(f"Invalidated beatmap lookup cache for {beatmap_id}")
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error invalidating beatmap lookup cache: {e}")

    async def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        try:
            beatmapset_keys = await self.redis.keys("beatmapset:*")
            lookup_keys = await self.redis.keys("beatmap_lookup:*")
            search_keys = await self.redis.keys("beatmapset_search:*")

            return {
                "cached_beatmapsets": len(beatmapset_keys),
                "cached_lookups": len(lookup_keys),
                "cached_searches": len(search_keys),
                "total_keys": len(beatmapset_keys) + len(lookup_keys) + len(search_keys),
            }
        except (ValueError, TypeError, AttributeError) as e:
            logger.error(f"Error getting cache stats: {e}")
            return {"error": str(e)}


# Global cache service instance
_cache_service: BeatmapsetCacheService | None = None


def get_beatmapset_cache_service(redis: Redis) -> BeatmapsetCacheService:
    """Get beatmapset cache service instance."""
    global _cache_service
    if _cache_service is None:
        _cache_service = BeatmapsetCacheService(redis)
    return _cache_service


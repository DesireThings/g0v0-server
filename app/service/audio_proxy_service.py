"""
Audio proxy service.
Provides functionality to fetch beatmapset audio previews from official osu! and cache them.
"""

from app.log import logger

from fastapi import HTTPException
import httpx
import redis.asyncio as redis


class AudioProxyService:
    """Audio proxy service."""

    def __init__(self, redis_binary_client: redis.Redis, redis_text_client: redis.Redis):
        self.redis_binary = redis_binary_client
        self.redis_text = redis_text_client
        self.http_client = httpx.AsyncClient(timeout=30.0)
        self._cache_ttl = 7 * 24 * 60 * 60

    async def close(self):
        """Close HTTP client."""
        await self.http_client.aclose()

    def _get_beatmapset_cache_key(self, beatmapset_id: int) -> str:
        """Generate beatmapset audio cache key."""
        return f"beatmapset_audio:{beatmapset_id}"

    def _get_beatmapset_metadata_key(self, beatmapset_id: int) -> str:
        """Generate beatmapset audio metadata cache key."""
        return f"beatmapset_audio_meta:{beatmapset_id}"

    async def get_beatmapset_audio_from_cache(self, beatmapset_id: int) -> tuple[bytes, str] | None:
        """Get beatmapset audio data and content type from cache."""
        try:
            cache_key = self._get_beatmapset_cache_key(beatmapset_id)
            metadata_key = self._get_beatmapset_metadata_key(beatmapset_id)

            # Get audio data (binary) and metadata (text)
            audio_data = await self.redis_binary.get(cache_key)
            metadata = await self.redis_text.get(metadata_key)

            if audio_data and metadata:
                logger.debug(f"Beatmapset audio cache hit for ID: {beatmapset_id}")
                # audio_data is already bytes type, metadata is str type
                return audio_data, metadata
            return None
        except (redis.RedisError, redis.ConnectionError) as e:
            logger.error(f"Error getting beatmapset audio from cache: {e}")
            return None

    async def cache_beatmapset_audio(self, beatmapset_id: int, audio_data: bytes, content_type: str):
        """Cache beatmapset audio data."""
        try:
            cache_key = self._get_beatmapset_cache_key(beatmapset_id)
            metadata_key = self._get_beatmapset_metadata_key(beatmapset_id)

            # Cache audio data (binary) and metadata (text)
            await self.redis_binary.setex(cache_key, self._cache_ttl, audio_data)
            await self.redis_text.setex(metadata_key, self._cache_ttl, content_type)

            logger.debug(f"Cached beatmapset audio for ID: {beatmapset_id}, size: {len(audio_data)} bytes")
        except (redis.RedisError, redis.ConnectionError) as e:
            logger.error(f"Error caching beatmapset audio: {e}")

    async def fetch_beatmapset_audio(self, beatmapset_id: int) -> tuple[bytes, str]:
        """Fetch beatmapset audio preview from official osu!."""
        try:
            # Construct official osu! audio preview URL
            preview_url = f"https://b.ppy.sh/preview/{beatmapset_id}.mp3"
            logger.info(f"Fetching beatmapset audio from: {preview_url}")

            response = await self.http_client.get(preview_url)
            response.raise_for_status()

            # osu! audio previews are typically in mp3 format
            content_type = response.headers.get("content-type", "audio/mpeg")
            audio_data = response.content

            # Check file size limit (10MB, preview audio is usually not very large)
            max_size = 10 * 1024 * 1024  # 10MB
            if len(audio_data) > max_size:
                raise HTTPException(
                    status_code=413, detail=f"Audio file too large: {len(audio_data)} bytes (max: {max_size})"
                )

            if len(audio_data) == 0:
                raise HTTPException(status_code=404, detail="Audio preview not available for this beatmapset")

            logger.info(f"Successfully fetched beatmapset audio: {len(audio_data)} bytes, type: {content_type}")
            return audio_data, content_type

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching beatmapset audio for ID {beatmapset_id}: {e}")
            if e.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Audio preview not found for this beatmapset") from e
            else:
                raise HTTPException(
                    status_code=e.response.status_code, detail=f"Failed to fetch audio: {e.response.status_code}"
                ) from e
        except httpx.RequestError as e:
            logger.error(f"Request error fetching beatmapset audio for ID {beatmapset_id}: {e}")
            raise HTTPException(status_code=503, detail="Failed to connect to osu! servers") from e
        except Exception as e:
            logger.error(f"Unexpected error fetching beatmapset audio for ID {beatmapset_id}: {e}")
            raise HTTPException(status_code=500, detail="Internal server error while fetching audio") from e

    async def get_beatmapset_audio(self, beatmapset_id: int) -> tuple[bytes, str]:
        """Get audio preview by beatmapset_id."""
        # Try to get from cache first
        cached_result = await self.get_beatmapset_audio_from_cache(beatmapset_id)
        if cached_result:
            return cached_result

        # Cache miss, fetch from official osu!
        audio_data, content_type = await self.fetch_beatmapset_audio(beatmapset_id)

        # Cache newly fetched audio data
        await self.cache_beatmapset_audio(beatmapset_id, audio_data, content_type)

        return audio_data, content_type


def get_audio_proxy_service(redis_binary_client: redis.Redis, redis_text_client: redis.Redis) -> AudioProxyService:
    """Get audio proxy service instance."""
    # Create a new instance each time to avoid global state
    return AudioProxyService(redis_binary_client, redis_text_client)

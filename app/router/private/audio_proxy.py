"""
Audio proxy interface
Provides proxy service for fetching beatmapset audio previews from official osu!
"""

from typing import Annotated

from app.dependencies.database import get_redis, get_redis_binary
from app.service.audio_proxy_service import AudioProxyService, get_audio_proxy_service

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import Response
from fastapi_limiter.depends import RateLimiter
from loguru import logger
import redis.asyncio as redis

router = APIRouter(prefix="/audio", tags=["Audio Proxy"])


async def get_audio_proxy_dependency(
    redis_binary_client: Annotated[redis.Redis, Depends(get_redis_binary)],
    redis_text_client: Annotated[redis.Redis, Depends(get_redis)],
) -> AudioProxyService:
    """Audio proxy service dependency injection"""
    return get_audio_proxy_service(redis_binary_client, redis_text_client)


@router.get(
    "/beatmapset/{beatmapset_id}",
    dependencies=[
        Depends(RateLimiter(times=30, minutes=1)),  # Max 30 requests per minute
        Depends(RateLimiter(times=5, seconds=10)),  # Max 5 requests per 10 seconds
    ],
)
async def get_beatmapset_audio(
    beatmapset_id: Annotated[int, Path(description="Beatmapset ID", ge=1)],
    audio_service: Annotated[AudioProxyService, Depends(get_audio_proxy_dependency)],
):
    """
    Get audio preview for a beatmapset

    Fetch the official osu! audio preview file by beatmapset ID.
    Audio files are cached for 7 days to improve response time.

    Rate limits:
    - Max 30 requests per minute
    - Max 5 requests per 10 seconds

    Parameters:
    - beatmapset_id: Beatmapset ID

    Returns:
    - Binary audio data with Content-Type audio/mpeg
    """
    try:
        # Get beatmapset audio data
        audio_data, content_type = await audio_service.get_beatmapset_audio(beatmapset_id)

        # Return audio response
        return Response(
            content=audio_data,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=604800",  # 7-day cache
                "Content-Length": str(len(audio_data)),
                "Content-Disposition": f'inline; filename="{beatmapset_id}.mp3"',
            },
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting beatmapset audio: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging

from fastapi import HTTPException
import httpx

logger = logging.getLogger(__name__)


@dataclass
class DownloadEndpoint:
    """Download endpoint configuration."""

    name: str
    base_url: str
    health_check_url: str
    url_template: str  # Download URL template, using {sid} and {type} placeholders
    is_china: bool = False
    priority: int = 0  # Priority, smaller number means higher priority
    timeout: int = 10  # Health check timeout in seconds


@dataclass
class EndpointStatus:
    """Endpoint status."""

    endpoint: DownloadEndpoint
    is_healthy: bool = True
    last_check: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class BeatmapDownloadService:
    """Beatmap download service - load balancing and health checking."""

    def __init__(self):
        # China region endpoints
        self.china_endpoints = [
            DownloadEndpoint(
                name="Sayobot",
                base_url="https://dl.sayobot.cn",
                health_check_url="https://dl.sayobot.cn/",
                url_template="https://dl.sayobot.cn/beatmaps/download/{type}/{sid}",
                is_china=True,
                priority=0,
                timeout=5,
            )
        ]

        # International endpoints, ordered by historical reliability under
        # actual load (measured 2026-05-08 from this server's IP):
        #   0. Nerinyan — fastest 302 (~30ms), but its CDN often 404s on
        #      maps that exist elsewhere; effective TTFB after follow ranges
        #      from 1.5s (cache hit on their CDN) to 5s (404 tail).
        #   1. osu.direct — slower (~600ms-2s) but consistent 200s for
        #      maps that exist anywhere.
        #   2. Akatsuki — even slower (~3-6s) but the most complete catalog
        #      we've found; lifeline for maps no other free mirror has.
        #   3. Gatari — older mirror, smaller catalog, occasional flaky.
        # Catboy is intentionally NOT in this list (operator says it's
        # unreliable). Chimu was removed (DNS dead, project shut down).
        # Sayobot is in china_endpoints only — its CDN is geo-fenced.
        self.international_endpoints = [
            DownloadEndpoint(
                name="Nerinyan",
                base_url="https://api.nerinyan.moe",
                health_check_url="https://api.nerinyan.moe/d/1",
                url_template="https://api.nerinyan.moe/d/{sid}",
                is_china=False,
                priority=0,
                timeout=10,
            ),
            DownloadEndpoint(
                name="osu.direct",
                base_url="https://osu.direct",
                health_check_url="https://osu.direct/api",
                url_template="https://osu.direct/api/d/{sid}",
                is_china=False,
                priority=1,
                timeout=10,
            ),
            DownloadEndpoint(
                name="Akatsuki",
                base_url="https://akatsuki.gg",
                health_check_url="https://akatsuki.gg/d/1",
                url_template="https://akatsuki.gg/d/{sid}",
                is_china=False,
                priority=2,
                timeout=15,
            ),
            DownloadEndpoint(
                name="Gatari",
                base_url="https://osu.gatari.pw",
                health_check_url="https://osu.gatari.pw/d/1",
                url_template="https://osu.gatari.pw/d/{sid}",
                is_china=False,
                priority=3,
                timeout=10,
            ),
        ]

        # Endpoint status tracking
        self.endpoint_status: dict[str, EndpointStatus] = {}
        self._initialize_status()

        # Health check configuration
        self.health_check_interval = 600  # Health check interval in seconds
        self.max_consecutive_failures = 3  # Maximum consecutive failures
        self.health_check_running = False
        self.health_check_task = None  # Reference to health check task

        # HTTP client
        self.http_client = httpx.AsyncClient(timeout=30)

    def _initialize_status(self):
        """Initialize endpoint statuses."""
        all_endpoints = self.china_endpoints + self.international_endpoints
        for endpoint in all_endpoints:
            self.endpoint_status[endpoint.name] = EndpointStatus(endpoint=endpoint)

    async def start_health_check(self):
        """Start health check task."""
        if self.health_check_running:
            return

        self.health_check_running = True
        self.health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info("Beatmap download service health check started")

    async def stop_health_check(self):
        """Stop health check task."""
        self.health_check_running = False
        await self.http_client.aclose()
        logger.info("Beatmap download service health check stopped")

    async def _health_check_loop(self):
        """Health check loop."""
        while self.health_check_running:
            try:
                await self._check_all_endpoints()
                await asyncio.sleep(self.health_check_interval)
            except Exception as e:
                logger.error(f"Health check loop error: {e}")
                await asyncio.sleep(5)  # Brief wait on error

    async def _check_all_endpoints(self):
        """Check health status of all endpoints."""
        all_endpoints = self.china_endpoints + self.international_endpoints

        # Concurrently check all endpoints
        tasks = []
        for endpoint in all_endpoints:
            task = asyncio.create_task(self._check_endpoint_health(endpoint))
            tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_endpoint_health(self, endpoint: DownloadEndpoint):
        """Check health status of a single endpoint."""
        status = self.endpoint_status[endpoint.name]

        try:
            async with httpx.AsyncClient(timeout=endpoint.timeout) as client:
                response = await client.get(endpoint.health_check_url)

                # Determine health status based on endpoint response
                is_healthy = False
                # 200, 302 (redirect), 304 (not modified) all indicate a live endpoint
                is_healthy = response.status_code in [200, 302, 304]

                if is_healthy:
                    # Health check succeeded
                    if not status.is_healthy:
                        logger.info(f"Endpoint {endpoint.name} is now healthy")

                    status.is_healthy = True
                    status.consecutive_failures = 0
                    status.last_error = None
                else:
                    raise httpx.HTTPStatusError(
                        f"Health check failed with status {response.status_code}",
                        request=response.request,
                        response=response,
                    )

        except Exception as e:
            # Health check failed
            status.consecutive_failures += 1
            status.last_error = str(e)

            if status.consecutive_failures >= self.max_consecutive_failures:
                if status.is_healthy:
                    logger.warning(
                        f"Endpoint {endpoint.name} marked as unhealthy after "
                        f"{status.consecutive_failures} consecutive failures: {e}"
                    )
                status.is_healthy = False

        finally:
            status.last_check = datetime.now()

    def get_healthy_endpoints(self, is_china: bool) -> list[DownloadEndpoint]:
        """Get list of healthy endpoints."""
        endpoints = self.china_endpoints if is_china else self.international_endpoints

        healthy_endpoints = []
        for endpoint in endpoints:
            status = self.endpoint_status[endpoint.name]
            if status.is_healthy:
                healthy_endpoints.append(endpoint)

        # Sort by priority
        healthy_endpoints.sort(key=lambda x: x.priority)
        return healthy_endpoints

    def _get_endpoint_pool(self, is_china: bool) -> list[DownloadEndpoint]:
        """Get endpoint pool. Chinese users prioritize China mirrors, falling back to international mirrors on failure."""
        if is_china:
            return self.china_endpoints + self.international_endpoints
        return self.international_endpoints

    def _build_download_url(self, endpoint: DownloadEndpoint, beatmapset_id: int, no_video: bool) -> str:
        """Generate download URL based on endpoint configuration."""
        if endpoint.name == "Sayobot":
            video_type = "novideo" if no_video else "full"
            return endpoint.url_template.format(type=video_type, sid=beatmapset_id)
        return endpoint.url_template.format(sid=beatmapset_id)

    def get_download_urls(self, beatmapset_id: int, no_video: bool, is_china: bool) -> list[str]:
        """Get list of download URLs (sorted by priority, healthy endpoints first)."""
        endpoints = self._get_endpoint_pool(is_china)
        if not endpoints:
            return []

        healthy = [ep for ep in sorted(endpoints, key=lambda x: x.priority) if self.endpoint_status[ep.name].is_healthy]
        healthy_names = {ep.name for ep in healthy}
        unhealthy = [ep for ep in sorted(endpoints, key=lambda x: x.priority) if ep.name not in healthy_names]
        ordered_endpoints = healthy + unhealthy

        return [self._build_download_url(ep, beatmapset_id, no_video) for ep in ordered_endpoints]

    def get_download_url(self, beatmapset_id: int, no_video: bool, is_china: bool) -> str:
        """Get download URL with load balancing and failover."""
        urls = self.get_download_urls(beatmapset_id=beatmapset_id, no_video=no_video, is_china=is_china)
        if not urls:
            raise HTTPException(status_code=503, detail="No download endpoints available")
        return urls[0]

    def get_service_status(self) -> dict:
        """Get service status information."""
        status_info = {
            "service_running": self.health_check_running,
            "last_update": datetime.now().isoformat(),
            "endpoints": {},
        }

        for name, status in self.endpoint_status.items():
            status_info["endpoints"][name] = {
                "healthy": status.is_healthy,
                "last_check": status.last_check.isoformat() if status.last_check else None,
                "consecutive_failures": status.consecutive_failures,
                "last_error": status.last_error,
                "priority": status.endpoint.priority,
                "is_china": status.endpoint.is_china,
            }

        return status_info


# Global service instance
download_service = BeatmapDownloadService()

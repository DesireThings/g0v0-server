import asyncio
from datetime import datetime
import time

from app.dependencies.database import get_redis
from app.log import fetcher_logger
from app.utils import utcnow

from httpx import AsyncClient, HTTPStatusError, TimeoutException


class TokenAuthError(Exception):
    """Token authorization failure exception."""

    pass


class PassiveRateLimiter:
    """
    Passive rate limiter.
    When receiving a 429 response, reads the Retry-After header and pauses all requests.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._retry_after_time: float | None = None
        self._waiting_tasks: set[asyncio.Task] = set()

    async def wait_if_limited(self) -> None:
        """Wait until the rate-limit window expires, without holding the lock
        during the sleep so concurrent callers don't serialise behind each other."""
        while True:
            async with self._lock:
                if self._retry_after_time is None:
                    return
                wait_seconds = self._retry_after_time - time.time()
                if wait_seconds <= 0:
                    self._retry_after_time = None
                    return
            # Sleep outside the lock so other coroutines can also check.
            logger.warning(f"Rate limited, waiting {wait_seconds:.2f} seconds")
            await asyncio.sleep(wait_seconds)

    async def handle_rate_limit(self, retry_after: str | int | None) -> None:
        """
        Handle 429 response and set rate limit wait time.

        Args:
            retry_after: Value of the Retry-After header, either seconds or an HTTP date string.
        """
        async with self._lock:
            if retry_after is None:
                # If there is no Retry-After header, default to waiting 60 seconds
                wait_seconds = 60
            elif isinstance(retry_after, int):
                wait_seconds = retry_after
            elif retry_after.isdigit():
                wait_seconds = int(retry_after)
            else:
                # Try to parse HTTP date format
                try:
                    retry_time = datetime.strptime(retry_after, "%a, %d %b %Y %H:%M:%S %Z")
                    #wait_seconds = max(0, (retry_time - datetime.utcnow()).total_seconds())
                    wait_seconds = max(0, (retry_time - utcnow()).total_seconds())
                except ValueError:
                    # Parsing failed, default to waiting 60 seconds
                    wait_seconds = 60

            self._retry_after_time = time.time() + wait_seconds
            logger.warning(f"Rate limit triggered, will retry after {wait_seconds} seconds")


logger = fetcher_logger("Fetcher")


class BaseFetcher:
    # Class-level rate limiter, shared across all instances
    _rate_limiter = PassiveRateLimiter()

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        scope: list[str] = ["public"],
        callback_url: str = "",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.token_expiry: int = 0
        self.callback_url: str = callback_url
        self.scope = scope
        self._token_lock = asyncio.Lock()

    # NOTE: Reserve for user-based fetchers
    # @property
    # def authorize_url(self) -> str:
    #     return (
    #         f"https://osu.ppy.sh/oauth/authorize?client_id={self.client_id}"
    #         f"&response_type=code&scope={quote(' '.join(self.scope))}"
    #         f"&redirect_uri={self.callback_url}"
    #     )

    @property
    def header(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def request_api(self, url: str, method: str = "GET", **kwargs) -> dict:
        """
        Send API request with passive rate limiting support.
        """
        await self.ensure_valid_access_token()

        headers = kwargs.pop("headers", {}).copy()
        attempt = 0

        while attempt < 2:
            # Wait for rate limit before sending request
            await self._rate_limiter.wait_if_limited()

            request_headers = {**headers, **self.header}
            request_kwargs = kwargs.copy()

            async with AsyncClient() as client:
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=request_headers,
                        **request_kwargs,
                    )
                    response.raise_for_status()
                    return response.json()

                except HTTPStatusError as e:
                    # Handle 429 rate limit response
                    if e.response.status_code == 429:
                        retry_after = e.response.headers.get("Retry-After")
                        logger.warning(f"Rate limited for {url}, Retry-After: {retry_after}")
                        await self._rate_limiter.handle_rate_limit(retry_after)
                        # Retry current request after rate limiting (without incrementing attempt)
                        continue

                    # Handle 401 unauthorized response
                    if e.response.status_code == 401:
                        attempt += 1
                        logger.warning(f"Received 401 error for {url}, attempt {attempt}")
                        await self._handle_unauthorized()
                        continue

                    # Re-raise other HTTP errors directly
                    raise

        await self._clear_access_token()
        logger.warning(f"Failed to authorize after retries for {url}, cleaned up tokens")
        await self.grant_access_token()
        raise TokenAuthError(f"Failed to authorize after retries for {url}")

    def is_token_expired(self) -> bool:
        if not isinstance(self.token_expiry, int):
            return True
        return self.token_expiry <= int(time.time()) or not self.access_token

    async def grant_access_token(self, retries: int = 3, backoff: float = 1.0) -> None:
        last_error: Exception | None = None
        async with AsyncClient(timeout=30.0) as client:
            for attempt in range(1, retries + 1):
                try:
                    response = await client.post(
                        "https://osu.ppy.sh/oauth/token",
                        data={
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "grant_type": "client_credentials",
                            "scope": "public",
                        },
                    )
                    response.raise_for_status()
                    token_data = response.json()
                    self.access_token = token_data["access_token"]
                    self.token_expiry = int(time.time()) + token_data["expires_in"]
                    redis = get_redis()
                    await redis.set(
                        f"fetcher:access_token:{self.client_id}",
                        self.access_token,
                        ex=token_data["expires_in"],
                    )
                    await redis.set(
                        f"fetcher:expire_at:{self.client_id}",
                        self.token_expiry,
                        ex=token_data["expires_in"],
                    )
                    logger.success(
                        f"Granted new access token for client {self.client_id}, "
                        f"expires in {token_data['expires_in']} seconds"
                    )
                    return

                except TimeoutException as exc:
                    last_error = exc
                    logger.warning(
                        f"Timed out while requesting access token for "
                        f"client {self.client_id} (attempt {attempt}/{retries})"
                    )
                except HTTPStatusError as exc:
                    last_error = exc
                    logger.warning(
                        f"HTTP error while requesting access token for client {self.client_id}"
                        f" (status: {exc.response.status_code}, attempt {attempt}/{retries})"
                    )
                except Exception as exc:
                    last_error = exc
                    logger.exception(
                        f"Unexpected error while requesting access token for client {self.client_id}"
                        f" (attempt {attempt}/{retries})"
                    )

                if attempt < retries:
                    await asyncio.sleep(backoff * attempt)

        raise TokenAuthError("Failed to grant access token after retries") from last_error

    async def ensure_valid_access_token(self) -> None:
        if self.is_token_expired():
            await self.grant_access_token()

    async def _handle_unauthorized(self) -> None:
        await self.grant_access_token()

    async def _clear_access_token(self) -> None:
        logger.warning(f"Clearing access token for client {self.client_id}")

        self.access_token = ""
        self.token_expiry = 0

        redis = get_redis()
        await redis.delete(f"fetcher:access_token:{self.client_id}")
        await redis.delete(f"fetcher:expire_at:{self.client_id}")

"""
FastAPI session verification middleware.

Based on osu-web session verification system, adapted for FastAPI framework.
"""

from collections.abc import Callable
from typing import ClassVar

from app.auth import get_token_by_access_token
from app.const import SUPPORT_TOTP_VERIFICATION_VER
from app.database.user import User
from app.database.verification import LoginSession
from app.dependencies.database import get_redis, with_db
from app.log import log
from app.service.verification_service import LoginSessionService
from app.utils import extract_user_agent

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

logger = log("Middleware")


class SessionState:
    """Session state class.

    Simplified session state management.
    """

    def __init__(self, session: LoginSession, user: User, redis: Redis, db: AsyncSession, api_version: int = 0) -> None:
        self.session = session
        self.user = user
        self.redis = redis
        self.db = db
        self.api_version = api_version
        self._verification_method: str | None = None

    def is_verified(self) -> bool:
        """Check if session is verified."""
        return self.session.is_verified

    async def get_method(self) -> str:
        """Get verification method."""
        if self._verification_method is None:
            # Get configured method from Redis
            token_id = self.session.token_id
            if token_id is not None:
                self._verification_method = await LoginSessionService.get_login_method(
                    self.user.id, token_id, self.redis
                )

            if self._verification_method is None:
                if self.api_version < SUPPORT_TOTP_VERIFICATION_VER:
                    self._verification_method = "mail"
                    return self._verification_method

                await self.user.awaitable_attrs.totp_key
                totp_key = self.user.totp_key
                self._verification_method = "totp" if totp_key else "mail"

                token_id = self.session.token_id
                if token_id is not None:
                    await LoginSessionService.set_login_method(
                        self.user.id, token_id, self._verification_method, self.redis
                    )

        return self._verification_method

    async def mark_verified(self) -> None:
        """Mark session as verified."""
        try:
            token_id = self.session.token_id
            if token_id is not None:
                await LoginSessionService.mark_session_verified(
                    self.db,
                    self.redis,
                    self.user.id,
                    token_id,
                    self.session.ip_address,
                    extract_user_agent(self.session.user_agent),
                    self.session.web_uuid,
                )
        except Exception as e:
            logger.error(f"Error marking verified: {e}")

    async def issue_mail_if_needed(self) -> None:
        """Send verification email if needed."""
        try:
            if await self.get_method() == "mail":
                from app.service.verification_service import EmailVerificationService

                # Email sending can be triggered here
                await EmailVerificationService.send_verification_email(
                    self.db,
                    self.redis,
                    self.user.id,
                    self.user.username,
                    self.user.email,
                    None,
                    None,
                    self.user.country_code,
                )
        except Exception as e:
            logger.error(f"Error issuing mail: {e}")

    def get_key(self) -> str:
        """Get session key."""
        return str(self.session.id) if self.session.id else ""

    @property
    def key_for_event(self) -> str:
        """Get session key for event broadcasting."""
        return LoginSessionService.get_key_for_event(self.get_key())

    @property
    def user_id(self) -> int:
        """Get user ID."""
        return self.user.id


class VerifySessionMiddleware(BaseHTTPMiddleware):
    """Session verification middleware.

    Referenced from osu-web VerifyUser middleware, adapted for FastAPI.
    """

    # Routes that skip verification
    SKIP_VERIFICATION_ROUTES: ClassVar[set[str]] = {
        "/api/v2/session/verify",
        "/api/v2/session/verify/reissue",
        "/api/v2/session/verify/mail-fallback",
        "/api/v2/me",
        "/api/v2/me/",
        "/api/v2/logout",
        "/oauth/token",
        "/health",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
    }

    # Route prefixes that always require verification
    ALWAYS_VERIFY_PATTERNS: ClassVar[set[str]] = {
        "/api/private/admin/",
    }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch logic."""
        # Check if verification should be skipped
        if self._should_skip_verification(request):
            return await call_next(request)

        # Fast path: safe methods (GET/HEAD/OPTIONS) never need verification
        # unless the route is in ALWAYS_VERIFY_PATTERNS. Skip expensive DB
        # lookups (2 extra sessions + 3 queries) for the vast majority of requests.
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            path = request.url.path
            needs_verify = any(path.startswith(p) for p in self.ALWAYS_VERIFY_PATTERNS)
            if not needs_verify:
                return await call_next(request)

        # Get current user
        user = await self._get_current_user(request)
        if not user:
            # Unauthenticated users skip verification
            return await call_next(request)

        # Get session state
        session_state = await self._get_session_state(request, user)
        if not session_state:
            # No session state, continue request
            return await call_next(request)

        # Check if already verified
        if session_state.is_verified():
            return await call_next(request)

        # Check if verification is required
        if not self._requires_verification(request, user):
            return await call_next(request)

        # Initiate verification flow
        return await self._initiate_verification(session_state)

    def _should_skip_verification(self, request: Request) -> bool:
        """Check if verification should be skipped."""
        path = request.url.path

        # Exact match routes to skip
        if path in self.SKIP_VERIFICATION_ROUTES:
            return True

        # Skip non-API requests
        return bool(not path.startswith("/api/"))

    def _requires_verification(self, request: Request, user: User) -> bool:
        """Check if verification is required."""
        path = request.url.path
        method = request.method

        # Check if route always requires verification
        for pattern in self.ALWAYS_VERIFY_PATTERNS:
            if path.startswith(pattern):
                return True

        if not user.is_active:
            return True

        # Safe methods (GET/HEAD/OPTIONS) generally do not require verification
        safe_methods = {"GET", "HEAD", "OPTIONS"}
        if method in safe_methods:
            return False

        # Mutation operations (POST/PUT/DELETE/PATCH) require verification
        return method in {"POST", "PUT", "DELETE", "PATCH"}

    async def _get_current_user(self, request: Request) -> User | None:
        """Get current user."""
        try:
            # Extract token from Authorization header
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return None

            token = auth_header[7:]  # Remove "Bearer " prefix

            # Create dedicated database session
            async with with_db() as db:
                # Get token record
                token_record = await get_token_by_access_token(db, token)
                if not token_record:
                    return None

                # Get user
                user = (await db.exec(select(User).where(User.id == token_record.user_id))).first()
                return user

        except Exception as e:
            logger.debug(f"Error getting user: {e}")
            return None

    async def _get_session_state(self, request: Request, user: User) -> SessionState | None:
        """Get session state."""
        try:
            # Extract session token (simplified here to use the same auth token)
            auth_header = request.headers.get("Authorization", "")
            api_version = 0
            raw_api_version = request.headers.get("x-api-version")
            if raw_api_version is not None:
                try:
                    api_version = int(raw_api_version)
                except ValueError:
                    api_version = 0

            if not auth_header.startswith("Bearer "):
                return None

            session_token = auth_header[7:]

            # Get database and Redis connections
            async with with_db() as db:
                redis = get_redis()

                # Find session
                session = await LoginSessionService.find_for_verification(db, session_token)
                if not session or session.user_id != user.id:
                    return None

                return SessionState(session, user, redis, db, api_version)

        except Exception as e:
            logger.error(f"Error getting session state: {e}")
            return None

    async def _initiate_verification(self, state: SessionState) -> Response:
        """Initiate verification flow."""
        try:
            method = await state.get_method()
            if method == "mail":
                await state.issue_mail_if_needed()

            # Return verification required response
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"method": method, "message": "Session verification required"},
            )

        except Exception as e:
            logger.error(f"Error initiating verification: {e}")
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"error": "Verification initiation failed"}
            )

from typing import Annotated

from app.auth import get_token_by_access_token
from app.config import settings
from app.const import SUPPORT_TOTP_VERIFICATION_VER
from app.database import User
from app.database.auth import OAuthToken, V1APIKeys
from app.models.oauth import OAuth2ClientCredentialsBearer

from .api_version import APIVersion
from .database import Database, get_redis

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import (
    APIKeyQuery,
    HTTPBearer,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    SecurityScopes,
)
from fastapi.security.utils import get_authorization_scheme_param
from redis.asyncio import Redis
from sqlmodel import select

security = HTTPBearer()
RESTRICTED_ACCESS_DETAIL = (
    "Your account is banned from Torii. "
    "Wait 1 month without any further offenses before trying again."
)


oauth2_password = OAuth2PasswordBearer(
    tokenUrl="oauth/token",
    refreshUrl="oauth/token",
    scopes={"*": "Allow access to all APIs."},
    description="osu!lazer or web client password login authentication, with full permissions",
    scheme_name="Password Grant",
    auto_error=False,
)

oauth2_code = OAuth2AuthorizationCodeBearer(
    authorizationUrl="oauth/authorize",
    tokenUrl="oauth/token",
    refreshUrl="oauth/token",
    scopes={
        "chat.read": "Allow reading chat messages on behalf of user.",
        "chat.write": "Allow sending chat messages on behalf of user.",
        "chat.write_manage": ("Allow joining and leaving chat channels on behalf of user."),
        "delegate": ("Allow acting as client owner; only applicable for client credentials grant."),
        "forum.write": "Allow creating and editing forum posts on behalf of user.",
        "friends.read": "Allow reading user's friend list.",
        "identify": "Allow reading user's public profile (/me).",
        "public": "Allow reading public data on behalf of user.",
    },
    description="osu! OAuth authentication (Authorization Code Grant)",
    scheme_name="Authorization Code Grant",
    auto_error=False,
)

oauth2_client_credentials = OAuth2ClientCredentialsBearer(
    tokenUrl="oauth/token",
    refreshUrl="oauth/token",
    scopes={
        "public": "Allow reading public data.",
    },
    description="osu! OAuth authentication (Client Credentials Grant)",
    scheme_name="Client Credentials Grant",
    auto_error=False,
)

v1_api_key = APIKeyQuery(name="k", scheme_name="V1 API Key", description="v1 API key")


async def v1_authorize(
    db: Database,
    api_key: Annotated[str, Depends(v1_api_key)],
):
    """V1 API Key authorization"""
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    api_key_record = (await db.exec(select(V1APIKeys).where(V1APIKeys.key == api_key))).first()
    if not api_key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")


async def get_client_user_and_token(
    db: Database,
    token: Annotated[str | None, Depends(oauth2_password)],
) -> tuple[User, OAuthToken]:
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token_record = await get_token_by_access_token(db, token)
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = (await db.exec(select(User).where(User.id == token_record.user_id))).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # The website (web client) keeps READ access while restricted, so a restricted
    # player can still log in and browse their own profile read-only and see the
    # restriction banner. Every WRITE endpoint blocks restricted users on its own
    # (search: current_user.is_restricted -> 403), so this only opens reads. The
    # in-game client and third-party OAuth apps stay fully blocked; the client uses
    # that 403 to pop the ToriiBriefingGlass restriction panel. The token is kept
    # (never deleted) either way so /api/v2/torii/restriction stays reachable.
    if token_record.client_id != settings.osu_web_client_id and await user.is_restricted(db):
        raise HTTPException(status_code=403, detail=RESTRICTED_ACCESS_DETAIL)

    return user, token_record


UserAndToken = tuple[User, OAuthToken]


async def get_client_user_no_verified(user_and_token: UserAndToken = Depends(get_client_user_and_token)):
    return user_and_token[0]


async def get_client_user(
    db: Database,
    redis: Annotated[Redis, Depends(get_redis)],
    api_version: APIVersion,
    user_and_token: UserAndToken = Depends(get_client_user_and_token),
):
    from app.service.verification_service import LoginSessionService

    user, token = user_and_token

    if await LoginSessionService.check_is_need_verification(db, user.id, token.id):
        # Get current verification method
        verify_method = None
        if api_version >= SUPPORT_TOTP_VERIFICATION_VER:
            verify_method = await LoginSessionService.get_login_method(user.id, token.id, redis)

        if verify_method is None:
            # Intelligently select verification method (referencing osu-web State.php:36)
            totp_key = await user.awaitable_attrs.totp_key
            verify_method = "totp" if totp_key is not None and api_version >= SUPPORT_TOTP_VERIFICATION_VER else "mail"

            # Store selected verification method in Redis to avoid re-selection
            if api_version >= SUPPORT_TOTP_VERIFICATION_VER:
                await LoginSessionService.set_login_method(user.id, token.id, verify_method, redis)

        # Return error response matching osu! API standard
        error_response = {"error": "User not verified", "method": verify_method}
        raise HTTPException(status_code=401, detail=error_response)
    return user


async def _validate_token(
    db: Database,
    token: str,
    security_scopes: SecurityScopes,
) -> UserAndToken:
    token_record = await get_token_by_access_token(db, token)
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    is_client = token_record.client_id in (
        settings.osu_client_id,
        settings.osu_web_client_id,
    )

    if not is_client:
        for scope in security_scopes.scopes:
            if scope not in token_record.scope.split(","):
                raise HTTPException(status_code=403, detail=f"Insufficient scope: {scope}")

    user = (await db.exec(select(User).where(User.id == token_record.user_id))).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # The website (web client) keeps READ access while restricted, so a restricted
    # player can still log in and browse their own profile read-only and see the
    # restriction banner. Every WRITE endpoint blocks restricted users on its own
    # (search: current_user.is_restricted -> 403), so this only opens reads. The
    # in-game client and third-party OAuth apps stay fully blocked; the client uses
    # that 403 to pop the ToriiBriefingGlass restriction panel. The token is kept
    # (never deleted) either way so /api/v2/torii/restriction stays reachable.
    if token_record.client_id != settings.osu_web_client_id and await user.is_restricted(db):
        raise HTTPException(status_code=403, detail=RESTRICTED_ACCESS_DETAIL)
    return user, token_record


async def get_current_user_and_token(
    db: Database,
    security_scopes: SecurityScopes,
    token_pw: Annotated[str | None, Depends(oauth2_password)] = None,
    token_code: Annotated[str | None, Depends(oauth2_code)] = None,
    token_client_credentials: Annotated[str | None, Depends(oauth2_client_credentials)] = None,
) -> UserAndToken:
    """Get currently authenticated user"""
    token = token_pw or token_code or token_client_credentials
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return await _validate_token(db, token, security_scopes)


async def get_current_user(
    user_and_token: UserAndToken = Depends(get_current_user_and_token),
) -> User:
    return user_and_token[0]


async def get_optional_user(
    db: Database,
    security_scopes: SecurityScopes,
    request: Request,
) -> User | None:
    # Be tolerant for optional auth endpoints:
    # clients may send non-bearer or stale auth headers while still expecting public data.
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None

    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() != "bearer" or not token:
        return None

    try:
        return (await _validate_token(db, token, security_scopes))[0]
    except HTTPException:
        return None


ClientUser = Annotated[User, Security(get_client_user, scopes=["*"])]

from datetime import timedelta
import hashlib
import re
import secrets
import string

from app.config import settings
from app.const import BACKUP_CODE_LENGTH
from app.database import (
    OAuthToken,
    User,
)
from app.database.auth import TotpKeys
from app.log import log
from app.models.totp import FinishStatus, StartCreateTotpKeyResp
from app.utils import utcnow

import bcrypt
from jose import JWTError, jwt
from passlib.context import CryptContext
import pyotp
from redis.asyncio import Redis
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# bcrypt cache (simulates application state cache)
bcrypt_cache = {}

logger = log("Auth")


def validate_username(username: str) -> list[str]:
    """Validate username."""
    errors = []

    if not username:
        errors.append("Username is required")
        return errors

    if len(username) < 3:
        errors.append("Username must be at least 3 characters long")

    if len(username) > 15:
        errors.append("Username must be at most 15 characters long")

    # Username format: letters, numbers, underscores, hyphens, and spaces.
    if not re.match(r"^[a-zA-Z0-9_ -]+$", username):
        errors.append("Username can only contain letters, numbers, spaces, underscores, and hyphens")

    # A single space between words is allowed, but not a leading/trailing space
    # or doubled spaces (so "first last" works; " name", "name " and
    # "first  last" do not).
    if username != username.strip():
        errors.append("Username cannot start or end with a space")
    elif "  " in username:
        errors.append("Username cannot contain consecutive spaces")

    if username.lower() in settings.banned_name:
        errors.append("This username is not allowed")

    return errors


def validate_password(password: str) -> list[str]:
    """Validate password."""
    errors = []

    if not password:
        errors.append("Password is required")
        return errors

    if len(password) < 8:
        errors.append("Password must be at least 8 characters long")

    return errors


def verify_password_legacy(plain_password: str, bcrypt_hash: str) -> bool:
    """
    Verify password - using osu!'s verification flow:
    1. Plaintext password -> MD5 hash
    2. MD5 hash -> bcrypt verification
    """
    # 1. Plaintext password to MD5
    pw_md5 = hashlib.md5(plain_password.encode()).hexdigest().encode()  # noqa: S324

    # 2. Check cache
    if bcrypt_hash in bcrypt_cache:
        return bcrypt_cache[bcrypt_hash] == pw_md5

    # 3. If not in cache, verify with bcrypt
    try:
        # Verify MD5 hash against bcrypt hash
        is_valid = bcrypt.checkpw(pw_md5, bcrypt_hash.encode())

        # If verification succeeds, cache the result
        if is_valid:
            bcrypt_cache[bcrypt_hash] = pw_md5

        return is_valid
    except Exception:
        logger.exception("Password verification error")
        return False


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password (backward compatible)."""
    # First try legacy osu! verification flow
    if verify_password_legacy(plain_password, hashed_password):
        return True

    # If failed, try standard bcrypt verification
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Generate password hash - using osu!'s method."""
    # 1. Plaintext password -> MD5
    pw_md5 = hashlib.md5(password.encode()).hexdigest().encode()  # noqa: S324
    # 2. MD5 -> bcrypt
    pw_bcrypt = bcrypt.hashpw(pw_md5, bcrypt.gensalt())
    return pw_bcrypt.decode()


async def authenticate_user_legacy(db: AsyncSession, name: str, password: str) -> User | None:
    """
    Authenticate user identity - using logic similar to from_login.
    """
    # 1. Plaintext password to MD5
    pw_md5 = hashlib.md5(password.encode()).hexdigest()  # noqa: S324

    # 2. Find user by username/email/id
    user = None
    user = (await db.exec(select(User).where(User.username == name))).first()
    if user is None:
        user = (await db.exec(select(User).where(User.email == name))).first()
    if user is None and name.isdigit():
        user = (await db.exec(select(User).where(User.id == int(name)))).first()
    if user is None:
        return None

    # 3. Verify password
    if user.pw_bcrypt is None or user.pw_bcrypt == "":
        return None

    # 4. Check cache
    if user.pw_bcrypt in bcrypt_cache:
        if bcrypt_cache[user.pw_bcrypt] == pw_md5.encode():
            return user
        else:
            return None

    # 5. Verify bcrypt
    try:
        is_valid = bcrypt.checkpw(pw_md5.encode(), user.pw_bcrypt.encode())
        if is_valid:
            # Cache verification result
            bcrypt_cache[user.pw_bcrypt] = pw_md5.encode()
            return user
    except Exception:
        logger.exception(f"Authentication error for user {name}")

    return None


async def authenticate_user(db: AsyncSession, username: str, password: str) -> User | None:
    """Authenticate user identity."""
    return await authenticate_user_legacy(db, username, password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = utcnow() + expires_delta
    else:
        expire = utcnow() + timedelta(minutes=settings.access_token_expire_minutes)

    # Add standard JWT claims
    to_encode.update({"exp": expire, "jti": secrets.token_hex(16)})
    if settings.jwt_audience:
        to_encode["aud"] = settings.jwt_audience
    to_encode["iss"] = str(settings.server_url)

    # Encode JWT
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)
    return encoded_jwt


def generate_refresh_token() -> str:
    """Generate refresh token."""
    length = 64
    characters = string.ascii_letters + string.digits
    return "".join(secrets.choice(characters) for _ in range(length))


async def invalidate_user_tokens(db: AsyncSession, user_id: int) -> int:
    """Invalidate all tokens for the specified user.

    Returns the number of deleted tokens.
    """
    # Use select to get all tokens first
    stmt = select(OAuthToken).where(OAuthToken.user_id == user_id)
    result = await db.exec(stmt)
    tokens = result.all()

    # Delete tokens one by one
    count = 0
    for token in tokens:
        await db.delete(token)
        count += 1

    # Commit changes
    await db.commit()
    return count


def verify_token(token: str) -> dict | None:
    """Verify access token."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return payload
    except JWTError:
        return None


async def store_token(
    db: AsyncSession,
    user_id: int,
    client_id: int,
    scopes: list[str],
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_token_expires_in: int,
    allow_multiple_devices: bool = True,
) -> OAuthToken:
    """Store token into database (supports multiple devices)."""
    expires_at = utcnow() + timedelta(seconds=expires_in)
    refresh_token_expires_at = utcnow() + timedelta(seconds=refresh_token_expires_in)

    if not allow_multiple_devices:
        # Legacy behavior: delete old tokens of user (single device mode)
        statement = select(OAuthToken).where(OAuthToken.user_id == user_id, OAuthToken.client_id == client_id)
        old_tokens = (await db.exec(statement)).all()
        for token in old_tokens:
            await db.delete(token)
    else:
        # New behavior: only delete expired tokens, keep valid tokens (multi-device mode)
        statement = select(OAuthToken).where(
            OAuthToken.user_id == user_id, OAuthToken.client_id == client_id, OAuthToken.expires_at <= utcnow()
        )
        expired_tokens = (await db.exec(statement)).all()
        for token in expired_tokens:
            await db.delete(token)

        # Limit maximum tokens per user per client (prevent unbounded growth)
        max_tokens_per_client = settings.max_tokens_per_client
        statement = (
            select(OAuthToken)
            .where(OAuthToken.user_id == user_id, OAuthToken.client_id == client_id, OAuthToken.expires_at > utcnow())
            .order_by(col(OAuthToken.created_at).desc())
        )

        active_tokens = (await db.exec(statement)).all()
        if len(active_tokens) >= max_tokens_per_client:
            # Delete oldest tokens
            tokens_to_delete = active_tokens[max_tokens_per_client - 1 :]
            for token in tokens_to_delete:
                await db.delete(token)
            logger.info(f"Cleaned up {len(tokens_to_delete)} old tokens for user {user_id}")

    # Check if there is a duplicate access_token
    duplicate_token = (await db.exec(select(OAuthToken).where(OAuthToken.access_token == access_token))).first()
    if duplicate_token:
        await db.delete(duplicate_token)

    # Create new token record
    token_record = OAuthToken(
        user_id=user_id,
        client_id=client_id,
        access_token=access_token,
        scope=",".join(scopes),
        refresh_token=refresh_token,
        expires_at=expires_at,
        refresh_token_expires_at=refresh_token_expires_at,
    )
    db.add(token_record)
    await db.commit()
    await db.refresh(token_record)

    logger.info(f"Created new token for user {user_id}, client {client_id} (multi-device: {allow_multiple_devices})")
    return token_record


async def get_token_by_access_token(db: AsyncSession, access_token: str) -> OAuthToken | None:
    """Get token record by access token."""
    statement = select(OAuthToken).where(
        OAuthToken.access_token == access_token,
        OAuthToken.expires_at > utcnow(),
    )
    return (await db.exec(statement)).first()


async def get_token_by_refresh_token(db: AsyncSession, refresh_token: str) -> OAuthToken | None:
    """Get token record by refresh token."""
    statement = select(OAuthToken).where(
        OAuthToken.refresh_token == refresh_token,
        OAuthToken.refresh_token_expires_at > utcnow(),
    )
    return (await db.exec(statement)).first()


async def get_user_by_authorization_code(
    db: AsyncSession, redis: Redis, client_id: int, code: str
) -> tuple[User, list[str]] | None:
    user_id = await redis.hget(f"oauth:code:{client_id}:{code}", "user_id")  # pyright: ignore[reportGeneralTypeIssues]
    scopes = await redis.hget(f"oauth:code:{client_id}:{code}", "scopes")  # pyright: ignore[reportGeneralTypeIssues]
    if not user_id or not scopes:
        return None

    await redis.hdel(f"oauth:code:{client_id}:{code}", "user_id", "scopes")  # pyright: ignore[reportGeneralTypeIssues]

    statement = select(User).where(User.id == int(user_id))
    user = (await db.exec(statement)).first()
    if user:
        await db.refresh(user)
        return (user, scopes.split(","))
    return None


def totp_redis_key(user: User) -> str:
    return f"totp:setup:{user.email}"


def _generate_totp_account_label(user: User) -> str:
    """Generate TOTP account label.

    Select username or email based on config, and add server info to make label more descriptive.
    """
    primary_identifier = user.username if settings.totp_use_username_in_label else user.email

    # If service name is configured, append to label for distinction in authenticator app
    if settings.totp_service_name:
        return f"{primary_identifier} ({settings.totp_service_name})"
    else:
        return primary_identifier


def _generate_totp_issuer_name() -> str:
    """Generate TOTP issuer name.

    Prioritize custom totp_issuer, otherwise use service name.
    """
    if settings.totp_issuer:
        return settings.totp_issuer
    elif settings.totp_service_name:
        return settings.totp_service_name
    else:
        # Fallback to default value
        return "osu! Private Server"


async def start_create_totp_key(user: User, redis: Redis) -> StartCreateTotpKeyResp:
    secret = pyotp.random_base32()
    await redis.hset(totp_redis_key(user), mapping={"secret": secret, "fails": 0})  # pyright: ignore[reportGeneralTypeIssues]
    await redis.expire(totp_redis_key(user), 300)

    # Generate more descriptive account label and issuer info
    account_label = _generate_totp_account_label(user)
    issuer_name = _generate_totp_issuer_name()

    return StartCreateTotpKeyResp(
        secret=secret,
        uri=pyotp.totp.TOTP(secret).provisioning_uri(name=account_label, issuer_name=issuer_name),
    )


def verify_totp_key(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)


async def verify_totp_key_with_replay_protection(user_id: int, secret: str, code: str, redis: Redis) -> bool:
    """Verify TOTP token with replay attack protection."""
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return False

    # Prevent reusing the same token within 120 seconds (referenced from osu-web)
    cache_key = f"totp:{user_id}:{code}"
    if await redis.exists(cache_key):
        return False

    # Set 120-second expiration
    await redis.setex(cache_key, 120, "1")
    return True


def _generate_backup_codes(count=10, length=BACKUP_CODE_LENGTH) -> list[str]:
    alphabet = string.ascii_uppercase + string.digits
    return ["".join(secrets.choice(alphabet) for _ in range(length)) for _ in range(count)]


async def _store_totp_key(user: User, secret: str, db: AsyncSession) -> list[str]:
    backup_codes = _generate_backup_codes()
    hashed_codes = [bcrypt.hashpw(code.encode(), bcrypt.gensalt()) for code in backup_codes]
    totp_secret = TotpKeys(user_id=user.id, secret=secret, backup_keys=[code.decode() for code in hashed_codes])
    db.add(totp_secret)
    await db.commit()
    return backup_codes


async def finish_create_totp_key(
    user: User, code: str, redis: Redis, db: AsyncSession
) -> tuple[FinishStatus, list[str]]:
    data = await redis.hgetall(totp_redis_key(user))  # pyright: ignore[reportGeneralTypeIssues]
    if not data or "secret" not in data or "fails" not in data:
        return FinishStatus.INVALID, []

    secret = data["secret"]
    fails = int(data["fails"])

    if fails >= 3:
        await redis.delete(totp_redis_key(user))  # pyright: ignore[reportGeneralTypeIssues]
        return FinishStatus.TOO_MANY_ATTEMPTS, []

    if verify_totp_key(secret, code):
        await redis.delete(totp_redis_key(user))  # pyright: ignore[reportGeneralTypeIssues]
        backup_codes = await _store_totp_key(user, secret, db)
        return FinishStatus.SUCCESS, backup_codes
    else:
        fails += 1
        await redis.hset(totp_redis_key(user), "fails", str(fails))  # pyright: ignore[reportGeneralTypeIssues]
        return FinishStatus.FAILED, []


async def disable_totp(user: User, db: AsyncSession) -> None:
    totp = await db.get(TotpKeys, user.id)
    if totp:
        await db.delete(totp)
        await db.commit()


def check_totp_backup_code(totp: TotpKeys, code: str) -> bool:
    for hashed_code in totp.backup_keys:
        if bcrypt.checkpw(code.encode(), hashed_code.encode()):
            copy = totp.backup_keys[:]
            copy.remove(hashed_code)
            totp.backup_keys = copy
            return True
    return False

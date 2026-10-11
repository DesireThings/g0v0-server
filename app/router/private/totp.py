from typing import Annotated

from app.auth import (
    check_totp_backup_code,
    finish_create_totp_key,
    start_create_totp_key,
    totp_redis_key,
    verify_totp_key_with_replay_protection,
)
from app.const import BACKUP_CODE_LENGTH
from app.database.auth import TotpKeys
from app.dependencies.database import Database, Redis
from app.dependencies.user import ClientUser
from app.models.totp import FinishStatus, StartCreateTotpKeyResp

from .router import router

from fastapi import Body, HTTPException
from pydantic import BaseModel
import pyotp


class TotpStatusResp(BaseModel):
    """TOTP status response"""

    enabled: bool
    created_at: str | None = None


@router.get(
    "/totp/status",
    name="Check TOTP Status",
    description="Check whether the current user has enabled TOTP two-factor authentication",
    tags=["Authentication", "g0v0 API"],
    response_model=TotpStatusResp,
)
async def get_totp_status(
    current_user: ClientUser,
):
    """Check if user has configured TOTP"""
    totp_key = await current_user.awaitable_attrs.totp_key

    if totp_key:
        return TotpStatusResp(enabled=True, created_at=totp_key.created_at.isoformat())
    else:
        return TotpStatusResp(enabled=False)


@router.post(
    "/totp/create",
    name="Start TOTP Creation Flow",
    description=(
        "Start the TOTP creation flow\n\n"
        "Returns the TOTP secret and URI for the user to add the account in their authenticator app.\n\n"
        "Then make a PUT `/api/private/totp/create` request with the code from the authenticator app to complete setup.\n\n"
        "If not completed within 5 minutes or after 3 failed attempts, the process must be restarted."
    ),
    tags=["Authentication", "g0v0 API"],
    response_model=StartCreateTotpKeyResp,
    status_code=201,
)
async def start_create_totp(
    redis: Redis,
    current_user: ClientUser,
):
    if await current_user.awaitable_attrs.totp_key:
        raise HTTPException(status_code=400, detail="TOTP is already enabled for this user")

    previous = await redis.hgetall(totp_redis_key(current_user))  # pyright: ignore[reportGeneralTypeIssues]
    if previous:  # pyright: ignore[reportGeneralTypeIssues]
        from app.auth import _generate_totp_account_label, _generate_totp_issuer_name

        account_label = _generate_totp_account_label(current_user)
        issuer_name = _generate_totp_issuer_name()

        return StartCreateTotpKeyResp(
            secret=previous["secret"],
            uri=pyotp.totp.TOTP(previous["secret"]).provisioning_uri(
                name=account_label,
                issuer_name=issuer_name,
            ),
        )
    return await start_create_totp_key(current_user, redis)


@router.put(
    "/totp/create",
    name="Complete TOTP Creation Flow",
    description=(
        "Complete TOTP creation flow by verifying the provided TOTP code.\n\n"
        "- If verification succeeds, enables TOTP 2FA for user and returns backup codes.\n- If verification fails, returns error."
    ),
    tags=["Authentication", "g0v0 API"],
    response_model=list[str],
    status_code=201,
)
async def finish_create_totp(
    session: Database,
    code: Annotated[str, Body(..., embed=True, description="TOTP code provided by the user")],
    redis: Redis,
    current_user: ClientUser,
):
    status, backup_codes = await finish_create_totp_key(current_user, code, redis, session)
    if status == FinishStatus.SUCCESS:
        return backup_codes
    elif status == FinishStatus.INVALID:
        raise HTTPException(status_code=400, detail="No TOTP setup in progress or invalid data")
    elif status == FinishStatus.TOO_MANY_ATTEMPTS:
        raise HTTPException(status_code=400, detail="Too many failed attempts. Please start over.")
    else:
        raise HTTPException(status_code=400, detail="Invalid TOTP code")


@router.delete(
    "/totp",
    name="Disable TOTP Two-Factor Authentication",
    description="Disable TOTP two-factor authentication for current user",
    tags=["Authentication", "g0v0 API"],
    status_code=204,
)
async def disable_totp(
    session: Database,
    code: Annotated[str, Body(..., embed=True, description="TOTP code or backup code provided by user")],
    redis: Redis,
    current_user: ClientUser,
):
    totp = await session.get(TotpKeys, current_user.id)
    if not totp:
        raise HTTPException(status_code=400, detail="TOTP is not enabled for this user")

    # Verify with replay-protected TOTP or backup code
    is_totp_valid = False
    if len(code) == 6 and code.isdigit():
        is_totp_valid = await verify_totp_key_with_replay_protection(current_user.id, totp.secret, code, redis)
    elif len(code) == BACKUP_CODE_LENGTH:
        is_totp_valid = check_totp_backup_code(totp, code)

    if is_totp_valid:
        await session.delete(totp)
        await session.commit()
    else:
        raise HTTPException(status_code=400, detail="Invalid TOTP code or backup code")

"""
User login log service.
"""

import asyncio
from datetime import timedelta

from app.database.user_login_log import UserLoginLog
from app.dependencies.geoip import get_client_ip, get_geoip_helper, normalize_ip
from app.log import logger
from app.utils import utcnow

from fastapi import Request
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


class LoginLogService:
    """User login log service."""

    @staticmethod
    async def record_login(
        db: AsyncSession,
        user_id: int,
        request: Request,
        user_agent: str | None = None,
        client_hash: str | None = None,
        client_label: str | None = None,
        login_success: bool = True,
        login_method: str = "password",
        notes: str | None = None,
    ) -> UserLoginLog:
        """
        Record user login information.

        Args:
            db: Database session.
            user_id: User ID.
            request: HTTP request object.
            login_success: Whether login succeeded.
            login_method: Login method.
            notes: Note information.

        Returns:
            UserLoginLog: Login log object.
        """
        # Get client IP and normalize format
        raw_ip = get_client_ip(request)
        ip_address = normalize_ip(raw_ip)

        # Prefer explicit user_agent, fallback to request header, and keep
        # it within the database max length.
        resolved_user_agent = (user_agent or request.headers.get("User-Agent") or "").strip()
        if resolved_user_agent:
            resolved_user_agent = resolved_user_agent[:500]
        else:
            resolved_user_agent = None

        # Create base login log record
        resolved_client_hash = (client_hash or "").strip().lower()
        if resolved_client_hash:
            resolved_client_hash = resolved_client_hash[:128]
        else:
            resolved_client_hash = None

        resolved_client_label = (client_label or "").strip()
        if resolved_client_label:
            resolved_client_label = resolved_client_label[:255]
        else:
            resolved_client_label = None

        login_log = UserLoginLog(
            user_id=user_id,
            ip_address=ip_address,
            user_agent=resolved_user_agent,
            client_hash=resolved_client_hash,
            client_label=resolved_client_label,
            login_time=utcnow(),
            login_success=login_success,
            login_method=login_method,
            notes=notes,
        )

        # Asynchronously fetch GeoIP information
        try:
            geoip = get_geoip_helper()

            # Run GeoIP lookup in background thread (avoid blocking)
            loop = asyncio.get_event_loop()
            geo_info = await loop.run_in_executor(None, lambda: geoip.lookup(ip_address))

            if geo_info:
                login_log.country_code = geo_info.get("country_iso", "")
                login_log.country_name = geo_info.get("country_name", "")
                login_log.city_name = geo_info.get("city_name", "")
                login_log.latitude = geo_info.get("latitude", "")
                login_log.longitude = geo_info.get("longitude", "")
                login_log.time_zone = geo_info.get("time_zone", "")

                # Process ASN (may be string, convert to integer)
                asn_value = geo_info.get("asn")
                if asn_value is not None:
                    try:
                        login_log.asn = int(asn_value)
                    except (ValueError, TypeError):
                        login_log.asn = None

                login_log.organization = geo_info.get("organization", "")

                logger.debug(f"GeoIP lookup for {ip_address}: {geo_info.get('country_name', 'Unknown')}")
            else:
                logger.warning(f"GeoIP lookup failed for {ip_address}")

        except Exception as e:
            logger.warning(f"GeoIP lookup error for {ip_address}: {e}")

        # Save to database
        db.add(login_log)
        await db.commit()
        await db.refresh(login_log)

        logger.info(f"Login recorded for user {user_id} from {ip_address} ({login_method})")
        return login_log

    @staticmethod
    async def record_failed_login(
        db: AsyncSession,
        request: Request,
        attempted_username: str | None = None,
        login_method: str = "password",
        notes: str | None = None,
        user_agent: str | None = None,
        client_hash: str | None = None,
        client_label: str | None = None,
    ) -> UserLoginLog:
        """
        Record failed login attempt.

        Args:
            db: Database session.
            request: HTTP request object.
            attempted_username: Attempted username.
            login_method: Login method.
            notes: Note information.

        Returns:
            UserLoginLog: Login log object.
        """
        # For failed logins, use user_id=0 to represent unknown user
        return await LoginLogService.record_login(
            db=db,
            user_id=0,  # 0 indicates unknown/failed login
            request=request,
            login_success=False,
            login_method=login_method,
            user_agent=user_agent,
            client_hash=client_hash,
            client_label=client_label,
            notes=(
                f"Failed login attempt on user {attempted_username}: {notes}"
                if attempted_username
                else "Failed login attempt"
            ),
        )


    @staticmethod
    async def attach_client_identity_to_recent_login(
        db: AsyncSession,
        user_id: int,
        request: Request,
        client_hash: str | None = None,
        client_label: str | None = None,
        lookback_hours: int = 24,
    ) -> bool:
        """
        Backfill hash/label into a recent successful login row for this user.

        This is used when OAuth login does not send version_hash but later
        gameplay requests (e.g. score token creation) do include it.
        """
        normalized_hash = (client_hash or "").strip().lower()
        if normalized_hash:
            normalized_hash = normalized_hash[:128]
        else:
            normalized_hash = None

        normalized_label = (client_label or "").strip()
        if normalized_label:
            normalized_label = normalized_label[:255]
        else:
            normalized_label = None

        if not normalized_hash and not normalized_label:
            return False

        current_ip = normalize_ip(get_client_ip(request))
        since_time = utcnow() - timedelta(hours=max(1, lookback_hours))

        rows = (
            await db.exec(
                select(UserLoginLog)
                .where(
                    UserLoginLog.user_id == user_id,
                    UserLoginLog.login_success.is_(True),
                    col(UserLoginLog.login_time) >= since_time,
                )
                .order_by(col(UserLoginLog.login_time).desc())
                .limit(30)
            )
        ).all()
        if not rows:
            return False

        target: UserLoginLog | None = None

        # 1) best effort: same IP + missing hash
        for row in rows:
            if row.ip_address == current_ip and not (row.client_hash or "").strip():
                target = row
                break

        # 2) same IP + matching hash (refresh label only)
        if target is None and normalized_hash:
            for row in rows:
                if row.ip_address == current_ip and (row.client_hash or "").strip().lower() == normalized_hash:
                    target = row
                    break

        # 3) fallback: most recent row with missing hash
        if target is None:
            for row in rows:
                if not (row.client_hash or "").strip():
                    target = row
                    break

        if target is None:
            return False

        changed = False
        if normalized_hash and not (target.client_hash or "").strip():
            target.client_hash = normalized_hash
            changed = True

        generic_labels = {"unknown", "-", "osu!", "osu!lazer", "lazer", "osulazer"}
        if normalized_label:
            existing = (target.client_label or "").strip().lower()
            if not existing or existing in generic_labels:
                target.client_label = normalized_label
                changed = True

        if changed:
            db.add(target)
            await db.commit()
            logger.debug(
                "Backfilled login client identity for user %s (log_id=%s, hash=%s, label=%s)",
                user_id,
                target.id,
                target.client_hash,
                target.client_label,
            )

        return changed

    @staticmethod
    async def record_session_resume_if_due(
        db: AsyncSession,
        user_id: int,
        request: Request,
        user_agent: str | None = None,
        client_hash: str | None = None,
        client_label: str | None = None,
        lookback_minutes: int = 20,
    ) -> bool:
        """
        Record a lightweight 'session_resume' event when a client reconnects
        with an already-verified session.

        To avoid log spam, skip if a similar successful event exists recently
        for the same user/ip and (when available) same hash.
        """
        now = utcnow()
        since_time = now - timedelta(minutes=max(1, lookback_minutes))
        current_ip = normalize_ip(get_client_ip(request))
        normalized_hash = (client_hash or "").strip().lower() or None
        if normalized_hash:
            normalized_hash = normalized_hash[:128]

        recent_rows = (
            await db.exec(
                select(UserLoginLog)
                .where(
                    UserLoginLog.user_id == user_id,
                    UserLoginLog.login_success.is_(True),
                    UserLoginLog.ip_address == current_ip,
                    col(UserLoginLog.login_time) >= since_time,
                )
                .order_by(col(UserLoginLog.login_time).desc())
                .limit(20)
            )
        ).all()

        for row in recent_rows:
            row_hash = (row.client_hash or "").strip().lower() or None
            same_hash = (not normalized_hash) or (row_hash == normalized_hash)
            if same_hash and row.login_method in {
                "session_resume",
                "password",
                "password_pending_verification",
                "totp",
                "mail",
                "totp_backup_code",
            }:
                return False

        await LoginLogService.record_login(
            db=db,
            user_id=user_id,
            request=request,
            user_agent=user_agent,
            client_hash=normalized_hash,
            client_label=client_label,
            login_success=True,
            login_method="session_resume",
            notes="Session resumed with existing token",
        )
        return True


def get_request_info(request: Request) -> dict:
    """
    Extract request details.

    Args:
        request: HTTP request object.

    Returns:
        dict: Dictionary containing request information.
    """
    return {
        "ip": get_client_ip(request),
        "user_agent": request.headers.get("User-Agent", ""),
        "referer": request.headers.get("Referer", ""),
        "accept_language": request.headers.get("Accept-Language", ""),
        "x_forwarded_for": request.headers.get("X-Forwarded-For", ""),
        "x_real_ip": request.headers.get("X-Real-IP", ""),
    }

"""
Database cleanup service - cleans up expired verification codes and sessions.
"""

from datetime import timedelta

from app.database.auth import OAuthToken
from app.database.verification import EmailVerification, LoginSession, TrustedDevice
from app.log import logger
from app.utils import utcnow

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession


class DatabaseCleanupService:
    """Database cleanup service."""

    @staticmethod
    async def cleanup_expired_verification_codes(db: AsyncSession) -> int:
        """
        Clean up expired email verification codes.

        Args:
            db: Database session.

        Returns:
            int: Number of records cleaned.
        """
        try:
            # Find expired verification code records
            current_time = utcnow()

            stmt = select(EmailVerification).where(EmailVerification.expires_at < current_time)
            result = await db.exec(stmt)
            expired_codes = result.all()

            # Delete expired records
            deleted_count = 0
            for code in expired_codes:
                await db.delete(code)
                deleted_count += 1

            await db.commit()

            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} expired email verification codes")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning expired verification codes: {e!s}")
            return 0

    @staticmethod
    async def cleanup_expired_login_sessions(db: AsyncSession) -> int:
        """
        Clean up expired login sessions.

        Args:
            db: Database session.

        Returns:
            int: Number of records cleaned.
        """
        try:
            # Find expired login session records
            current_time = utcnow()

            stmt = select(LoginSession).where(
                LoginSession.expires_at < current_time, col(LoginSession.is_verified).is_(False)
            )
            result = await db.exec(stmt)
            expired_sessions = result.all()

            # Delete expired records
            deleted_count = 0
            for session in expired_sessions:
                await db.delete(session)
                deleted_count += 1

            await db.commit()

            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} expired login sessions")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning expired login sessions: {e!s}")
            return 0

    @staticmethod
    async def cleanup_old_used_verification_codes(db: AsyncSession, days_old: int = 7) -> int:
        """
        Clean up old used verification code records.

        Args:
            db: Database session.
            days_old: Clean records used more than this many days ago, default 7 days.

        Returns:
            int: Number of records cleaned.
        """
        try:
            # Find used verification code records from before specified days
            cutoff_time = utcnow() - timedelta(days=days_old)

            stmt = select(EmailVerification).where(col(EmailVerification.is_used).is_(True))
            result = await db.exec(stmt)
            all_used_codes = result.all()

            # Filter out expired records
            old_used_codes = [code for code in all_used_codes if code.used_at and code.used_at < cutoff_time]

            # Delete old used records
            deleted_count = 0
            for code in old_used_codes:
                await db.delete(code)
                deleted_count += 1

            await db.commit()

            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} used verification codes older than {days_old} days")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning old used verification codes: {e!s}")
            return 0

    @staticmethod
    async def cleanup_unverified_login_sessions(db: AsyncSession, hours_old: int = 1) -> int:
        """
        Clean up login sessions created before specified hours that are still unverified.

        Args:
            db: Database session.
            hours_old: Clean sessions created before this many hours that are still unverified, default 1 hour.

        Returns:
            int: Number of records cleaned.
        """
        try:
            # Calculate cutoff time
            cutoff_time = utcnow() - timedelta(hours=hours_old)

            # Find session records created before cutoff time and still unverified
            stmt = select(LoginSession).where(
                col(LoginSession.is_verified).is_(False), LoginSession.created_at < cutoff_time
            )
            result = await db.exec(stmt)
            unverified_sessions = result.all()

            # Delete unverified session records
            deleted_count = 0
            for session in unverified_sessions:
                await db.delete(session)
                deleted_count += 1

            await db.commit()

            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} unverified login sessions older than {hours_old} hour(s)")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning unverified login sessions: {e!s}")
            return 0

    @staticmethod
    async def cleanup_outdated_verified_sessions(db: AsyncSession) -> int:
        """
        Clean up outdated session records.

        Args:
            db: Database session.

        Returns:
            int: Number of records cleaned.
        """
        try:
            stmt = select(LoginSession).where(
                col(LoginSession.is_verified).is_(True), col(LoginSession.token_id).is_(None)
            )
            result = await db.exec(stmt)
            # Delete old verified records
            deleted_count = 0
            for session in result.all():
                await db.delete(session)
                deleted_count += 1

            await db.commit()

            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} outdated verified sessions")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning outdated verified sessions: {e!s}")
            return 0

    @staticmethod
    async def cleanup_outdated_trusted_devices(db: AsyncSession) -> int:
        """
        Clean up expired trusted device records.

        Args:
            db: Database session.

        Returns:
            int: Number of records cleaned.
        """
        try:
            # Find expired trusted device records
            current_time = utcnow()

            stmt = select(TrustedDevice).where(TrustedDevice.expires_at < current_time)
            result = await db.exec(stmt)
            expired_devices = result.all()

            # Delete expired records
            deleted_count = 0
            for device in expired_devices:
                await db.delete(device)
                deleted_count += 1

            await db.commit()

            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} expired trusted devices")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning expired trusted devices: {e!s}")
            return 0

    @staticmethod
    async def cleanup_outdated_tokens(db: AsyncSession) -> int:
        """
        Clean up expired OAuth tokens.

        Args:
            db: Database session.

        Returns:
            int: Number of records cleaned.
        """
        try:
            current_time = utcnow()

            stmt = select(OAuthToken).where(OAuthToken.refresh_token_expires_at < current_time)
            result = await db.exec(stmt)
            expired_tokens = result.all()

            deleted_count = 0
            for token in expired_tokens:
                await db.delete(token)
                deleted_count += 1

            await db.commit()

            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} expired OAuth tokens")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning expired OAuth tokens: {e!s}")
            return 0

    @staticmethod
    async def run_full_cleanup(db: AsyncSession) -> dict[str, int]:
        """
        Run complete cleanup process.

        Args:
            db: Database session.

        Returns:
            dict: Cleanup statistics for each item.
        """
        results = {}

        # Clean up expired verification codes
        results["expired_verification_codes"] = await DatabaseCleanupService.cleanup_expired_verification_codes(db)

        # Clean up expired login sessions
        results["expired_login_sessions"] = await DatabaseCleanupService.cleanup_expired_login_sessions(db)

        # Clean up unverified login sessions from 1 hour ago
        results["unverified_login_sessions"] = await DatabaseCleanupService.cleanup_unverified_login_sessions(db, 1)

        # Clean up used verification codes from 7 days ago
        results["old_used_verification_codes"] = await DatabaseCleanupService.cleanup_old_used_verification_codes(db, 7)

        # Clean up expired trusted devices
        results["outdated_trusted_devices"] = await DatabaseCleanupService.cleanup_outdated_trusted_devices(db)

        # Clean up expired OAuth tokens
        results["outdated_oauth_tokens"] = await DatabaseCleanupService.cleanup_outdated_tokens(db)

        # Clean up outdated (expired token) verified sessions
        results["outdated_verified_sessions"] = await DatabaseCleanupService.cleanup_outdated_verified_sessions(db)

        total_cleaned = sum(results.values())
        if total_cleaned > 0:
            logger.debug(f"Full cleanup completed, total cleaned: {total_cleaned} records - {results}")

        return results

    @staticmethod
    async def get_cleanup_statistics(db: AsyncSession) -> dict[str, int]:
        """
        Get cleanup statistics.

        Args:
            db: Database session.

        Returns:
            dict: Statistics information.
        """
        try:
            current_time = utcnow()
            cutoff_1_hour = current_time - timedelta(hours=1)
            cutoff_7_days = current_time - timedelta(days=7)
            cutoff_30_days = current_time - timedelta(days=30)

            # Count expired verification codes
            expired_codes_stmt = (
                select(func.count()).select_from(EmailVerification).where(EmailVerification.expires_at < current_time)
            )
            expired_codes_result = await db.exec(expired_codes_stmt)
            expired_codes_count = expired_codes_result.one()

            # Count expired login sessions
            expired_sessions_stmt = (
                select(func.count()).select_from(LoginSession).where(LoginSession.expires_at < current_time)
            )
            expired_sessions_result = await db.exec(expired_sessions_stmt)
            expired_sessions_count = expired_sessions_result.one()

            # Count unverified login sessions from 1 hour ago
            unverified_sessions_stmt = (
                select(func.count())
                .select_from(LoginSession)
                .where(col(LoginSession.is_verified).is_(False), LoginSession.created_at < cutoff_1_hour)
            )
            unverified_sessions_result = await db.exec(unverified_sessions_stmt)
            unverified_sessions_count = unverified_sessions_result.one()

            # Count used verification codes from 7 days ago
            old_used_codes_stmt = select(EmailVerification).where(col(EmailVerification.is_used).is_(True))
            old_used_codes_result = await db.exec(old_used_codes_stmt)
            all_used_codes = old_used_codes_result.all()
            old_used_codes_count = len(
                [code for code in all_used_codes if code.used_at and code.used_at < cutoff_7_days]
            )

            # Count verified sessions from 30 days ago
            outdated_verified_sessions_stmt = select(LoginSession).where(col(LoginSession.is_verified).is_(True))
            outdated_verified_sessions_result = await db.exec(outdated_verified_sessions_stmt)
            all_verified_sessions = outdated_verified_sessions_result.all()
            outdated_verified_sessions_count = len(
                [
                    session
                    for session in all_verified_sessions
                    if session.verified_at and session.verified_at < cutoff_30_days
                ]
            )

            # Count expired OAuth tokens
            outdated_tokens_stmt = (
                select(func.count()).select_from(OAuthToken).where(OAuthToken.refresh_token_expires_at < current_time)
            )
            outdated_tokens_result = await db.exec(outdated_tokens_stmt)
            outdated_tokens_count = outdated_tokens_result.one()

            # Count expired trusted devices
            outdated_devices_stmt = (
                select(func.count()).select_from(TrustedDevice).where(TrustedDevice.expires_at < current_time)
            )
            outdated_devices_result = await db.exec(outdated_devices_stmt)
            outdated_devices_count = outdated_devices_result.one()

            return {
                "expired_verification_codes": expired_codes_count,
                "expired_login_sessions": expired_sessions_count,
                "unverified_login_sessions": unverified_sessions_count,
                "old_used_verification_codes": old_used_codes_count,
                "outdated_verified_sessions": outdated_verified_sessions_count,
                "outdated_oauth_tokens": outdated_tokens_count,
                "outdated_trusted_devices": outdated_devices_count,
                "total_cleanable": expired_codes_count
                + expired_sessions_count
                + unverified_sessions_count
                + old_used_codes_count
                + outdated_verified_sessions_count
                + outdated_tokens_count
                + outdated_devices_count,
            }

        except Exception as e:
            logger.error(f"Error getting cleanup statistics: {e!s}")
            return {
                "expired_verification_codes": 0,
                "expired_login_sessions": 0,
                "unverified_login_sessions": 0,
                "old_used_verification_codes": 0,
                "outdated_verified_sessions": 0,
                "outdated_oauth_tokens": 0,
                "outdated_trusted_devices": 0,
                "total_cleanable": 0,
            }

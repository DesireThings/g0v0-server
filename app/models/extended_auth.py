"""
Extended OAuth response models with two-factor authentication support
"""

from pydantic import BaseModel


class ExtendedTokenResponse(BaseModel):
    """Extended token response supporting two-factor authentication state"""

    access_token: str | None = None
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int | None = None
    refresh_token: str | None = None
    scope: str | None = None

    # Two-factor authentication related fields
    requires_second_factor: bool = False
    verification_message: str | None = None
    user_id: int | None = None  # User ID for two-factor authentication

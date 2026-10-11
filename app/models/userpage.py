"""
Userpage editing related API models
"""

from pydantic import BaseModel, Field, field_validator


class UpdateUserpageRequest(BaseModel):
    """Update userpage request model (matches official osu-web format)"""

    body: str = Field(
        description="Raw BBCode content of the userpage",
        max_length=60000,
        examples=["[b]Hello![/b] This is my profile page.\n[color=blue]Blue text[/color]"],
    )

    @field_validator("body")
    @classmethod
    def validate_body_content(cls, v: str) -> str:
        """Validate raw content"""
        if not v.strip():
            return ""

        # Basic length validation
        if len(v) > 60000:
            msg = "Content too long. Maximum 60000 characters allowed."
            raise ValueError(msg)

        return v


class UpdateUserpageResponse(BaseModel):
    """Update userpage response model (matches official osu-web format)"""

    html: str = Field(description="Processed HTML content")


class UserpageResponse(BaseModel):
    """Userpage response model (contains html and raw, matches official format)"""

    html: str = Field(description="Processed HTML content")
    raw: str = Field(description="Raw BBCode content")


class ValidateBBCodeRequest(BaseModel):
    """Validate BBCode request model"""

    content: str = Field(description="BBCode content to validate", max_length=60000)


class ValidateBBCodeResponse(BaseModel):
    """Validate BBCode response model"""

    valid: bool = Field(description="Whether BBCode is valid")
    errors: list[str] = Field(default_factory=list, description="List of errors")
    preview: dict[str, str] = Field(description="Preview content")


class UserpageError(Exception):
    """Base class for userpage processing errors"""

    def __init__(self, message: str, code: str = "userpage_error"):
        self.message = message
        self.code = code
        super().__init__(message)


class ContentTooLongError(UserpageError):
    """Content too long error"""

    def __init__(self, current_length: int, max_length: int):
        message = f"Content too long. Maximum {max_length} characters allowed, got {current_length}."
        super().__init__(message, "content_too_long")
        self.current_length = current_length
        self.max_length = max_length


class ContentEmptyError(UserpageError):
    """Content empty error"""

    def __init__(self):
        super().__init__("Content cannot be empty.", "content_empty")


class BBCodeValidationError(UserpageError):
    """BBCode validation error"""

    def __init__(self, errors: list[str]):
        message = f"BBCode validation failed: {'; '.join(errors)}"
        super().__init__(message, "bbcode_validation_error")
        self.errors = errors


class ForbiddenTagError(UserpageError):
    """Forbidden tag error"""

    def __init__(self, tag: str):
        message = f"Forbidden tag '{tag}' is not allowed."
        super().__init__(message, "forbidden_tag")
        self.tag = tag


class MaliciousBBCodeError(UserpageError):
    """Malicious BBCode error"""

    def __init__(self, detail: str):
        message = f"Malicious BBCode detected: {detail}"
        super().__init__(message, "malicious_bbcode")
        self.detail = detail

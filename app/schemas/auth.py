"""Request/response schemas for authentication."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import EmploymentStatus, UserRole


class UserRegister(BaseModel):
    """Public registration payload.

    Deliberately has no ``role`` field, and forbids unknown keys, so a caller
    cannot smuggle one in. The service hard-codes the role regardless; this is
    the first of the two layers.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=160)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        # users.email carries a CHECK (email = lower(email)) constraint.
        return v.strip().lower()


class UserLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, v: str) -> str:
        return v.strip().lower()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the access token expires")


class UserPublic(BaseModel):
    """A user as returned by the API. Never includes the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    employee_code: str
    full_name: str
    role: UserRole
    employment_status: EmploymentStatus
    is_active: bool
    department_id: UUID | None = None


class MessageResponse(BaseModel):
    detail: str

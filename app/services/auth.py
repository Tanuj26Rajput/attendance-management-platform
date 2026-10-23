"""Authentication business logic.

Routers stay thin: they translate these outcomes into HTTP responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.enums import UserRole
from app.models.token import RevokedToken
from app.models.user import User
from app.schemas.auth import UserRegister


class AuthError(Exception):
    """Base class for authentication failures."""


class EmailAlreadyRegisteredError(AuthError):
    pass


class InvalidCredentialsError(AuthError):
    pass


class InactiveUserError(AuthError):
    pass


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(
        select(User).where(User.email == email.strip().lower())
    )
    return result.scalar_one_or_none()


async def register_user(session: AsyncSession, payload: UserRegister) -> User:
    """Create a new employee account.

    The role is hard-coded here and never read from the request. This is the
    second layer of the "registration cannot grant admin" rule -- the schema
    rejects a ``role`` key, and this ignores the input entirely even if the
    schema were later loosened.
    """
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        employee_code=f"EMP-{uuid4().hex[:8].upper()}",
        role=UserRole.EMPLOYEE,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailAlreadyRegisteredError(payload.email) from exc

    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    """Return the user for valid credentials, or raise.

    The same error is raised for an unknown email and a wrong password, so the
    response cannot be used to enumerate registered addresses.
    """
    user = await get_user_by_email(session, email)

    # Runs a dummy verification when user is None, keeping the timing similar.
    if not verify_password(user.hashed_password if user else None, password):
        raise InvalidCredentialsError(email)

    assert user is not None  # verify_password only returns True with a user

    if not user.is_active:
        raise InactiveUserError(email)

    # Transparently upgrade hashes when Argon2 parameters change.
    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)

    user.last_login_at = datetime.now(UTC)
    await session.commit()
    return user


async def issue_token(user: User, settings: Settings) -> tuple[str, int]:
    """Mint an access token; returns ``(token, expires_in_seconds)``."""
    token, _jti, expires_at = create_access_token(
        subject=user.id, role=user.role.value, settings=settings
    )
    expires_in = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    return token, expires_in


async def revoke_token(
    session: AsyncSession, jti: UUID, user_id: UUID, expires_at: datetime
) -> None:
    """Add a token's jti to the revocation list. Revoking twice is a no-op."""
    if await is_token_revoked(session, jti):
        return
    session.add(RevokedToken(jti=jti, user_id=user_id, expires_at=expires_at))
    try:
        await session.commit()
    except IntegrityError:
        # Lost a race with a concurrent logout; the token is revoked either way.
        await session.rollback()


async def is_token_revoked(session: AsyncSession, jti: UUID) -> bool:
    result = await session.execute(
        select(RevokedToken.id).where(RevokedToken.jti == jti).limit(1)
    )
    return result.scalar_one_or_none() is not None

"""Password hashing and JWT issuing/decoding.

Argon2id is used for passwords (the current OWASP recommendation) via
argon2-cffi directly rather than through passlib, which is unmaintained and
adds a layer that only obscures the parameters.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import (
    HashingError,
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from app.core.config import Settings, get_settings

TOKEN_TYPE_ACCESS: Final = "access"


@lru_cache
def _get_hasher() -> PasswordHasher:
    """The configured Argon2id hasher.

    Built lazily from settings rather than at import time so the work factor
    can be lowered for the test suite; production parameters are floored by a
    validator in ``core.config``.
    """
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.ARGON2_TIME_COST,
        memory_cost=settings.ARGON2_MEMORY_COST,
        parallelism=settings.ARGON2_PARALLELISM,
    )


@lru_cache
def _get_dummy_hash() -> str:
    """A hash to verify against when no user exists.

    Verifying it gives a failed login the same cost as a successful one, so
    response time does not reveal whether an email is registered. It must use
    the same parameters as real hashes or the timing would differ.
    """
    return _get_hasher().hash("never-a-real-password")


class TokenError(Exception):
    """Raised when a token is malformed, tampered with, or expired."""


class ExpiredTokenError(TokenError):
    """Raised specifically when a token's exp has passed."""


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    try:
        return _get_hasher().hash(password)
    except HashingError as exc:  # pragma: no cover - argon2 internal failure
        raise RuntimeError("password hashing failed") from exc


def verify_password(hashed: str | None, password: str) -> bool:
    """Constant-ish time verification.

    ``hashed=None`` (no such user) still performs a real Argon2 verification
    against a dummy hash so the timing matches a genuine failed login.
    """
    if hashed is None:
        with contextlib.suppress(
            VerifyMismatchError, VerificationError, InvalidHashError
        ):
            _get_hasher().verify(_get_dummy_hash(), password)
        return False

    try:
        _get_hasher().verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(hashed: str) -> bool:
    """True when the stored hash predates the current Argon2 parameters."""
    try:
        return _get_hasher().check_needs_rehash(hashed)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TokenPayload:
    subject: uuid.UUID
    role: str
    jti: uuid.UUID
    expires_at: datetime
    token_type: str


def create_access_token(
    subject: uuid.UUID,
    role: str,
    settings: Settings | None = None,
    expires_delta: timedelta | None = None,
) -> tuple[str, uuid.UUID, datetime]:
    """Return ``(encoded_jwt, jti, expires_at)``.

    The jti is returned so logout can record it on the revocation list.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    expires_at = now + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    jti = uuid.uuid4()

    claims: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "jti": str(jti),
        "type": TOKEN_TYPE_ACCESS,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    encoded = jwt.encode(
        claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )
    return encoded, jti, expires_at


def decode_token(token: str, settings: Settings | None = None) -> TokenPayload:
    settings = settings or get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredTokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token is invalid") from exc

    if claims.get("type") != TOKEN_TYPE_ACCESS:
        raise TokenError("unexpected token type")

    try:
        subject = uuid.UUID(claims["sub"])
        jti = uuid.UUID(claims["jti"])
    except (KeyError, ValueError) as exc:
        raise TokenError("token claims are malformed") from exc

    return TokenPayload(
        subject=subject,
        role=str(claims.get("role", "")),
        jti=jti,
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        token_type=TOKEN_TYPE_ACCESS,
    )

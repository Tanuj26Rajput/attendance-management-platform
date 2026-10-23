"""Shared FastAPI dependencies.

All authentication and authorisation funnels through here, so a route is
protected by declaring a dependency rather than by remembering to call a
checker. ``CurrentUser`` means "any authenticated active user";
``CurrentAdmin`` means "an admin".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import ExpiredTokenError, TokenError, TokenPayload, decode_token
from app.db.session import get_session
from app.models.enums import UserRole
from app.models.user import User
from app.services import auth as auth_service

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# auto_error=False so a missing header yields our own 401 with WWW-Authenticate
# rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False, description="JWT access token")
BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]

_UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=_UNAUTHENTICATED,
    )


async def get_token_payload(
    credentials: BearerDep, settings: SettingsDep
) -> TokenPayload:
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Not authenticated")
    try:
        return decode_token(credentials.credentials, settings=settings)
    except ExpiredTokenError as exc:
        raise _unauthorized("Token has expired") from exc
    except TokenError as exc:
        raise _unauthorized("Could not validate credentials") from exc


TokenDep = Annotated[TokenPayload, Depends(get_token_payload)]


async def get_current_user(payload: TokenDep, session: SessionDep) -> User:
    """Resolve the token to a live user.

    The database is consulted on every request rather than trusting the token's
    claims, so deactivating a user or revoking a token takes effect immediately
    instead of when the token happens to expire.
    """
    if await auth_service.is_token_revoked(session, payload.jti):
        raise _unauthorized("Token has been revoked")

    user = await session.get(User, payload.subject)
    if user is None:
        raise _unauthorized("Could not validate credentials")

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user account"
        )

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """Dependency factory restricting a route to the given roles."""

    allowed = frozenset(roles)

    async def _guard(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient privileges for this operation",
            )
        return user

    return _guard


require_admin = require_roles(UserRole.ADMIN)
CurrentAdmin = Annotated[User, Depends(require_admin)]

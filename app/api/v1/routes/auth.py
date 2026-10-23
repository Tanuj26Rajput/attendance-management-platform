"""Registration, login, logout, and the current-user endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep, SettingsDep, TokenDep
from app.schemas.auth import (
    MessageResponse,
    TokenResponse,
    UserLogin,
    UserPublic,
    UserRegister,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new employee account",
)
async def register(
    payload: UserRegister, session: SessionDep, settings: SettingsDep
) -> UserPublic:
    """Public registration. Always creates an *employee*.

    There is no way to obtain an admin account through this endpoint: the
    schema forbids a ``role`` key and the service ignores role input entirely.
    Admins are provisioned out of band.
    """
    try:
        user = await auth_service.register_user(session, payload)
    except auth_service.EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists",
        ) from exc
    return UserPublic.model_validate(user)


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials")
async def login(
    payload: UserLogin, session: SessionDep, settings: SettingsDep
) -> TokenResponse:
    try:
        user = await auth_service.authenticate(session, payload.email, payload.password)
    except auth_service.InvalidCredentialsError as exc:
        # Same response for unknown email and wrong password.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except auth_service.InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user account"
        ) from exc

    token, expires_in = await auth_service.issue_token(user, settings)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/logout", response_model=MessageResponse, summary="Invalidate token")
async def logout(
    token: TokenDep, user: CurrentUser, session: SessionDep
) -> MessageResponse:
    """Revoke the presented access token.

    JWTs are self-contained, so this records the token's ``jti`` on a
    revocation list that every authenticated request checks.
    """
    await auth_service.revoke_token(
        session, jti=token.jti, user_id=user.id, expires_at=token.expires_at
    )
    return MessageResponse(detail="Token revoked")


@router.get("/me", response_model=UserPublic, summary="Current authenticated user")
async def read_me(user: CurrentUser) -> UserPublic:
    return UserPublic.model_validate(user)

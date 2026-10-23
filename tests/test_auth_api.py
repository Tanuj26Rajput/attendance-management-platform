"""End-to-end authentication tests.

Marked integration: they need a real PostgreSQL database (the models use
UUID/JSONB/INET/ARRAY, so SQLite is not an option). Every test runs inside a
transaction that is rolled back, so no rows survive.

Run with:  pytest -m integration
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.core.security import create_access_token
from app.models.enums import UserRole

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-fine-password"


async def _register(api: httpx.AsyncClient, email: str) -> httpx.Response:
    return await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Test Person"},
    )


async def _login(api: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await api.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- registration
async def test_register_creates_an_employee(api, unique_email) -> None:
    response = await _register(api, unique_email())
    assert response.status_code == 201
    body = response.json()
    assert body["role"] == UserRole.EMPLOYEE.value
    assert body["is_active"] is True
    assert body["employee_code"].startswith("EMP-")


async def test_register_never_returns_a_password_hash(api, unique_email) -> None:
    response = await _register(api, unique_email())
    assert "hashed_password" not in response.text
    assert "argon2" not in response.text
    assert "password" not in response.json()


async def test_duplicate_email_is_rejected(api, unique_email) -> None:
    email = unique_email()
    assert (await _register(api, email)).status_code == 201

    duplicate = await _register(api, email)
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


async def test_duplicate_email_is_case_insensitive(api, unique_email) -> None:
    """Emails are lowercased, so Mixed@ and mixed@ are the same account."""
    email = unique_email()
    assert (await _register(api, email)).status_code == 201
    assert (await _register(api, email.upper())).status_code == 409


async def test_registration_cannot_grant_admin(api, unique_email) -> None:
    """A smuggled role is rejected, and no account is created at all."""
    email = unique_email()
    response = await api.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Sneaky",
            "role": "admin",
        },
    )
    assert response.status_code == 422
    assert (await _login(api, email, PASSWORD)).status_code == 401


# --------------------------------------------------------------------- login
async def test_login_returns_a_bearer_token(api, unique_email) -> None:
    email = unique_email()
    await _register(api, email)

    response = await _login(api, email, PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0


async def test_login_with_wrong_password_is_rejected(api, unique_email) -> None:
    email = unique_email()
    await _register(api, email)

    response = await _login(api, email, "definitely-the-wrong-password")
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


async def test_login_with_unknown_email_gives_the_same_error(api, unique_email) -> None:
    """An identical response prevents account enumeration."""
    response = await _login(api, unique_email(), PASSWORD)
    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect email or password"


async def test_inactive_user_cannot_log_in(api, make_user) -> None:
    user = await make_user(password=PASSWORD, is_active=False)
    response = await _login(api, user.email, PASSWORD)
    assert response.status_code == 403
    assert response.json()["detail"] == "Inactive user account"


# ----------------------------------------------------------------------- me
async def test_me_returns_the_authenticated_user(api, unique_email) -> None:
    email = unique_email()
    await _register(api, email)
    token = (await _login(api, email, PASSWORD)).json()["access_token"]

    response = await api.get("/api/v1/auth/me", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert body["role"] == UserRole.EMPLOYEE.value
    assert "hashed_password" not in body


async def test_me_without_a_token_is_unauthorised(api) -> None:
    response = await api.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_me_with_a_malformed_token_is_unauthorised(api) -> None:
    response = await api.get("/api/v1/auth/me", headers=_auth("not.a.jwt"))
    assert response.status_code == 401


async def test_me_with_an_expired_token_is_unauthorised(
    api, make_user, settings
) -> None:
    user = await make_user()
    token, _, _ = create_access_token(
        user.id,
        user.role.value,
        settings=settings,
        expires_delta=timedelta(seconds=-10),
    )
    response = await api.get("/api/v1/auth/me", headers=_auth(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "Token has expired"


async def test_token_for_a_deactivated_user_stops_working(
    api, make_user, db_session
) -> None:
    """Deactivation takes effect immediately, not at token expiry."""
    user = await make_user(password=PASSWORD)
    token = (await _login(api, user.email, PASSWORD)).json()["access_token"]
    assert (await api.get("/api/v1/auth/me", headers=_auth(token))).status_code == 200

    user.is_active = False
    await db_session.flush()

    response = await api.get("/api/v1/auth/me", headers=_auth(token))
    assert response.status_code == 403
    assert response.json()["detail"] == "Inactive user account"


# -------------------------------------------------------------------- logout
async def test_logout_revokes_the_token(api, make_user) -> None:
    user = await make_user(password=PASSWORD)
    token = (await _login(api, user.email, PASSWORD)).json()["access_token"]

    logout = await api.post("/api/v1/auth/logout", headers=_auth(token))
    assert logout.status_code == 200

    response = await api.get("/api/v1/auth/me", headers=_auth(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "Token has been revoked"


async def test_logout_twice_is_not_an_error(api, make_user) -> None:
    user = await make_user(password=PASSWORD)
    token = (await _login(api, user.email, PASSWORD)).json()["access_token"]

    first = await api.post("/api/v1/auth/logout", headers=_auth(token))
    assert first.status_code == 200
    # The second attempt is simply unauthorised: the token is already revoked.
    second = await api.post("/api/v1/auth/logout", headers=_auth(token))
    assert second.status_code == 401


# ------------------------------------------------------- admin authorisation
async def test_employee_cannot_reach_an_admin_route(api, make_user) -> None:
    user = await make_user(password=PASSWORD, role=UserRole.EMPLOYEE)
    token = (await _login(api, user.email, PASSWORD)).json()["access_token"]

    response = await api.get("/api/v1/admin/users", headers=_auth(token))
    assert response.status_code == 403
    assert response.json()["detail"] == "Insufficient privileges for this operation"


async def test_admin_can_reach_an_admin_route(api, make_user) -> None:
    admin = await make_user(password=PASSWORD, role=UserRole.ADMIN)
    token = (await _login(api, admin.email, PASSWORD)).json()["access_token"]

    response = await api.get("/api/v1/admin/users", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert isinstance(body["items"], list)


async def test_admin_route_without_a_token_is_unauthorised(api) -> None:
    response = await api.get("/api/v1/admin/users")
    assert response.status_code == 401


async def test_admin_listing_never_leaks_password_hashes(api, make_user) -> None:
    admin = await make_user(password=PASSWORD, role=UserRole.ADMIN)
    token = (await _login(api, admin.email, PASSWORD)).json()["access_token"]

    response = await api.get("/api/v1/admin/users", headers=_auth(token))
    assert "hashed_password" not in response.text
    assert "argon2" not in response.text

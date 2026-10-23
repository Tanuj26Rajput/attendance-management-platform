"""End-to-end attendance workflow tests.

Integration: needs PostgreSQL. Each test runs in a rolled-back transaction.

Shifts are built relative to "now" with a very wide grace period, so the tests
assert on *workflow* behaviour (duplicate punches, missing punch-in, scoping)
and never on wall-clock-dependent status. Status derivation is covered
exhaustively by the pure unit tests in test_attendance_rules.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.enums import AttendanceStatus, AuditAction

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-fine-password"


async def _token(api: httpx.AsyncClient, user) -> str:
    response = await api.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _now_shift_kwargs() -> dict:
    """A shift starting now, with grace wide enough to never read as late."""
    now = datetime.now(UTC)
    return {
        "start_time": now.time().replace(microsecond=0),
        "end_time": (now + timedelta(hours=8)).time().replace(microsecond=0),
        "break_minutes": 0,
        "grace_period_minutes": 1439,
        "half_day_threshold_minutes": 1,
        "working_days": [1, 2, 3, 4, 5, 6, 7],
    }


@pytest.fixture
async def worker(make_user, make_shift, assign_shift):
    """An employee with a live shift assignment."""
    user = await make_user(password=PASSWORD)
    shift = await make_shift(**_now_shift_kwargs())
    await assign_shift(user, shift)
    return user


# ------------------------------------------------------------------ punch in
async def test_punch_in_opens_the_day(api, worker) -> None:
    token = await _token(api, worker)
    response = await api.post(
        "/api/v1/attendance/punch-in", json={}, headers=_auth(token)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["check_in_at"] is not None
    assert body["check_out_at"] is None
    assert body["shift_id"] is not None


async def test_punch_in_stores_utc(api, worker) -> None:
    token = await _token(api, worker)
    body = (
        await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    ).json()

    checked_in = datetime.fromisoformat(body["check_in_at"])
    assert checked_in.tzinfo is not None, "timestamp must be timezone-aware"
    assert abs((checked_in.astimezone(UTC) - datetime.now(UTC)).total_seconds()) < 120


async def test_duplicate_punch_in_is_rejected(api, worker) -> None:
    token = await _token(api, worker)
    first = await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    assert first.status_code == 201

    second = await api.post(
        "/api/v1/attendance/punch-in", json={}, headers=_auth(token)
    )
    assert second.status_code == 409
    assert "already punched in" in second.json()["detail"].lower()


async def test_punch_in_without_a_shift_is_rejected(api, make_user) -> None:
    user = await make_user(password=PASSWORD)
    token = await _token(api, user)

    response = await api.post(
        "/api/v1/attendance/punch-in", json={}, headers=_auth(token)
    )
    assert response.status_code == 422
    assert "no active shift" in response.json()["detail"].lower()


async def test_punch_in_on_a_non_working_day_is_rejected(
    api, make_user, make_shift, assign_shift
) -> None:
    user = await make_user(password=PASSWORD)
    today_iso = datetime.now(UTC).isoweekday()
    kwargs = _now_shift_kwargs()
    kwargs["working_days"] = [d for d in range(1, 8) if d != today_iso]
    shift = await make_shift(**kwargs)
    await assign_shift(user, shift)

    token = await _token(api, user)
    response = await api.post(
        "/api/v1/attendance/punch-in", json={}, headers=_auth(token)
    )
    assert response.status_code == 422
    assert "not a working day" in response.json()["detail"].lower()


async def test_punch_in_rejects_an_out_of_range_coordinate(api, worker) -> None:
    token = await _token(api, worker)
    response = await api.post(
        "/api/v1/attendance/punch-in",
        json={"latitude": 999, "longitude": 0},
        headers=_auth(token),
    )
    assert response.status_code == 422


async def test_punch_in_rejects_an_unknown_field(api, worker) -> None:
    """A body cannot smuggle in another user's id."""
    token = await _token(api, worker)
    response = await api.post(
        "/api/v1/attendance/punch-in",
        json={"user_id": str(worker.id)},
        headers=_auth(token),
    )
    assert response.status_code == 422


# ----------------------------------------------------------------- punch out
async def test_punch_out_closes_the_day(api, worker) -> None:
    token = await _token(api, worker)
    await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))

    response = await api.post(
        "/api/v1/attendance/punch-out", json={}, headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["check_out_at"] is not None
    assert body["worked_minutes"] is not None
    assert body["worked_minutes"] >= 0


async def test_punch_out_without_punch_in_is_rejected(api, worker) -> None:
    token = await _token(api, worker)
    response = await api.post(
        "/api/v1/attendance/punch-out", json={}, headers=_auth(token)
    )
    assert response.status_code == 409
    assert "no open punch-in" in response.json()["detail"].lower()


async def test_double_punch_out_is_rejected(api, worker) -> None:
    token = await _token(api, worker)
    await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    assert (
        await api.post("/api/v1/attendance/punch-out", json={}, headers=_auth(token))
    ).status_code == 200

    second = await api.post(
        "/api/v1/attendance/punch-out", json={}, headers=_auth(token)
    )
    assert second.status_code == 409


async def test_punch_in_again_after_completing_the_day_is_rejected(api, worker) -> None:
    token = await _token(api, worker)
    await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    await api.post("/api/v1/attendance/punch-out", json={}, headers=_auth(token))

    response = await api.post(
        "/api/v1/attendance/punch-in", json={}, headers=_auth(token)
    )
    assert response.status_code == 409
    assert "already complete" in response.json()["detail"].lower()


# ----------------------------------------------------------------------- me
async def test_me_is_404_before_any_punch(api, worker) -> None:
    token = await _token(api, worker)
    response = await api.get("/api/v1/attendance/me", headers=_auth(token))
    assert response.status_code == 404


async def test_me_returns_todays_record(api, worker) -> None:
    token = await _token(api, worker)
    created = (
        await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    ).json()

    response = await api.get("/api/v1/attendance/me", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_history_lists_own_records(api, worker) -> None:
    token = await _token(api, worker)
    await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))

    response = await api.get("/api/v1/attendance/me/history", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1


async def test_history_rejects_an_inverted_date_range(api, worker) -> None:
    token = await _token(api, worker)
    response = await api.get(
        "/api/v1/attendance/me/history?start=2026-05-01&end=2026-04-01",
        headers=_auth(token),
    )
    assert response.status_code == 422


async def test_history_date_filter_excludes_other_days(api, worker) -> None:
    token = await _token(api, worker)
    await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))

    response = await api.get(
        "/api/v1/attendance/me/history?start=1999-01-01&end=1999-12-31",
        headers=_auth(token),
    )
    assert response.json()["total"] == 0


# ------------------------------------------------------------- cross-user access
async def test_history_never_leaks_another_users_records(
    api, make_user, make_shift, assign_shift
) -> None:
    """Two employees punch in; each sees only their own row."""
    shift = await make_shift(**_now_shift_kwargs())

    alice = await make_user(password=PASSWORD)
    bob = await make_user(password=PASSWORD)
    await assign_shift(alice, shift)
    await assign_shift(bob, shift)

    alice_token = await _token(api, alice)
    bob_token = await _token(api, bob)

    alice_record = (
        await api.post(
            "/api/v1/attendance/punch-in", json={}, headers=_auth(alice_token)
        )
    ).json()
    bob_record = (
        await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(bob_token))
    ).json()
    assert alice_record["id"] != bob_record["id"]

    alice_history = (
        await api.get("/api/v1/attendance/me/history", headers=_auth(alice_token))
    ).json()
    ids = {item["id"] for item in alice_history["items"]}
    assert ids == {alice_record["id"]}
    assert bob_record["id"] not in ids


async def test_punching_in_does_not_affect_another_user(
    api, make_user, make_shift, assign_shift
) -> None:
    shift = await make_shift(**_now_shift_kwargs())
    alice = await make_user(password=PASSWORD)
    bob = await make_user(password=PASSWORD)
    await assign_shift(alice, shift)
    await assign_shift(bob, shift)

    await api.post(
        "/api/v1/attendance/punch-in", json={}, headers=_auth(await _token(api, alice))
    )

    # Bob still has nothing for today.
    bob_today = await api.get(
        "/api/v1/attendance/me", headers=_auth(await _token(api, bob))
    )
    assert bob_today.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/attendance/punch-in"),
        ("POST", "/api/v1/attendance/punch-out"),
        ("GET", "/api/v1/attendance/me"),
        ("GET", "/api/v1/attendance/me/history"),
    ],
)
async def test_attendance_requires_authentication(api, method, path) -> None:
    response = await api.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 401


# --------------------------------------------------------------------- audit
async def test_punches_are_audited(api, worker, db_session) -> None:
    token = await _token(api, worker)
    await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    await api.post("/api/v1/attendance/punch-out", json={}, headers=_auth(token))

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.actor_id == worker.id,
                    AuditLog.entity_type == "attendance_record",
                )
            )
        )
        .scalars()
        .all()
    )

    actions = {row.action for row in rows}
    assert AuditAction.CREATE in actions
    assert AuditAction.UPDATE in actions
    assert all(row.entity_id is not None for row in rows)


async def test_status_is_a_known_value(api, worker) -> None:
    token = await _token(api, worker)
    body = (
        await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    ).json()
    assert body["status"] in {s.value for s in AttendanceStatus}


async def test_a_second_punch_out_says_the_day_is_already_complete(api, worker) -> None:
    """Not "you have no open punch-in" -- they did punch in, and telling them
    otherwise reads like the punch was lost rather than already recorded."""
    token = await _token(api, worker)
    await api.post("/api/v1/attendance/punch-in", json={}, headers=_auth(token))
    await api.post("/api/v1/attendance/punch-out", json={}, headers=_auth(token))

    second = await api.post(
        "/api/v1/attendance/punch-out", json={}, headers=_auth(token)
    )
    assert second.status_code == 409
    detail = second.json()["detail"].lower()
    assert "already punched out" in detail
    assert "no open punch-in" not in detail

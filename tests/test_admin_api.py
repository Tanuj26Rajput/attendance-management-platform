"""Admin directory API tests: permissions, validation, conflicts, history.

Integration: needs PostgreSQL. Each test runs in a rolled-back transaction.
"""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.models.attendance import AttendanceRecord
from app.models.audit import AuditLog
from app.models.enums import UserRole

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-fine-password"
TODAY = dt.date.today()


async def _token(api: httpx.AsyncClient, user) -> str:
    response = await api.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
async def admin(make_user):
    return await make_user(password=PASSWORD, role=UserRole.ADMIN)


@pytest.fixture
async def admin_headers(api, admin):
    return _auth(await _token(api, admin))


@pytest.fixture
async def employee(make_user):
    return await make_user(password=PASSWORD)


def _shift_body(**overrides) -> dict:
    body = {
        "name": f"Shift {_suffix()}",
        "code": f"S{_suffix()[:6]}",
        "start_time": "09:00:00",
        "end_time": "18:00:00",
        "working_days": [1, 2, 3, 4, 5],
    }
    return {**body, **overrides}


def _department_body(**overrides) -> dict:
    s = _suffix()
    return {"name": f"Dept {s}", "code": f"D{s[:6]}", **overrides}


# --------------------------------------------------------------- permissions
ADMIN_ROUTES = [
    ("GET", "/api/v1/admin/departments"),
    ("POST", "/api/v1/admin/departments"),
    ("GET", "/api/v1/admin/shifts"),
    ("POST", "/api/v1/admin/shifts"),
    ("GET", "/api/v1/admin/assignments"),
    ("POST", "/api/v1/admin/assignments"),
    ("GET", "/api/v1/admin/users"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_admin_routes_reject_anonymous_callers(api, method, path) -> None:
    response = await api.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_admin_routes_reject_employees(api, employee, method, path) -> None:
    """An authenticated non-admin must get 403, not 404 or a partial result."""
    headers = _auth(await _token(api, employee))
    response = await api.request(
        method, path, headers=headers, json={} if method == "POST" else None
    )
    assert response.status_code == 403, f"{method} {path} -> {response.status_code}"


async def test_deactivated_admin_loses_access(api, make_user, db_session) -> None:
    """A token issued before deactivation must stop working."""
    user = await make_user(password=PASSWORD, role=UserRole.ADMIN)
    headers = _auth(await _token(api, user))
    assert (await api.get("/api/v1/admin/shifts", headers=headers)).status_code == 200

    user.is_active = False
    await db_session.flush()

    assert (await api.get("/api/v1/admin/shifts", headers=headers)).status_code == 403


# --------------------------------------------------------------- departments
async def test_create_and_list_departments(api, admin_headers) -> None:
    body = _department_body()
    created = await api.post(
        "/api/v1/admin/departments", json=body, headers=admin_headers
    )
    assert created.status_code == 201, created.text
    assert created.json()["code"] == body["code"].upper()
    assert created.json()["is_active"] is True

    listed = await api.get(
        f"/api/v1/admin/departments?search={body['code']}", headers=admin_headers
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


async def test_duplicate_department_code_is_rejected(api, admin_headers) -> None:
    body = _department_body()
    assert (
        await api.post("/api/v1/admin/departments", json=body, headers=admin_headers)
    ).status_code == 201

    clash = await api.post(
        "/api/v1/admin/departments",
        json=_department_body(code=body["code"]),
        headers=admin_headers,
    )
    assert clash.status_code == 409


async def test_deactivate_department_keeps_the_row(api, admin_headers) -> None:
    created = (
        await api.post(
            "/api/v1/admin/departments",
            json=_department_body(),
            headers=admin_headers,
        )
    ).json()

    response = await api.post(
        f"/api/v1/admin/departments/{created['id']}/deactivate", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False

    # Still retrievable -- deactivation is not deletion.
    still_there = await api.get(
        f"/api/v1/admin/departments?is_active=false&search={created['code']}",
        headers=admin_headers,
    )
    assert still_there.json()["total"] == 1


async def test_department_patch_with_no_fields_is_rejected(api, admin_headers) -> None:
    created = (
        await api.post(
            "/api/v1/admin/departments",
            json=_department_body(),
            headers=admin_headers,
        )
    ).json()
    response = await api.patch(
        f"/api/v1/admin/departments/{created['id']}", json={}, headers=admin_headers
    )
    assert response.status_code == 422


async def test_unknown_department_is_404(api, admin_headers) -> None:
    response = await api.patch(
        f"/api/v1/admin/departments/{uuid.uuid4()}",
        json={"name": "Nope"},
        headers=admin_headers,
    )
    assert response.status_code == 404


# -------------------------------------------------------------------- shifts
async def test_create_shift_with_a_timezone(api, admin_headers) -> None:
    body = _shift_body(timezone="Asia/Kolkata", grace_period_minutes=10)
    response = await api.post("/api/v1/admin/shifts", json=body, headers=admin_headers)
    assert response.status_code == 201, response.text

    created = response.json()
    assert created["timezone"] == "Asia/Kolkata"
    assert created["grace_period_minutes"] == 10
    assert created["start_time"] == "09:00:00"
    assert created["is_overnight"] is False


async def test_create_overnight_shift(api, admin_headers) -> None:
    response = await api.post(
        "/api/v1/admin/shifts",
        json=_shift_body(start_time="22:00:00", end_time="06:00:00", is_overnight=True),
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["is_overnight"] is True


async def test_overnight_window_without_the_flag_is_rejected(
    api, admin_headers
) -> None:
    """22:00-06:00 is only coherent as an overnight shift."""
    response = await api.post(
        "/api/v1/admin/shifts",
        json=_shift_body(start_time="22:00:00", end_time="06:00:00"),
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert "is_overnight" in response.text


async def test_same_day_window_flagged_overnight_is_rejected(
    api, admin_headers
) -> None:
    response = await api.post(
        "/api/v1/admin/shifts",
        json=_shift_body(start_time="09:00:00", end_time="18:00:00", is_overnight=True),
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_unknown_timezone_is_rejected(api, admin_headers) -> None:
    response = await api.post(
        "/api/v1/admin/shifts",
        json=_shift_body(timezone="Mars/Olympus_Mons"),
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert "timezone" in response.text


@pytest.mark.parametrize(
    "bad",
    [
        {"working_days": [0]},
        {"working_days": [1, 1]},
        {"working_days": [8]},
        {"grace_period_minutes": -1},
        {"half_day_threshold_minutes": 0},
        {"break_minutes": -5},
    ],
)
async def test_invalid_shift_fields_are_rejected(api, admin_headers, bad) -> None:
    response = await api.post(
        "/api/v1/admin/shifts", json=_shift_body(**bad), headers=admin_headers
    )
    assert response.status_code == 422


async def test_shift_patch_validates_the_merged_window(api, admin_headers) -> None:
    """Moving end_time alone must still be checked against the stored start."""
    created = (
        await api.post(
            "/api/v1/admin/shifts", json=_shift_body(), headers=admin_headers
        )
    ).json()

    response = await api.patch(
        f"/api/v1/admin/shifts/{created['id']}",
        json={"end_time": "08:00:00"},
        headers=admin_headers,
    )
    assert response.status_code == 422

    ok = await api.patch(
        f"/api/v1/admin/shifts/{created['id']}",
        json={"end_time": "08:00:00", "is_overnight": True},
        headers=admin_headers,
    )
    assert ok.status_code == 200
    assert ok.json()["is_overnight"] is True


async def test_shift_listing_paginates(api, admin_headers) -> None:
    marker = _suffix()
    for i in range(3):
        assert (
            await api.post(
                "/api/v1/admin/shifts",
                json=_shift_body(name=f"Pager {marker} {i}"),
                headers=admin_headers,
            )
        ).status_code == 201

    page = await api.get(
        f"/api/v1/admin/shifts?search=Pager {marker}&limit=2&offset=0",
        headers=admin_headers,
    )
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

    second = await api.get(
        f"/api/v1/admin/shifts?search=Pager {marker}&limit=2&offset=2",
        headers=admin_headers,
    )
    assert len(second.json()["items"]) == 1


# --------------------------------------------------------------- assignments
async def test_assign_employee_to_shift(api, admin_headers, employee) -> None:
    shift = (
        await api.post(
            "/api/v1/admin/shifts", json=_shift_body(), headers=admin_headers
        )
    ).json()

    response = await api.post(
        "/api/v1/admin/assignments",
        json={
            "user_id": str(employee.id),
            "shift_id": shift["id"],
            "effective_from": TODAY.isoformat(),
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["effective_to"] is None


async def test_overlapping_assignment_is_rejected(
    api, admin_headers, employee, make_shift, assign_shift
) -> None:
    """An open-ended assignment blocks any later overlapping one."""
    existing = await make_shift()
    await assign_shift(employee, existing, effective_from=TODAY - dt.timedelta(days=30))

    other = (
        await api.post(
            "/api/v1/admin/shifts", json=_shift_body(), headers=admin_headers
        )
    ).json()

    response = await api.post(
        "/api/v1/admin/assignments",
        json={
            "user_id": str(employee.id),
            "shift_id": other["id"],
            "effective_from": TODAY.isoformat(),
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert "already has a shift assignment" in response.json()["detail"]


async def test_non_overlapping_assignment_is_allowed(
    api, admin_headers, employee, make_shift, assign_shift
) -> None:
    """Closing the old assignment first makes room for the next one."""
    old_shift = await make_shift()
    assignment = await assign_shift(
        employee, old_shift, effective_from=TODAY - dt.timedelta(days=30)
    )

    ended = await api.post(
        f"/api/v1/admin/assignments/{assignment.id}/end"
        f"?effective_to={(TODAY - dt.timedelta(days=1)).isoformat()}",
        headers=admin_headers,
    )
    assert ended.status_code == 200, ended.text

    new_shift = (
        await api.post(
            "/api/v1/admin/shifts", json=_shift_body(), headers=admin_headers
        )
    ).json()
    response = await api.post(
        "/api/v1/admin/assignments",
        json={
            "user_id": str(employee.id),
            "shift_id": new_shift["id"],
            "effective_from": TODAY.isoformat(),
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text


async def test_inverted_assignment_dates_are_rejected(
    api, admin_headers, employee, make_shift
) -> None:
    shift = await make_shift()
    response = await api.post(
        "/api/v1/admin/assignments",
        json={
            "user_id": str(employee.id),
            "shift_id": str(shift.id),
            "effective_from": TODAY.isoformat(),
            "effective_to": (TODAY - dt.timedelta(days=5)).isoformat(),
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_cannot_assign_to_an_inactive_shift(api, admin_headers, employee) -> None:
    shift = (
        await api.post(
            "/api/v1/admin/shifts", json=_shift_body(), headers=admin_headers
        )
    ).json()
    assert (
        await api.post(
            f"/api/v1/admin/shifts/{shift['id']}/deactivate", headers=admin_headers
        )
    ).status_code == 200

    response = await api.post(
        "/api/v1/admin/assignments",
        json={
            "user_id": str(employee.id),
            "shift_id": shift["id"],
            "effective_from": TODAY.isoformat(),
        },
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert "inactive shift" in response.json()["detail"]


async def test_assignment_listing_filters_by_date(
    api, admin_headers, employee, make_shift, assign_shift
) -> None:
    shift = await make_shift()
    await assign_shift(employee, shift, effective_from=TODAY - dt.timedelta(days=10))

    live = await api.get(
        f"/api/v1/admin/assignments?user_id={employee.id}&active_on={TODAY}",
        headers=admin_headers,
    )
    assert live.json()["total"] == 1

    before_it_existed = await api.get(
        f"/api/v1/admin/assignments?user_id={employee.id}"
        f"&active_on={(TODAY - dt.timedelta(days=90)).isoformat()}",
        headers=admin_headers,
    )
    assert before_it_existed.json()["total"] == 0


async def test_assignment_cannot_be_repointed_at_another_shift(
    api, admin_headers, employee, make_shift, assign_shift
) -> None:
    """shift_id is not a patchable field, so history cannot be rewritten."""
    shift = await make_shift()
    assignment = await assign_shift(employee, shift)

    response = await api.patch(
        f"/api/v1/admin/assignments/{assignment.id}",
        json={"shift_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert response.status_code == 422


# ----------------------------------------------------------------- history
async def test_editing_a_shift_preserves_recorded_attendance(
    api, admin_headers, make_user, make_shift, assign_shift, db_session
) -> None:
    """A punch keeps the status and shift it was judged with at the time."""
    now = dt.datetime.now(dt.UTC)
    worker = await make_user(password=PASSWORD)
    shift = await make_shift(
        start_time=now.time().replace(microsecond=0),
        end_time=(now + dt.timedelta(hours=8)).time().replace(microsecond=0),
        grace_period_minutes=1439,
        half_day_threshold_minutes=1,
        working_days=[1, 2, 3, 4, 5, 6, 7],
    )
    await assign_shift(worker, shift)

    punched = (
        await api.post(
            "/api/v1/attendance/punch-in",
            json={},
            headers=_auth(await _token(api, worker)),
        )
    ).json()
    assert punched["status"] == "present"

    # Tighten the shift to zero grace, which would have made that punch late.
    patched = await api.patch(
        f"/api/v1/admin/shifts/{shift.id}",
        json={"grace_period_minutes": 0},
        headers=admin_headers,
    )
    assert patched.status_code == 200, patched.text

    record = await db_session.get(AttendanceRecord, uuid.UUID(punched["id"]))
    await db_session.refresh(record)
    assert record.status.value == "present", "history must not be rescored"
    assert record.shift_id == shift.id


async def test_deactivating_a_shift_preserves_recorded_attendance(
    api, admin_headers, make_user, make_shift, assign_shift, db_session
) -> None:
    now = dt.datetime.now(dt.UTC)
    worker = await make_user(password=PASSWORD)
    shift = await make_shift(
        start_time=now.time().replace(microsecond=0),
        end_time=(now + dt.timedelta(hours=8)).time().replace(microsecond=0),
        grace_period_minutes=1439,
        half_day_threshold_minutes=1,
        working_days=[1, 2, 3, 4, 5, 6, 7],
    )
    await assign_shift(worker, shift)
    worker_headers = _auth(await _token(api, worker))

    punched = (
        await api.post("/api/v1/attendance/punch-in", json={}, headers=worker_headers)
    ).json()

    assert (
        await api.post(
            f"/api/v1/admin/shifts/{shift.id}/deactivate", headers=admin_headers
        )
    ).status_code == 200

    # The record survives and is still readable by its owner.
    history = await api.get("/api/v1/attendance/me/history", headers=worker_headers)
    assert history.json()["total"] == 1
    assert history.json()["items"][0]["id"] == punched["id"]

    record = await db_session.get(AttendanceRecord, uuid.UUID(punched["id"]))
    assert record is not None
    assert record.shift_id == shift.id


async def test_retired_shift_blocks_new_punches(
    api, admin_headers, make_user, make_shift, assign_shift
) -> None:
    worker = await make_user(password=PASSWORD)
    shift = await make_shift(working_days=[1, 2, 3, 4, 5, 6, 7])
    await assign_shift(worker, shift)

    assert (
        await api.post(
            f"/api/v1/admin/shifts/{shift.id}/deactivate", headers=admin_headers
        )
    ).status_code == 200

    response = await api.post(
        "/api/v1/attendance/punch-in",
        json={},
        headers=_auth(await _token(api, worker)),
    )
    assert response.status_code == 422
    assert "no active shift" in response.json()["detail"].lower()


# ------------------------------------------------------------------- audit
async def test_directory_changes_are_audited(api, admin, admin_headers, db_session):
    created = (
        await api.post(
            "/api/v1/admin/shifts", json=_shift_body(), headers=admin_headers
        )
    ).json()
    await api.patch(
        f"/api/v1/admin/shifts/{created['id']}",
        json={"grace_period_minutes": 5},
        headers=admin_headers,
    )

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.actor_id == admin.id,
                    AuditLog.entity_type == "shift",
                )
            )
        )
        .scalars()
        .all()
    )

    actions = {row.action.value for row in rows}
    assert {"create", "update"} <= actions

    update = next(r for r in rows if r.action.value == "update")
    assert update.changes["after"]["grace_period_minutes"] == 5
    assert update.changes["before"]["grace_period_minutes"] == 15


# ------------------------------------- RBAC over every privileged route
# ADMIN_ROUTES above is hand-written, so a route added later is protected by
# whoever remembers to extend it. These derive the list from the application's
# own schema instead: every path the app actually serves under /admin or
# /reports is checked, and a new one is covered the moment it exists.
def _privileged_routes() -> list[tuple[str, str]]:
    from app.main import create_app

    schema = create_app().openapi()
    routes = [
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method in {"get", "post", "patch", "put", "delete"}
        and ("/admin/" in path or "/reports/" in path)
    ]
    assert routes, "no privileged routes found -- the filter is wrong"
    return sorted(routes)


def _concrete(path: str) -> str:
    """Fill path parameters with a well-formed id that matches nothing.

    Authorisation must be decided before the row is looked up, so a caller
    who may not use the route gets 403 rather than 404 -- which would other-
    wise confirm whether a given id exists.
    """
    out = path
    while "{" in out:
        start, end = out.index("{"), out.index("}")
        out = out[:start] + str(uuid.uuid4()) + out[end + 1 :]
    return out


@pytest.mark.parametrize(("method", "path"), _privileged_routes())
async def test_every_privileged_route_rejects_anonymous_callers(
    api, method, path
) -> None:
    response = await api.request(
        method, _concrete(path), json={} if method in {"POST", "PATCH", "PUT"} else None
    )
    assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


@pytest.mark.parametrize(("method", "path"), _privileged_routes())
async def test_every_privileged_route_rejects_employees(
    api, employee, method, path
) -> None:
    headers = _auth(await _token(api, employee))
    response = await api.request(
        method,
        _concrete(path),
        headers=headers,
        json={} if method in {"POST", "PATCH", "PUT"} else None,
    )
    assert response.status_code == 403, f"{method} {path} -> {response.status_code}"

"""Reporting integration tests: derivation, leave, timezones, RBAC.

Integration: needs PostgreSQL. Each test runs in a rolled-back transaction.

Dates are fixed in May 2026, comfortably in the past, so every shift in range
has finished and the "day not over yet" rule never interferes. The tests that
are *about* that rule call ``build_grid`` directly with an explicit ``now``.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import httpx
import pytest
from sqlalchemy import event

from app.models.attendance import AttendanceRecord
from app.models.enums import (
    AttendanceStatus,
    EmploymentStatus,
    LeaveStatus,
    LeaveType,
    UserRole,
)
from app.models.leave import LeaveRequest
from app.services import reports

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-fine-password"

# A Monday, and the four days after it.
MON = dt.date(2026, 5, 4)
TUE = dt.date(2026, 5, 5)
WED = dt.date(2026, 5, 6)
RANGE_START = MON
RANGE_END = dt.date(2026, 5, 8)  # Friday

EVERY_DAY = [1, 2, 3, 4, 5, 6, 7]
WEEKDAYS = [1, 2, 3, 4, 5]


async def _token(api: httpx.AsyncClient, user) -> str:
    response = await api.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin(make_user):
    return await make_user(password=PASSWORD, role=UserRole.ADMIN)


@pytest.fixture
async def admin_headers(api, admin):
    return _auth(await _token(api, admin))


@pytest.fixture
async def add_record(db_session):
    """Insert an attendance record directly.

    Reports read history, so the tests write history straight in rather than
    punching -- punching can only ever produce *today*.
    """

    async def _add(
        user,
        work_date: dt.date,
        *,
        status: AttendanceStatus = AttendanceStatus.PRESENT,
        shift=None,
        late_minutes: int = 0,
        worked_minutes: int | None = 480,
    ) -> AttendanceRecord:
        record = AttendanceRecord(
            user_id=user.id,
            work_date=work_date,
            shift_id=shift.id if shift is not None else None,
            check_in_at=dt.datetime.combine(work_date, dt.time(9, 0), tzinfo=dt.UTC),
            check_out_at=dt.datetime.combine(work_date, dt.time(18, 0), tzinfo=dt.UTC),
            status=status,
            late_minutes=late_minutes,
            worked_minutes=worked_minutes,
        )
        db_session.add(record)
        await db_session.flush()
        return record

    return _add


@pytest.fixture
async def add_leave(db_session, admin):
    async def _add(
        user,
        start: dt.date,
        end: dt.date,
        *,
        status: LeaveStatus = LeaveStatus.APPROVED,
    ) -> LeaveRequest:
        decided = status in (LeaveStatus.APPROVED, LeaveStatus.REJECTED)
        request = LeaveRequest(
            user_id=user.id,
            leave_type=LeaveType.ANNUAL,
            start_date=start,
            end_date=end,
            total_days=decimal.Decimal((end - start).days + 1),
            status=status,
            reviewed_by_id=admin.id if decided else None,
            reviewed_at=dt.datetime.now(dt.UTC) if decided else None,
        )
        db_session.add(request)
        await db_session.flush()
        return request

    return _add


@pytest.fixture
async def rostered(make_user, make_shift, assign_shift):
    """An employee working every day, assigned well before the range."""

    async def _make(*, timezone: str = "UTC", working_days=None, **user_kwargs):
        user = await make_user(password=PASSWORD, **user_kwargs)
        shift = await make_shift(
            timezone=timezone,
            working_days=working_days if working_days is not None else EVERY_DAY,
        )
        await assign_shift(user, shift, effective_from=MON - dt.timedelta(days=60))
        return user, shift

    return _make


# ----------------------------------------------------------------- RBAC
REPORT_ROUTES = [
    f"/api/v1/reports/daily?on={MON}",
    f"/api/v1/reports/daily-summaries?start={RANGE_START}&end={RANGE_END}",
    f"/api/v1/reports/trends?start={RANGE_START}&end={RANGE_END}",
    f"/api/v1/reports/late-arrivals?start={RANGE_START}&end={RANGE_END}",
    f"/api/v1/reports/absences?start={RANGE_START}&end={RANGE_END}",
    f"/api/v1/reports/absenteeism?start={RANGE_START}&end={RANGE_END}",
]


@pytest.mark.parametrize("path", REPORT_ROUTES)
async def test_reports_reject_anonymous_callers(api, path) -> None:
    assert (await api.get(path)).status_code == 401


@pytest.mark.parametrize("path", REPORT_ROUTES)
async def test_reports_reject_employees(api, make_user, path) -> None:
    """Reports expose other people's attendance, so employees get 403."""
    employee = await make_user(password=PASSWORD)
    headers = _auth(await _token(api, employee))
    assert (await api.get(path, headers=headers)).status_code == 403


# ----------------------------------------------------------- empty data
async def test_daily_summary_is_zero_filled_when_nobody_is_expected(
    api, admin_headers
) -> None:
    response = await api.get(f"/api/v1/reports/daily?on={MON}", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expected"] == 0
    assert body["attendance_rate"] == 0.0
    assert body["work_date"] == MON.isoformat()


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/reports/daily-summaries?start={RANGE_START}&end={RANGE_END}",
        f"/api/v1/reports/late-arrivals?start={RANGE_START}&end={RANGE_END}",
        f"/api/v1/reports/absences?start={RANGE_START}&end={RANGE_END}",
        f"/api/v1/reports/absenteeism?start={RANGE_START}&end={RANGE_END}",
    ],
)
async def test_listings_are_empty_not_an_error_with_no_data(
    api, admin_headers, path
) -> None:
    response = await api.get(path, headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_employee_without_an_assignment_is_never_counted(
    api, admin_headers, make_user
) -> None:
    """No roster means no expectation, so no absence."""
    await make_user(password=PASSWORD)
    response = await api.get(
        f"/api/v1/reports/absences?start={RANGE_START}&end={RANGE_END}",
        headers=admin_headers,
    )
    assert response.json()["total"] == 0


# -------------------------------------------------------- date boundaries
async def test_inverted_range_is_rejected(api, admin_headers) -> None:
    response = await api.get(
        f"/api/v1/reports/daily-summaries?start={RANGE_END}&end={RANGE_START}",
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert "before start" in response.json()["detail"]


async def test_excessive_range_is_rejected(api, admin_headers) -> None:
    far = RANGE_START + dt.timedelta(days=reports.MAX_RANGE_DAYS)
    response = await api.get(
        f"/api/v1/reports/daily-summaries?start={RANGE_START}&end={far}",
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert "maximum" in response.json()["detail"]


async def test_missing_date_parameters_are_rejected(api, admin_headers) -> None:
    assert (
        await api.get("/api/v1/reports/daily-summaries", headers=admin_headers)
    ).status_code == 422


async def test_malformed_date_is_rejected(api, admin_headers) -> None:
    assert (
        await api.get(
            "/api/v1/reports/daily-summaries?start=not-a-date&end=2026-05-08",
            headers=admin_headers,
        )
    ).status_code == 422


async def test_range_bounds_are_inclusive(api, admin_headers, rostered) -> None:
    """A record on the first and last day must both be inside the range."""
    await rostered()
    response = await api.get(
        f"/api/v1/reports/daily-summaries?start={MON}&end={WED}",
        headers=admin_headers,
    )
    dates = [item["work_date"] for item in response.json()["items"]]
    assert dates == [MON.isoformat(), TUE.isoformat(), WED.isoformat()]


async def test_days_outside_the_range_are_excluded(
    api, admin_headers, rostered, add_record
) -> None:
    user, shift = await rostered()
    await add_record(
        user, MON, status=AttendanceStatus.LATE, late_minutes=30, shift=shift
    )
    await add_record(
        user, WED, status=AttendanceStatus.LATE, late_minutes=45, shift=shift
    )

    only_monday = await api.get(
        f"/api/v1/reports/late-arrivals?start={MON}&end={MON}", headers=admin_headers
    )
    assert only_monday.json()["total"] == 1
    assert only_monday.json()["items"][0]["late_minutes"] == 30


# --------------------------------------------------------- derived absence
async def test_a_missing_record_on_a_working_day_is_an_absence(
    api, admin_headers, rostered
) -> None:
    user, _ = await rostered()
    response = await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )
    body = response.json()
    assert body["total"] == 3
    assert {item["work_date"] for item in body["items"]} == {
        MON.isoformat(),
        TUE.isoformat(),
        WED.isoformat(),
    }
    assert body["items"][0]["employee_code"] == user.employee_code


async def test_non_working_days_are_not_absences(api, admin_headers, rostered) -> None:
    """A weekday-only shift must not be marked absent on the weekend."""
    await rostered(working_days=WEEKDAYS)
    saturday, sunday = dt.date(2026, 5, 9), dt.date(2026, 5, 10)

    response = await api.get(
        f"/api/v1/reports/absences?start={saturday}&end={sunday}",
        headers=admin_headers,
    )
    assert response.json()["total"] == 0


async def test_a_present_record_is_not_an_absence(
    api, admin_headers, rostered, add_record
) -> None:
    user, shift = await rostered()
    await add_record(user, MON, shift=shift)

    response = await api.get(
        f"/api/v1/reports/absences?start={MON}&end={MON}", headers=admin_headers
    )
    assert response.json()["total"] == 0


# ------------------------------------------------------------ approved leave
async def test_approved_leave_is_not_an_absence(
    api, admin_headers, rostered, add_leave
) -> None:
    user, _ = await rostered()
    await add_leave(user, MON, TUE)

    absences = await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )
    assert [i["work_date"] for i in absences.json()["items"]] == [WED.isoformat()]

    monday = await api.get(f"/api/v1/reports/daily?on={MON}", headers=admin_headers)
    assert monday.json()["on_leave"] == 1
    assert monday.json()["absent"] == 0


@pytest.mark.parametrize(
    "status", [LeaveStatus.PENDING, LeaveStatus.REJECTED, LeaveStatus.CANCELLED]
)
async def test_undecided_or_refused_leave_does_not_excuse_an_absence(
    api, admin_headers, rostered, add_leave, status
) -> None:
    """Otherwise anyone could hide an absence by filing a request."""
    user, _ = await rostered()
    await add_leave(user, MON, MON, status=status)

    response = await api.get(
        f"/api/v1/reports/absences?start={MON}&end={MON}", headers=admin_headers
    )
    assert response.json()["total"] == 1


async def test_leave_is_excluded_from_the_attendance_rate(
    api, admin_headers, rostered, add_leave, add_record
) -> None:
    present, shift = await rostered()
    on_leave, _ = await rostered()
    await add_record(present, MON, shift=shift)
    await add_leave(on_leave, MON, MON)

    body = (
        await api.get(f"/api/v1/reports/daily?on={MON}", headers=admin_headers)
    ).json()
    assert body["expected"] == 2
    assert body["on_leave"] == 1
    # One expected at work, one attended.
    assert body["attendance_rate"] == 1.0


# ------------------------------------------------------- employee status
async def test_inactive_employees_are_excluded(api, admin_headers, rostered) -> None:
    """A leaver must not be counted absent for every day since they left."""
    await rostered(is_active=False)
    response = await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )
    assert response.json()["total"] == 0


async def test_terminated_employees_are_excluded(
    api, admin_headers, rostered, db_session
) -> None:
    user, _ = await rostered()
    user.employment_status = EmploymentStatus.TERMINATED
    await db_session.flush()

    response = await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )
    assert response.json()["total"] == 0


# ------------------------------------------------------------- timezones
async def test_an_unfinished_day_is_not_an_absence(db_session, rostered) -> None:
    """Midday on a 09:00-18:00 shift is not yet a failure to attend."""
    await rostered()
    midday = dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC)

    grid = await reports.build_grid(db_session, MON, MON, now=midday)
    assert grid.days == []

    after_the_shift = dt.datetime(2026, 5, 4, 19, 0, tzinfo=dt.UTC)
    grid = await reports.build_grid(db_session, MON, MON, now=after_the_shift)
    assert [d.status for d in grid.days] == [AttendanceStatus.ABSENT]


async def test_day_completion_is_judged_in_the_shifts_timezone(
    db_session, rostered
) -> None:
    """Same instant, same calendar date, two zones -- only one day is over.

    18:00 on 2026-05-04 is 04:00 UTC in Kiritimati (UTC+14) but 05:00 UTC the
    *next* day in Niue (UTC-11), so at 12:00 UTC the first shift has finished
    and the second has not even started.
    """
    early, _ = await rostered(timezone="Pacific/Kiritimati")
    still_running, _ = await rostered(timezone="Pacific/Niue")
    noon_utc = dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC)

    grid = await reports.build_grid(db_session, MON, MON, now=noon_utc)
    assert [d.user_id for d in grid.days] == [early.id]
    assert still_running.id not in {d.user_id for d in grid.days}


async def test_an_overnight_shift_is_complete_only_after_midnight(
    db_session, make_user, make_shift, assign_shift
) -> None:
    user = await make_user(password=PASSWORD)
    shift = await make_shift(
        start_time=dt.time(22, 0),
        end_time=dt.time(6, 0),
        is_overnight=True,
        working_days=EVERY_DAY,
    )
    await assign_shift(user, shift, effective_from=MON - dt.timedelta(days=60))

    # 23:00 on Monday: the shift that started at 22:00 is still running.
    during = dt.datetime(2026, 5, 4, 23, 0, tzinfo=dt.UTC)
    assert (await reports.build_grid(db_session, MON, MON, now=during)).days == []

    # 07:00 on Tuesday, after the 06:00 finish.
    after = dt.datetime(2026, 5, 5, 7, 0, tzinfo=dt.UTC)
    assert len((await reports.build_grid(db_session, MON, MON, now=after)).days) == 1


# ----------------------------------------------------- late arrivals etc.
async def test_late_arrivals_are_ranked_worst_first(
    api, admin_headers, rostered, add_record
) -> None:
    user, shift = await rostered()
    await add_record(
        user, MON, status=AttendanceStatus.LATE, late_minutes=5, shift=shift
    )
    await add_record(
        user, TUE, status=AttendanceStatus.LATE, late_minutes=50, shift=shift
    )

    items = (
        await api.get(
            f"/api/v1/reports/late-arrivals?start={MON}&end={WED}",
            headers=admin_headers,
        )
    ).json()["items"]
    assert [i["late_minutes"] for i in items] == [50, 5]


async def test_late_arrivals_respect_the_minimum_threshold(
    api, admin_headers, rostered, add_record
) -> None:
    user, shift = await rostered()
    await add_record(
        user, MON, status=AttendanceStatus.LATE, late_minutes=5, shift=shift
    )
    await add_record(
        user, TUE, status=AttendanceStatus.LATE, late_minutes=50, shift=shift
    )

    response = await api.get(
        f"/api/v1/reports/late-arrivals?start={MON}&end={WED}&min_late_minutes=30",
        headers=admin_headers,
    )
    assert response.json()["total"] == 1


async def test_reports_can_be_filtered_to_one_employee(
    api, admin_headers, rostered, add_record
) -> None:
    alice, alice_shift = await rostered()
    bob, bob_shift = await rostered()
    await add_record(
        alice, MON, status=AttendanceStatus.LATE, late_minutes=10, shift=alice_shift
    )
    await add_record(
        bob, MON, status=AttendanceStatus.LATE, late_minutes=20, shift=bob_shift
    )

    response = await api.get(
        f"/api/v1/reports/late-arrivals?start={MON}&end={MON}&user_id={alice.id}",
        headers=admin_headers,
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["user_id"] == str(alice.id)


# --------------------------------------------------------------- trends
async def test_trend_granularity_changes_the_bucket_count(
    api, admin_headers, rostered
) -> None:
    await rostered()
    base = f"/api/v1/reports/trends?start={MON}&end={RANGE_END}"

    daily = await api.get(f"{base}&granularity=day", headers=admin_headers)
    weekly = await api.get(f"{base}&granularity=week", headers=admin_headers)

    assert daily.json()["total"] == 5
    assert weekly.json()["total"] == 1
    assert weekly.json()["items"][0]["expected"] == 5


async def test_unknown_granularity_is_rejected(api, admin_headers) -> None:
    response = await api.get(
        f"/api/v1/reports/trends?start={MON}&end={WED}&granularity=fortnight",
        headers=admin_headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------- absenteeism
async def test_absenteeism_by_employee(
    api, admin_headers, rostered, add_record
) -> None:
    user, shift = await rostered()
    await add_record(user, MON, shift=shift)  # present
    # Tuesday and Wednesday have no record -> absent.

    response = await api.get(
        f"/api/v1/reports/absenteeism?start={MON}&end={WED}&group_by=employee",
        headers=admin_headers,
    )
    row = response.json()["items"][0]
    assert row["expected_days"] == 3
    assert row["absent_days"] == 2
    assert row["absence_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert user.employee_code in row["group_label"]


async def test_absenteeism_by_department_separates_groups(
    api, admin_headers, rostered, add_record, db_session
) -> None:
    from app.models.department import Department

    good = Department(
        name=f"Good {uuid.uuid4().hex[:6]}", code=f"G{uuid.uuid4().hex[:6]}"
    )
    bad = Department(
        name=f"Bad {uuid.uuid4().hex[:6]}", code=f"B{uuid.uuid4().hex[:6]}"
    )
    db_session.add_all([good, bad])
    await db_session.flush()

    diligent, diligent_shift = await rostered()
    absentee, _ = await rostered()
    diligent.department_id = good.id
    absentee.department_id = bad.id
    await db_session.flush()

    await add_record(diligent, MON, shift=diligent_shift)

    rows = (
        await api.get(
            f"/api/v1/reports/absenteeism?start={MON}&end={MON}",
            headers=admin_headers,
        )
    ).json()["items"]

    by_id = {row["group_id"]: row for row in rows}
    assert by_id[str(bad.id)]["absence_rate"] == 1.0
    assert by_id[str(good.id)]["absence_rate"] == 0.0


async def test_department_filter_narrows_the_report(
    api, admin_headers, rostered, db_session
) -> None:
    from app.models.department import Department

    dept = Department(
        name=f"Dept {uuid.uuid4().hex[:6]}", code=f"D{uuid.uuid4().hex[:6]}"
    )
    db_session.add(dept)
    await db_session.flush()

    inside, _ = await rostered()
    await rostered()  # outside the department
    inside.department_id = dept.id
    await db_session.flush()

    response = await api.get(
        f"/api/v1/reports/absences?start={MON}&end={MON}&department_id={dept.id}",
        headers=admin_headers,
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["user_id"] == str(inside.id)
    assert body["items"][0]["department_name"] == dept.name


# --------------------------------------------------------------- paging
async def test_report_listings_paginate(api, admin_headers, rostered) -> None:
    await rostered()
    base = f"/api/v1/reports/daily-summaries?start={MON}&end={RANGE_END}"

    first = (await api.get(f"{base}&limit=2&offset=0", headers=admin_headers)).json()
    second = (await api.get(f"{base}&limit=2&offset=4", headers=admin_headers)).json()

    assert first["total"] == 5
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1


async def test_pagination_bounds_are_validated(api, admin_headers) -> None:
    base = f"/api/v1/reports/absences?start={MON}&end={WED}"
    assert (await api.get(f"{base}&limit=0", headers=admin_headers)).status_code == 422
    assert (
        await api.get(f"{base}&offset=-1", headers=admin_headers)
    ).status_code == 422


# ----------------------------------------------------------------- N+1
async def test_grid_query_count_does_not_grow_with_the_data(
    db_session, rostered, add_record
) -> None:
    """The grid must cost the same whether it covers one employee or many.

    Counting statements is the only way to pin this: a correct-looking report
    built with a per-employee query would pass every other test in this file.
    """
    counter = {"n": 0}

    def _count(*args: object, **kwargs: object) -> None:
        counter["n"] += 1

    # get_bind() already hands back the synchronous Engine behind the
    # AsyncSession, which is where the cursor events fire.
    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", _count)
    try:
        one, shift = await rostered()
        await add_record(one, MON, shift=shift)
        counter["n"] = 0
        small = await reports.build_grid(db_session, MON, RANGE_END)
        small_queries = counter["n"]

        for _ in range(5):
            await rostered()
        counter["n"] = 0
        large = await reports.build_grid(db_session, MON, RANGE_END)
        large_queries = counter["n"]
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert len(large.days) > len(small.days), "the second grid must be bigger"
    assert small_queries == large_queries
    assert large_queries <= 6, (
        f"expected a flat handful of queries, got {large_queries}"
    )

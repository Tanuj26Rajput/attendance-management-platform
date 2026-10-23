"""Attendance rule unit tests: timezones, grace periods, shift boundaries.

These need no database, so they run in the default suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.models.enums import AttendanceStatus
from app.services.attendance_rules import (
    ShiftRules,
    evaluate_punch_in,
    evaluate_punch_out,
    is_working_day,
    resolve_timezone,
    resolve_work_date,
    scheduled_end,
    to_local,
)

IST = ZoneInfo("Asia/Kolkata")  # UTC+5:30, no DST
NYC = ZoneInfo("America/New_York")  # has DST


def day_shift(**overrides) -> ShiftRules:
    base = {
        "start_time": time(9, 0),
        "end_time": time(18, 0),
        "is_overnight": False,
        "break_minutes": 60,
        "grace_period_minutes": 15,
        "half_day_threshold_minutes": 240,
        "working_days": (1, 2, 3, 4, 5),
    }
    return ShiftRules(**{**base, **overrides})


def night_shift(**overrides) -> ShiftRules:
    return day_shift(
        start_time=time(22, 0), end_time=time(6, 0), is_overnight=True, **overrides
    )


# ----------------------------------------------------------------- timezones
def test_unknown_timezone_falls_back_to_utc() -> None:
    assert resolve_timezone("Mars/Olympus") == ZoneInfo("UTC")
    assert resolve_timezone(None) == ZoneInfo("UTC")


def test_naive_instants_are_treated_as_utc() -> None:
    naive = datetime(2026, 3, 2, 3, 30)
    assert to_local(naive, IST).hour == 9


def test_same_instant_is_on_time_in_one_zone_and_late_in_another() -> None:
    """03:30 UTC is 09:00 in Kolkata (on time) but 22:30 the night before in NYC."""
    instant = datetime(2026, 3, 2, 3, 30, tzinfo=UTC)

    ist = evaluate_punch_in(instant, day_shift(), IST)
    assert ist.status is AttendanceStatus.PRESENT
    assert ist.late_minutes == 0
    assert ist.work_date == date(2026, 3, 2)

    nyc = evaluate_punch_in(instant, day_shift(), NYC)
    assert nyc.work_date == date(2026, 3, 1)
    assert nyc.status is AttendanceStatus.LATE


def test_dst_transition_is_handled_by_the_zone() -> None:
    """US DST began 2026-03-08; 09:00 local is on time either side of it."""
    for day in (date(2026, 3, 6), date(2026, 3, 10)):
        local_nine = datetime.combine(day, time(9, 0), tzinfo=NYC)
        outcome = evaluate_punch_in(local_nine.astimezone(UTC), day_shift(), NYC)
        assert outcome.status is AttendanceStatus.PRESENT, day
        assert outcome.work_date == day


# -------------------------------------------------------------- grace period
@pytest.mark.parametrize(
    ("local_time", "expected_status", "expected_late"),
    [
        (time(8, 45), AttendanceStatus.PRESENT, 0),  # early
        (time(9, 0), AttendanceStatus.PRESENT, 0),  # exactly on time
        (time(9, 15), AttendanceStatus.PRESENT, 0),  # last moment of grace
        (time(9, 16), AttendanceStatus.LATE, 1),  # one minute over
        (time(10, 0), AttendanceStatus.LATE, 45),
    ],
)
def test_grace_period_boundary(local_time, expected_status, expected_late) -> None:
    instant = datetime.combine(date(2026, 3, 2), local_time, tzinfo=IST)
    outcome = evaluate_punch_in(instant.astimezone(UTC), day_shift(), IST)
    assert outcome.status is expected_status
    assert outcome.late_minutes == expected_late


def test_zero_grace_makes_one_minute_late() -> None:
    instant = datetime(2026, 3, 2, 9, 1, tzinfo=IST)
    outcome = evaluate_punch_in(
        instant.astimezone(UTC), day_shift(grace_period_minutes=0), IST
    )
    assert outcome.late_minutes == 1


# ------------------------------------------------------------ shift boundaries
def test_overnight_shift_ends_on_the_next_day() -> None:
    end = scheduled_end(date(2026, 3, 2), night_shift(), IST)
    assert end == datetime(2026, 3, 3, 6, 0, tzinfo=IST)


def test_day_shift_ends_the_same_day() -> None:
    end = scheduled_end(date(2026, 3, 2), day_shift(), IST)
    assert end == datetime(2026, 3, 2, 18, 0, tzinfo=IST)


def test_after_midnight_punch_belongs_to_the_previous_business_day() -> None:
    """A 22:00-06:00 worker arriving at 00:30 is late for yesterday's shift."""
    local = datetime(2026, 3, 3, 0, 30, tzinfo=IST)
    assert resolve_work_date(local, night_shift()) == date(2026, 3, 2)


def test_before_midnight_punch_belongs_to_the_same_day() -> None:
    local = datetime(2026, 3, 2, 22, 5, tzinfo=IST)
    assert resolve_work_date(local, night_shift()) == date(2026, 3, 2)


def test_day_shift_never_shifts_the_work_date() -> None:
    local = datetime(2026, 3, 2, 0, 30, tzinfo=IST)
    assert resolve_work_date(local, day_shift()) == date(2026, 3, 2)


@pytest.mark.parametrize(
    ("day", "expected"),
    [(date(2026, 3, 2), True), (date(2026, 3, 7), False)],  # Monday, Saturday
)
def test_working_days_respected(day, expected) -> None:
    assert is_working_day(day, day_shift()) is expected


def test_empty_working_days_means_every_day() -> None:
    assert is_working_day(date(2026, 3, 7), day_shift(working_days=())) is True


# ------------------------------------------------------------------ punch out
def test_worked_minutes_excludes_the_break() -> None:
    start = datetime(2026, 3, 2, 9, 0, tzinfo=IST)
    end = datetime(2026, 3, 2, 18, 0, tzinfo=IST)
    outcome = evaluate_punch_out(
        start.astimezone(UTC),
        end.astimezone(UTC),
        date(2026, 3, 2),
        day_shift(),
        IST,
        late_minutes=0,
    )
    assert outcome.worked_minutes == 9 * 60 - 60
    assert outcome.status is AttendanceStatus.PRESENT
    assert outcome.early_leave_minutes == 0
    assert outcome.overtime_minutes == 0


def test_leaving_early_is_measured_against_shift_end() -> None:
    start = datetime(2026, 3, 2, 9, 0, tzinfo=IST)
    end = datetime(2026, 3, 2, 16, 30, tzinfo=IST)
    outcome = evaluate_punch_out(
        start.astimezone(UTC),
        end.astimezone(UTC),
        date(2026, 3, 2),
        day_shift(),
        IST,
        late_minutes=0,
    )
    assert outcome.early_leave_minutes == 90
    assert outcome.overtime_minutes == 0


def test_staying_late_counts_as_overtime() -> None:
    start = datetime(2026, 3, 2, 9, 0, tzinfo=IST)
    end = datetime(2026, 3, 2, 20, 0, tzinfo=IST)
    outcome = evaluate_punch_out(
        start.astimezone(UTC),
        end.astimezone(UTC),
        date(2026, 3, 2),
        day_shift(),
        IST,
        late_minutes=0,
    )
    assert outcome.overtime_minutes == 120
    assert outcome.early_leave_minutes == 0


def test_short_day_becomes_half_day() -> None:
    start = datetime(2026, 3, 2, 9, 0, tzinfo=IST)
    end = datetime(2026, 3, 2, 12, 0, tzinfo=IST)
    outcome = evaluate_punch_out(
        start.astimezone(UTC),
        end.astimezone(UTC),
        date(2026, 3, 2),
        day_shift(),
        IST,
        late_minutes=0,
    )
    assert outcome.worked_minutes == 120
    assert outcome.status is AttendanceStatus.HALF_DAY


def test_lateness_survives_a_full_day() -> None:
    """A late arrival stays LATE after a complete shift."""
    start = datetime(2026, 3, 2, 10, 0, tzinfo=IST)
    end = datetime(2026, 3, 2, 19, 0, tzinfo=IST)
    outcome = evaluate_punch_out(
        start.astimezone(UTC),
        end.astimezone(UTC),
        date(2026, 3, 2),
        day_shift(),
        IST,
        late_minutes=45,
    )
    assert outcome.status is AttendanceStatus.LATE


def test_overnight_shift_spanning_midnight_is_a_full_day() -> None:
    start = datetime(2026, 3, 2, 22, 0, tzinfo=IST)
    end = datetime(2026, 3, 3, 6, 0, tzinfo=IST)
    outcome = evaluate_punch_out(
        start.astimezone(UTC),
        end.astimezone(UTC),
        date(2026, 3, 2),
        night_shift(break_minutes=30),
        IST,
        late_minutes=0,
    )
    assert outcome.worked_minutes == 8 * 60 - 30
    assert outcome.early_leave_minutes == 0
    assert outcome.overtime_minutes == 0
    assert outcome.status is AttendanceStatus.PRESENT


# ----------------------------------------------- the shift's own timezone
def test_shift_zone_defaults_to_utc() -> None:
    assert day_shift().zone is resolve_timezone("UTC")


def test_shift_zone_is_read_from_the_shift_not_the_employee() -> None:
    """A 09:00 IST shift starts at 03:30 UTC, whoever is rostered onto it."""
    rules = day_shift(timezone="Asia/Kolkata")
    assert rules.zone == IST

    on_time = evaluate_punch_in(
        datetime(2026, 5, 4, 3, 30, tzinfo=UTC), rules, rules.zone
    )
    assert on_time.status is AttendanceStatus.PRESENT
    assert on_time.late_minutes == 0

    # 04:00 UTC is 09:30 IST: 15 minutes past the 15-minute grace period.
    late = evaluate_punch_in(datetime(2026, 5, 4, 4, 0, tzinfo=UTC), rules, rules.zone)
    assert late.status is AttendanceStatus.LATE
    assert late.late_minutes == 15


def test_same_instant_differs_by_shift_zone() -> None:
    """One instant, two shifts: on time in Kolkata, hours late in New York."""
    punched = datetime(2026, 5, 4, 3, 30, tzinfo=UTC)

    in_kolkata = evaluate_punch_in(punched, day_shift(timezone="Asia/Kolkata"), IST)
    in_nyc = evaluate_punch_in(punched, day_shift(timezone="America/New_York"), NYC)

    assert in_kolkata.late_minutes == 0
    # 03:30 UTC is 23:30 the previous evening in New York -- long past 09:00.
    assert in_nyc.late_minutes > 0


def test_unknown_shift_zone_degrades_to_utc() -> None:
    """A bad timezone string must not stop someone clocking in."""
    assert day_shift(timezone="Mars/Olympus_Mons").zone is resolve_timezone(None)


def test_overnight_work_date_uses_the_shift_zone() -> None:
    """00:30 IST on a 22:00-06:00 IST shift belongs to the previous day."""
    rules = day_shift(
        start_time=time(22, 0),
        end_time=time(6, 0),
        is_overnight=True,
        working_days=(),
    )
    # 19:00 UTC on the 4th is 00:30 IST on the 5th.
    local = to_local(datetime(2026, 5, 4, 19, 0, tzinfo=UTC), IST)
    assert local.date() == date(2026, 5, 5)
    assert resolve_work_date(local, rules) == date(2026, 5, 4)

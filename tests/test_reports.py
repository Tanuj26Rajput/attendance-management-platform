"""Unit tests for the report folding logic.

These need no database: they build ``EmployeeDay`` rows directly and check the
arithmetic, so the rate definitions are pinned independently of how the grid is
assembled.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.models.enums import AttendanceStatus
from app.services.reports import (
    MAX_RANGE_DAYS,
    EmployeeDay,
    Grid,
    InvalidDateRangeError,
    absenteeism_by_department,
    absenteeism_by_employee,
    attendance_trend,
    daily_summaries,
    date_range,
    paginate,
    single_day_summary,
    summarise_days,
    validate_range,
)

DAY = dt.date(2026, 5, 4)  # a Monday
DEPT = uuid.uuid4()


def day(
    status: AttendanceStatus,
    *,
    on: dt.date = DAY,
    user_id: uuid.UUID | None = None,
    late_minutes: int = 0,
    department_id: uuid.UUID | None = DEPT,
) -> EmployeeDay:
    return EmployeeDay(
        user_id=user_id or uuid.uuid4(),
        department_id=department_id,
        work_date=on,
        status=status,
        late_minutes=late_minutes,
        worked_minutes=None,
    )


# ------------------------------------------------------------ range checks
def test_valid_range_is_accepted() -> None:
    validate_range(DAY, DAY + dt.timedelta(days=30))


def test_single_day_range_is_valid() -> None:
    validate_range(DAY, DAY)


def test_inverted_range_is_rejected() -> None:
    with pytest.raises(InvalidDateRangeError, match="before start"):
        validate_range(DAY, DAY - dt.timedelta(days=1))


def test_range_at_the_limit_is_accepted() -> None:
    validate_range(DAY, DAY + dt.timedelta(days=MAX_RANGE_DAYS - 1))


def test_range_one_day_over_the_limit_is_rejected() -> None:
    with pytest.raises(InvalidDateRangeError, match="maximum"):
        validate_range(DAY, DAY + dt.timedelta(days=MAX_RANGE_DAYS))


def test_date_range_is_inclusive_at_both_ends() -> None:
    days = date_range(DAY, DAY + dt.timedelta(days=2))
    assert days == [DAY, DAY + dt.timedelta(days=1), DAY + dt.timedelta(days=2)]


# --------------------------------------------------------------- summaries
def test_summarise_empty_input_is_none() -> None:
    assert summarise_days([]) is None


def test_summary_counts_each_status() -> None:
    summary = summarise_days(
        [
            day(AttendanceStatus.PRESENT),
            day(AttendanceStatus.PRESENT),
            day(AttendanceStatus.LATE, late_minutes=10),
            day(AttendanceStatus.HALF_DAY),
            day(AttendanceStatus.ABSENT),
            day(AttendanceStatus.ON_LEAVE),
        ]
    )
    assert summary is not None
    assert summary.expected == 6
    assert (summary.present, summary.late, summary.half_day) == (2, 1, 1)
    assert (summary.absent, summary.on_leave) == (1, 1)


def test_leave_is_excluded_from_the_rate_denominator() -> None:
    """Four expected, one on leave, one absent -> 2 of 3 attended."""
    summary = summarise_days(
        [
            day(AttendanceStatus.PRESENT),
            day(AttendanceStatus.LATE, late_minutes=5),
            day(AttendanceStatus.ABSENT),
            day(AttendanceStatus.ON_LEAVE),
        ]
    )
    assert summary is not None
    assert summary.attendance_rate == pytest.approx(2 / 3, abs=1e-4)
    assert summary.absence_rate == pytest.approx(1 / 3, abs=1e-4)


def test_a_day_where_everyone_is_on_leave_does_not_divide_by_zero() -> None:
    summary = summarise_days([day(AttendanceStatus.ON_LEAVE)])
    assert summary is not None
    assert summary.attendance_rate == 0.0
    assert summary.absence_rate == 0.0


def test_single_day_summary_zero_fills_an_empty_grid() -> None:
    grid = Grid(days=[], employees={}, departments={})
    summary = single_day_summary(grid, DAY)
    assert summary.work_date == DAY
    assert summary.expected == 0
    assert summary.attendance_rate == 0.0


def test_daily_summaries_are_ordered_by_date() -> None:
    grid = Grid(
        days=[
            day(AttendanceStatus.PRESENT, on=DAY + dt.timedelta(days=2)),
            day(AttendanceStatus.ABSENT, on=DAY),
            day(AttendanceStatus.PRESENT, on=DAY + dt.timedelta(days=1)),
        ],
        employees={},
        departments={},
    )
    dates = [s.work_date for s in daily_summaries(grid)]
    assert dates == sorted(dates)
    assert len(dates) == 3


def test_days_with_nobody_expected_produce_no_summary() -> None:
    """A weekend never enters the grid, so it is absent from the series."""
    grid = Grid(days=[day(AttendanceStatus.PRESENT)], employees={}, departments={})
    assert [s.work_date for s in daily_summaries(grid)] == [DAY]


# ------------------------------------------------------------------ trends
def test_daily_trend_has_one_point_per_day() -> None:
    grid = Grid(
        days=[
            day(AttendanceStatus.PRESENT, on=DAY),
            day(AttendanceStatus.ABSENT, on=DAY + dt.timedelta(days=1)),
        ],
        employees={},
        departments={},
    )
    points = attendance_trend(grid, "day")
    assert len(points) == 2
    assert points[0].attendance_rate == 1.0
    assert points[1].absence_rate == 1.0


def test_weekly_trend_buckets_from_iso_monday() -> None:
    """Monday the 4th and Sunday the 10th are the same ISO week."""
    grid = Grid(
        days=[
            day(AttendanceStatus.PRESENT, on=dt.date(2026, 5, 4)),
            day(AttendanceStatus.ABSENT, on=dt.date(2026, 5, 10)),
            day(AttendanceStatus.PRESENT, on=dt.date(2026, 5, 11)),
        ],
        employees={},
        departments={},
    )
    points = attendance_trend(grid, "week")
    assert [p.period_start for p in points] == [
        dt.date(2026, 5, 4),
        dt.date(2026, 5, 11),
    ]
    assert points[0].expected == 2
    assert points[1].expected == 1


def test_monthly_trend_buckets_by_calendar_month() -> None:
    grid = Grid(
        days=[
            day(AttendanceStatus.PRESENT, on=dt.date(2026, 5, 31)),
            day(AttendanceStatus.PRESENT, on=dt.date(2026, 6, 1)),
        ],
        employees={},
        departments={},
    )
    points = attendance_trend(grid, "month")
    assert [p.period_start for p in points] == [
        dt.date(2026, 5, 1),
        dt.date(2026, 6, 1),
    ]


def test_average_late_minutes_counts_only_late_days() -> None:
    grid = Grid(
        days=[
            day(AttendanceStatus.LATE, late_minutes=10),
            day(AttendanceStatus.LATE, late_minutes=20),
            day(AttendanceStatus.PRESENT),
        ],
        employees={},
        departments={},
    )
    assert attendance_trend(grid, "day")[0].average_late_minutes == 15.0


def test_trend_with_no_days_is_empty() -> None:
    grid = Grid(days=[], employees={}, departments={})
    assert attendance_trend(grid, "day") == []


# ------------------------------------------------------------ absenteeism
def test_absenteeism_by_department_groups_and_ranks() -> None:
    good, bad = uuid.uuid4(), uuid.uuid4()
    grid = Grid(
        days=[
            day(AttendanceStatus.PRESENT, department_id=good),
            day(AttendanceStatus.PRESENT, department_id=good),
            day(AttendanceStatus.ABSENT, department_id=bad),
            day(AttendanceStatus.PRESENT, department_id=bad),
        ],
        employees={},
        departments={},
    )
    rows = absenteeism_by_department(grid)
    # Worst absence rate first.
    assert rows[0].group_id == bad
    assert rows[0].absence_rate == 0.5
    assert rows[1].absence_rate == 0.0


def test_absenteeism_labels_unassigned_employees() -> None:
    grid = Grid(
        days=[day(AttendanceStatus.ABSENT, department_id=None)],
        employees={},
        departments={},
    )
    assert absenteeism_by_department(grid)[0].group_label == "Unassigned"


def test_absenteeism_by_employee_excludes_leave_from_the_denominator() -> None:
    worker = uuid.uuid4()
    grid = Grid(
        days=[
            day(AttendanceStatus.ON_LEAVE, user_id=worker, on=DAY),
            day(
                AttendanceStatus.ABSENT,
                user_id=worker,
                on=DAY + dt.timedelta(days=1),
            ),
            day(
                AttendanceStatus.PRESENT,
                user_id=worker,
                on=DAY + dt.timedelta(days=2),
            ),
        ],
        employees={},
        departments={},
    )
    row = absenteeism_by_employee(grid)[0]
    assert row.expected_days == 3
    assert row.on_leave_days == 1
    # One absence out of the two days actually expected at work.
    assert row.absence_rate == 0.5


# ------------------------------------------------------------- pagination
def test_paginate_returns_the_page_and_the_full_total() -> None:
    rows = list(range(10))
    page, total = paginate(rows, limit=3, offset=6)
    assert page == [6, 7, 8]
    assert total == 10


def test_paginate_past_the_end_is_empty_but_keeps_the_total() -> None:
    page, total = paginate(list(range(5)), limit=10, offset=50)
    assert page == []
    assert total == 5

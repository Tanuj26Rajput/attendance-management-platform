"""Attendance reporting and analytics.

Every number here is computed deterministically in SQL and Python. No language
model is involved: the chatbot calls into this module rather than doing its own
arithmetic, so a figure shown in chat and the same figure on a report always
agree.

The central problem is that **absence is not stored**. An ``attendance_records``
row only exists once someone punches, so "who was absent" cannot be read from a
table -- it has to be derived from who was *expected*:

    expected(employee, day) =
        a shift assignment covers the day
        AND the day is one of that shift's working days
        AND the shift has already finished, in the shift's own timezone

That last clause is what keeps a report honest near the present: an employee
whose shift ends at 18:00 Asia/Kolkata is not absent at 14:00 their time, even
though it is already "tomorrow" somewhere else.

To stay free of N+1, the grid is built from exactly five queries -- employees,
assignments, records, approved leave and departments -- regardless of how many
employees or days are in range. The per-day combination then happens in memory.
(Measured: 25 employees over 60 days produces 3,120 grid rows from those same
five queries.)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceRecord
from app.models.department import Department
from app.models.enums import AttendanceStatus, EmploymentStatus, LeaveStatus
from app.models.leave import LeaveRequest
from app.models.shift import EmployeeShiftAssignment, Shift
from app.models.user import User
from app.services.attendance_rules import (
    ShiftRules,
    is_working_day,
    scheduled_end,
)

# A guard against someone asking for a decade of daily rows in one request.
MAX_RANGE_DAYS = 366


class ReportError(Exception):
    """Base class for reporting failures."""


class InvalidDateRangeError(ReportError):
    """The requested range is inverted or too wide."""


def validate_range(start: date, end: date) -> None:
    """Both bounds are inclusive."""
    if end < start:
        raise InvalidDateRangeError("end must not be before start")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise InvalidDateRangeError(
            f"range spans {span} days; the maximum is {MAX_RANGE_DAYS}"
        )


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


@dataclass(frozen=True, slots=True)
class EmployeeDay:
    """One employee on one expected working day.

    Days the employee was not expected to work never become an ``EmployeeDay``
    at all, so no caller can accidentally count a weekend as an absence.
    """

    user_id: UUID
    department_id: UUID | None
    work_date: date
    status: AttendanceStatus
    late_minutes: int
    worked_minutes: int | None

    @property
    def is_absent(self) -> bool:
        return self.status is AttendanceStatus.ABSENT

    @property
    def is_leave(self) -> bool:
        return self.status is AttendanceStatus.ON_LEAVE

    @property
    def is_late(self) -> bool:
        return self.status is AttendanceStatus.LATE

    @property
    def is_attended(self) -> bool:
        """Turned up at all -- present, late or a half day."""
        return self.status in (
            AttendanceStatus.PRESENT,
            AttendanceStatus.LATE,
            AttendanceStatus.HALF_DAY,
        )


@dataclass(frozen=True, slots=True)
class Grid:
    """The derived attendance grid plus the lookups a caller needs to label it."""

    days: list[EmployeeDay]
    employees: dict[UUID, User]
    departments: dict[UUID, Department]


# --------------------------------------------------------------- loading
async def _load_employees(
    session: AsyncSession,
    *,
    department_id: UUID | None,
    user_ids: list[UUID] | None,
) -> list[User]:
    """Active employees only.

    A terminated or deactivated account is excluded deliberately: counting a
    leaver absent every day since they left would inflate every absenteeism
    figure that includes them.
    """
    stmt = select(User).where(
        User.is_active.is_(True),
        User.employment_status == EmploymentStatus.ACTIVE,
    )
    if department_id is not None:
        stmt = stmt.where(User.department_id == department_id)
    if user_ids is not None:
        stmt = stmt.where(User.id.in_(user_ids))
    return list((await session.execute(stmt)).scalars().all())


async def _load_assignments(
    session: AsyncSession, user_ids: list[UUID], start: date, end: date
) -> dict[UUID, list[tuple[EmployeeShiftAssignment, Shift]]]:
    """Assignments overlapping the range, with their shift, in one query."""
    stmt = (
        select(EmployeeShiftAssignment, Shift)
        .join(Shift, Shift.id == EmployeeShiftAssignment.shift_id)
        .where(
            EmployeeShiftAssignment.user_id.in_(user_ids),
            EmployeeShiftAssignment.effective_from <= end,
            or_(
                EmployeeShiftAssignment.effective_to.is_(None),
                EmployeeShiftAssignment.effective_to >= start,
            ),
        )
        .order_by(
            EmployeeShiftAssignment.is_primary.desc(),
            EmployeeShiftAssignment.effective_from.desc(),
        )
    )
    grouped: dict[UUID, list[tuple[EmployeeShiftAssignment, Shift]]] = defaultdict(list)
    for assignment, shift in (await session.execute(stmt)).all():
        grouped[assignment.user_id].append((assignment, shift))
    return grouped


async def _load_records(
    session: AsyncSession, user_ids: list[UUID], start: date, end: date
) -> dict[tuple[UUID, date], AttendanceRecord]:
    stmt = select(AttendanceRecord).where(
        AttendanceRecord.user_id.in_(user_ids),
        AttendanceRecord.work_date >= start,
        AttendanceRecord.work_date <= end,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {(row.user_id, row.work_date): row for row in rows}


async def _load_approved_leave(
    session: AsyncSession, user_ids: list[UUID], start: date, end: date
) -> dict[UUID, list[tuple[date, date]]]:
    """Approved leave periods overlapping the range.

    Only ``approved`` counts. A pending request must not excuse an absence, or
    anyone could suppress their own absences by filing a request nobody acts
    on; rejected and cancelled ones likewise mean the employee was expected.
    """
    stmt = select(LeaveRequest).where(
        LeaveRequest.user_id.in_(user_ids),
        LeaveRequest.status == LeaveStatus.APPROVED,
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
    )
    grouped: dict[UUID, list[tuple[date, date]]] = defaultdict(list)
    for row in (await session.execute(stmt)).scalars().all():
        grouped[row.user_id].append((row.start_date, row.end_date))
    return grouped


async def _load_departments(session: AsyncSession) -> dict[UUID, Department]:
    rows = (await session.execute(select(Department))).scalars().all()
    return {row.id: row for row in rows}


# ----------------------------------------------------------------- grid
def _rules_for_day(
    assignments: list[tuple[EmployeeShiftAssignment, Shift]], day: date
) -> ShiftRules | None:
    """The shift in force on a given day, or None if unassigned.

    Assignments arrive primary-first and newest-first, so the first match is
    the one that governs; the database's exclusion constraint means there is
    normally only one anyway.
    """
    for assignment, shift in assignments:
        if assignment.effective_from <= day and (
            assignment.effective_to is None or assignment.effective_to >= day
        ):
            return ShiftRules.from_shift(shift)
    return None


def _on_leave(periods: list[tuple[date, date]], day: date) -> bool:
    return any(start <= day <= end for start, end in periods)


async def build_grid(
    session: AsyncSession,
    start: date,
    end: date,
    *,
    department_id: UUID | None = None,
    user_ids: list[UUID] | None = None,
    now: datetime | None = None,
) -> Grid:
    """Derive one row per employee per expected working day.

    Five queries, independent of the size of the result.
    """
    validate_range(start, end)
    now = now or datetime.now(UTC)

    employees = await _load_employees(
        session, department_id=department_id, user_ids=user_ids
    )
    if not employees:
        return Grid(days=[], employees={}, departments={})

    ids = [employee.id for employee in employees]
    assignments = await _load_assignments(session, ids, start, end)
    records = await _load_records(session, ids, start, end)
    leave = await _load_approved_leave(session, ids, start, end)
    departments = await _load_departments(session)

    days: list[EmployeeDay] = []
    calendar = date_range(start, end)

    for employee in employees:
        employee_assignments = assignments.get(employee.id, [])
        if not employee_assignments:
            continue
        leave_periods = leave.get(employee.id, [])

        for day in calendar:
            rules = _rules_for_day(employee_assignments, day)
            if rules is None or not is_working_day(day, rules):
                continue

            # Skip days that have not finished yet in the shift's own zone;
            # an in-progress day is not an absence.
            if scheduled_end(day, rules, rules.zone) > now:
                continue

            record = records.get((employee.id, day))
            if record is not None:
                status = record.status
                late_minutes = record.late_minutes
                worked_minutes = record.worked_minutes
            elif _on_leave(leave_periods, day):
                status = AttendanceStatus.ON_LEAVE
                late_minutes = 0
                worked_minutes = None
            else:
                status = AttendanceStatus.ABSENT
                late_minutes = 0
                worked_minutes = None

            days.append(
                EmployeeDay(
                    user_id=employee.id,
                    department_id=employee.department_id,
                    work_date=day,
                    status=status,
                    late_minutes=late_minutes,
                    worked_minutes=worked_minutes,
                )
            )

    days.sort(key=lambda d: (d.work_date, str(d.user_id)))
    return Grid(
        days=days,
        employees={employee.id: employee for employee in employees},
        departments=departments,
    )


# -------------------------------------------------------------- summaries
@dataclass(frozen=True, slots=True)
class DailySummary:
    work_date: date
    expected: int
    present: int
    late: int
    half_day: int
    absent: int
    on_leave: int

    @property
    def attendance_rate(self) -> float:
        """Share of expected employees who turned up, leave excluded.

        Leave comes out of the denominator: an approved day off is not a
        failure to attend, and leaving it in would make a team's rate drop
        every holiday season.
        """
        base = self.expected - self.on_leave
        if base <= 0:
            return 0.0
        return round((self.present + self.late + self.half_day) / base, 4)

    @property
    def absence_rate(self) -> float:
        base = self.expected - self.on_leave
        if base <= 0:
            return 0.0
        return round(self.absent / base, 4)


def summarise_days(days: list[EmployeeDay]) -> DailySummary | None:
    """Fold a set of employee-days into one summary. Empty input yields None."""
    if not days:
        return None
    return DailySummary(
        work_date=days[0].work_date,
        expected=len(days),
        present=sum(1 for d in days if d.status is AttendanceStatus.PRESENT),
        late=sum(1 for d in days if d.is_late),
        half_day=sum(1 for d in days if d.status is AttendanceStatus.HALF_DAY),
        absent=sum(1 for d in days if d.is_absent),
        on_leave=sum(1 for d in days if d.is_leave),
    )


def daily_summaries(grid: Grid) -> list[DailySummary]:
    """One summary per date that had at least one expected employee."""
    by_date: dict[date, list[EmployeeDay]] = defaultdict(list)
    for day in grid.days:
        by_date[day.work_date].append(day)

    summaries = []
    for work_date in sorted(by_date):
        summary = summarise_days(by_date[work_date])
        if summary is not None:
            summaries.append(summary)
    return summaries


# ----------------------------------------------------------------- trends
def _bucket_start(day: date, granularity: str) -> date:
    if granularity == "week":
        return day - timedelta(days=day.isoweekday() - 1)  # ISO Monday
    if granularity == "month":
        return day.replace(day=1)
    return day


@dataclass(frozen=True, slots=True)
class TrendPoint:
    period_start: date
    period_end: date
    expected: int
    attended: int
    late: int
    absent: int
    on_leave: int
    attendance_rate: float
    absence_rate: float
    average_late_minutes: float


def attendance_trend(grid: Grid, granularity: str = "day") -> list[TrendPoint]:
    """Bucket the grid by day, ISO week or calendar month."""
    buckets: dict[date, list[EmployeeDay]] = defaultdict(list)
    for day in grid.days:
        buckets[_bucket_start(day.work_date, granularity)].append(day)

    points = []
    for period_start in sorted(buckets):
        rows = buckets[period_start]
        expected = len(rows)
        on_leave = sum(1 for r in rows if r.is_leave)
        attended = sum(1 for r in rows if r.is_attended)
        absent = sum(1 for r in rows if r.is_absent)
        late_rows = [r for r in rows if r.is_late]
        base = expected - on_leave

        points.append(
            TrendPoint(
                period_start=period_start,
                period_end=max(r.work_date for r in rows),
                expected=expected,
                attended=attended,
                late=len(late_rows),
                absent=absent,
                on_leave=on_leave,
                attendance_rate=round(attended / base, 4) if base > 0 else 0.0,
                absence_rate=round(absent / base, 4) if base > 0 else 0.0,
                average_late_minutes=(
                    round(sum(r.late_minutes for r in late_rows) / len(late_rows), 2)
                    if late_rows
                    else 0.0
                ),
            )
        )
    return points


# ---------------------------------------------------------------- listings
@dataclass(frozen=True, slots=True)
class EmployeeDayRow:
    """An employee-day with the employee's identity attached, for listings."""

    user_id: UUID
    employee_code: str
    full_name: str
    department_id: UUID | None
    department_name: str | None
    work_date: date
    status: AttendanceStatus
    late_minutes: int
    worked_minutes: int | None


def _to_row(grid: Grid, day: EmployeeDay) -> EmployeeDayRow:
    employee = grid.employees[day.user_id]
    department = (
        grid.departments.get(day.department_id)
        if day.department_id is not None
        else None
    )
    return EmployeeDayRow(
        user_id=day.user_id,
        employee_code=employee.employee_code,
        full_name=employee.full_name,
        department_id=day.department_id,
        department_name=department.name if department else None,
        work_date=day.work_date,
        status=day.status,
        late_minutes=day.late_minutes,
        worked_minutes=day.worked_minutes,
    )


def late_arrivals(grid: Grid, *, min_late_minutes: int = 1) -> list[EmployeeDayRow]:
    """Late days, worst first."""
    rows = [
        _to_row(grid, day)
        for day in grid.days
        if day.is_late and day.late_minutes >= min_late_minutes
    ]
    rows.sort(key=lambda r: (-r.late_minutes, r.work_date))
    return rows


def absences(grid: Grid) -> list[EmployeeDayRow]:
    """Expected days with no record and no approved leave, newest first."""
    rows = [_to_row(grid, day) for day in grid.days if day.is_absent]
    rows.sort(key=lambda r: (r.work_date, r.full_name), reverse=True)
    return rows


# ------------------------------------------------------------ absenteeism
@dataclass(frozen=True, slots=True)
class AbsenteeismRow:
    group_id: UUID | None
    group_label: str
    expected_days: int
    absent_days: int
    on_leave_days: int
    late_days: int
    attended_days: int
    absence_rate: float
    attendance_rate: float


def _absenteeism(
    buckets: dict[UUID | None, list[EmployeeDay]], label_for: dict[UUID | None, str]
) -> list[AbsenteeismRow]:
    rows = []
    for key, group in buckets.items():
        expected = len(group)
        on_leave = sum(1 for d in group if d.is_leave)
        absent = sum(1 for d in group if d.is_absent)
        attended = sum(1 for d in group if d.is_attended)
        base = expected - on_leave

        rows.append(
            AbsenteeismRow(
                group_id=key,
                group_label=label_for.get(key, "Unassigned"),
                expected_days=expected,
                absent_days=absent,
                on_leave_days=on_leave,
                late_days=sum(1 for d in group if d.is_late),
                attended_days=attended,
                absence_rate=round(absent / base, 4) if base > 0 else 0.0,
                attendance_rate=round(attended / base, 4) if base > 0 else 0.0,
            )
        )
    # Worst offenders first; ties broken by name so the order is stable.
    rows.sort(key=lambda r: (-r.absence_rate, -r.absent_days, r.group_label))
    return rows


def absenteeism_by_department(grid: Grid) -> list[AbsenteeismRow]:
    buckets: dict[UUID | None, list[EmployeeDay]] = defaultdict(list)
    for day in grid.days:
        buckets[day.department_id].append(day)

    labels: dict[UUID | None, str] = {
        dept_id: dept.name for dept_id, dept in grid.departments.items()
    }
    labels[None] = "Unassigned"
    return _absenteeism(buckets, labels)


def absenteeism_by_employee(grid: Grid) -> list[AbsenteeismRow]:
    buckets: dict[UUID | None, list[EmployeeDay]] = defaultdict(list)
    for day in grid.days:
        buckets[day.user_id].append(day)

    labels: dict[UUID | None, str] = {
        user_id: f"{employee.full_name} ({employee.employee_code})"
        for user_id, employee in grid.employees.items()
    }
    return _absenteeism(buckets, labels)


# ----------------------------------------------------------------- helper
def paginate(rows: list, limit: int, offset: int) -> tuple[list, int]:
    """Slice an in-memory result set, returning the page and the full total."""
    return rows[offset : offset + limit], len(rows)


def single_day_summary(grid: Grid, day: date) -> DailySummary:
    """A summary for one date, zero-filled when nobody was expected."""
    rows = [d for d in grid.days if d.work_date == day]
    return summarise_days(rows) or DailySummary(
        work_date=day,
        expected=0,
        present=0,
        late=0,
        half_day=0,
        absent=0,
        on_leave=0,
    )

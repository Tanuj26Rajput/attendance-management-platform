"""Pure attendance rules: no database, no I/O, fully unit-testable.

Everything is stored in UTC. Shift times (``start_time``/``end_time``) are
naive wall-clock times read in the shift's own ``timezone`` -- a shift is a
property of the workplace, not of whoever is rostered onto it, so two employees
on the same shift are judged late at the same instant regardless of where they
personally live. ``User.timezone`` is only a fallback for employees with no
shift assigned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.logging import get_logger
from app.models.enums import AttendanceStatus

logger = get_logger(__name__)

UTC = ZoneInfo("UTC")


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Return the named zone, falling back to UTC rather than failing a punch.

    A bad timezone string is a data problem, not a reason to stop someone
    clocking in, so it degrades to UTC and is logged.
    """
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r; falling back to UTC", name)
        return UTC


@dataclass(frozen=True, slots=True)
class ShiftRules:
    """The subset of a Shift the rules need, decoupled from the ORM."""

    start_time: time
    end_time: time
    is_overnight: bool
    break_minutes: int
    grace_period_minutes: int
    half_day_threshold_minutes: int
    working_days: tuple[int, ...]
    timezone: str = "UTC"

    @property
    def zone(self) -> ZoneInfo:
        """The zone the shift's wall-clock times are read in."""
        return resolve_timezone(self.timezone)

    @classmethod
    def from_shift(cls, shift) -> ShiftRules:
        return cls(
            start_time=shift.start_time,
            end_time=shift.end_time,
            is_overnight=shift.is_overnight,
            break_minutes=shift.break_minutes,
            grace_period_minutes=shift.grace_period_minutes,
            half_day_threshold_minutes=shift.half_day_threshold_minutes,
            working_days=tuple(shift.working_days or ()),
            timezone=shift.timezone,
        )


@dataclass(frozen=True, slots=True)
class PunchInOutcome:
    work_date: date
    status: AttendanceStatus
    late_minutes: int


@dataclass(frozen=True, slots=True)
class PunchOutOutcome:
    status: AttendanceStatus
    worked_minutes: int
    early_leave_minutes: int
    overtime_minutes: int


def to_local(instant: datetime, tz: ZoneInfo) -> datetime:
    """Convert a stored UTC instant into the employee's local wall clock."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(tz)


def scheduled_start(work_date: date, rules: ShiftRules, tz: ZoneInfo) -> datetime:
    return datetime.combine(work_date, rules.start_time, tzinfo=tz)


def scheduled_end(work_date: date, rules: ShiftRules, tz: ZoneInfo) -> datetime:
    """The shift's end instant.

    An overnight shift ends on the following calendar day, which is why
    ``is_overnight`` is a stored flag rather than inferred from
    ``end_time < start_time`` -- a shift running 09:00-09:00 is ambiguous
    otherwise.
    """
    end = datetime.combine(work_date, rules.end_time, tzinfo=tz)
    if rules.is_overnight:
        end += timedelta(days=1)
    return end


def resolve_work_date(local_punch_in: datetime, rules: ShiftRules) -> date:
    """Which business day a punch belongs to.

    For an overnight shift, clocking in just after midnight belongs to the
    previous business day: someone starting a 22:00-06:00 shift who arrives at
    00:30 is late for *yesterday's* shift, not early for today's.
    """
    if rules.is_overnight and local_punch_in.time() < rules.start_time:
        return local_punch_in.date() - timedelta(days=1)
    return local_punch_in.date()


def is_working_day(work_date: date, rules: ShiftRules) -> bool:
    """``working_days`` holds ISO weekday numbers: 1 = Monday ... 7 = Sunday."""
    if not rules.working_days:
        return True
    return work_date.isoweekday() in rules.working_days


def evaluate_punch_in(
    punched_at_utc: datetime, rules: ShiftRules, tz: ZoneInfo
) -> PunchInOutcome:
    local = to_local(punched_at_utc, tz)
    work_date = resolve_work_date(local, rules)
    start = scheduled_start(work_date, rules, tz)

    # Lateness is measured from the end of the grace period, so a 15-minute
    # grace means 09:15 is on time and 09:16 is one minute late.
    allowed = start + timedelta(minutes=rules.grace_period_minutes)
    late_minutes = max(0, int((local - allowed).total_seconds() // 60))

    status = AttendanceStatus.LATE if late_minutes > 0 else AttendanceStatus.PRESENT
    return PunchInOutcome(work_date=work_date, status=status, late_minutes=late_minutes)


def evaluate_punch_out(
    checked_in_utc: datetime,
    punched_at_utc: datetime,
    work_date: date,
    rules: ShiftRules,
    tz: ZoneInfo,
    late_minutes: int,
) -> PunchOutOutcome:
    local_out = to_local(punched_at_utc, tz)

    gross = int((punched_at_utc - checked_in_utc).total_seconds() // 60)
    worked_minutes = max(0, gross - rules.break_minutes)

    end = scheduled_end(work_date, rules, tz)
    early_leave_minutes = max(0, int((end - local_out).total_seconds() // 60))
    overtime_minutes = max(0, int((local_out - end).total_seconds() // 60))

    if worked_minutes < rules.half_day_threshold_minutes:
        status = AttendanceStatus.HALF_DAY
    elif late_minutes > 0:
        status = AttendanceStatus.LATE
    else:
        status = AttendanceStatus.PRESENT

    return PunchOutOutcome(
        status=status,
        worked_minutes=worked_minutes,
        early_leave_minutes=early_leave_minutes,
        overtime_minutes=overtime_minutes,
    )

"""Attendance workflow orchestration.

All instants are persisted in UTC; conversion to the employee's local time
happens only inside ``attendance_rules`` when deciding lateness and boundaries.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceRecord
from app.models.audit import AuditLog
from app.models.enums import AttendanceSource, AuditAction
from app.models.shift import EmployeeShiftAssignment, Shift
from app.models.user import User
from app.services.attendance_rules import (
    ShiftRules,
    evaluate_punch_in,
    evaluate_punch_out,
    is_working_day,
    resolve_timezone,
    resolve_work_date,
    to_local,
)
from app.services.audit import build_audit


class AttendanceError(Exception):
    """Base class for attendance workflow failures."""


class NoActiveShiftError(AttendanceError):
    """The employee has no shift assigned covering the date."""


class NotAWorkingDayError(AttendanceError):
    """The assigned shift does not run on this weekday."""


class AlreadyPunchedInError(AttendanceError):
    """An open punch already exists for the business day."""


class AlreadyPunchedOutError(AttendanceError):
    """The day's record is already closed."""


class NotPunchedInError(AttendanceError):
    """Punch-out attempted with no open punch-in."""


async def get_active_assignment(
    session: AsyncSession, user_id: UUID, on_date: date
) -> EmployeeShiftAssignment | None:
    """The shift assignment covering ``on_date``, preferring the primary one."""
    stmt = (
        select(EmployeeShiftAssignment)
        .join(Shift, Shift.id == EmployeeShiftAssignment.shift_id)
        .where(
            EmployeeShiftAssignment.user_id == user_id,
            EmployeeShiftAssignment.effective_from <= on_date,
            or_(
                EmployeeShiftAssignment.effective_to.is_(None),
                EmployeeShiftAssignment.effective_to >= on_date,
            ),
            Shift.is_active.is_(True),
        )
        .order_by(
            EmployeeShiftAssignment.is_primary.desc(),
            EmployeeShiftAssignment.effective_from.desc(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_shift(session: AsyncSession, shift_id: UUID) -> Shift:
    shift = await session.get(Shift, shift_id)
    if shift is None:  # pragma: no cover - FK guarantees it exists
        raise NoActiveShiftError("assigned shift is missing")
    return shift


def _audit(
    actor: User,
    action: AuditAction,
    record: AttendanceRecord,
    summary: str,
    ip: str | None,
) -> AuditLog:
    """An audit row describing a punch, staged by the caller."""
    return build_audit(
        actor_id=actor.id,
        action=action,
        entity_type="attendance_record",
        entity_id=record.id,
        summary=summary,
        ip=ip,
        changes={
            "work_date": record.work_date.isoformat(),
            "status": record.status.value,
            "check_in_at": record.check_in_at.isoformat()
            if record.check_in_at
            else None,
            "check_out_at": record.check_out_at.isoformat()
            if record.check_out_at
            else None,
        },
    )


async def punch_in(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    source: AttendanceSource = AttendanceSource.WEB,
    ip: str | None = None,
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
    notes: str | None = None,
) -> AttendanceRecord:
    """Open the day's attendance record.

    Duplicate punches are prevented twice over: by an explicit check, and by
    the ``uq_attendance_records_user_work_date`` constraint, which is what
    actually makes it safe under concurrency -- two simultaneous requests both
    pass the check, then one loses the insert race and is reported as a
    duplicate rather than silently creating a second row.
    """
    now = now or datetime.now(UTC)

    # The employee's own timezone only picks which calendar date to look the
    # assignment up by; the shift's own timezone is authoritative for every
    # rule evaluated below.
    lookup_tz = resolve_timezone(user.timezone)
    assignment = await get_active_assignment(
        session, user.id, to_local(now, lookup_tz).date()
    )
    if assignment is None:
        raise NoActiveShiftError("no active shift assignment for today")

    shift = await _load_shift(session, assignment.shift_id)
    rules = ShiftRules.from_shift(shift)
    tz = rules.zone
    local_now = to_local(now, tz)

    work_date = resolve_work_date(local_now, rules)
    if not is_working_day(work_date, rules):
        raise NotAWorkingDayError(f"{work_date} is not a working day for this shift")

    existing = (
        await session.execute(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.user_id == user.id,
                AttendanceRecord.work_date == work_date,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.check_out_at is None and existing.check_in_at is not None:
            raise AlreadyPunchedInError("already punched in for this day")
        raise AlreadyPunchedOutError("attendance for this day is already complete")

    outcome = evaluate_punch_in(now, rules, tz)
    record = AttendanceRecord(
        user_id=user.id,
        work_date=outcome.work_date,
        shift_id=shift.id,
        check_in_at=now,
        status=outcome.status,
        source=source,
        late_minutes=outcome.late_minutes,
        check_in_ip=ip,
        check_in_latitude=latitude,
        check_in_longitude=longitude,
        notes=notes,
    )
    session.add(record)

    try:
        await session.flush()
    except IntegrityError as exc:
        # Lost the race against a concurrent punch-in for the same day.
        await session.rollback()
        raise AlreadyPunchedInError("already punched in for this day") from exc

    session.add(_audit(user, AuditAction.CREATE, record, "Punched in", ip))
    await session.commit()
    await session.refresh(record)
    return record


async def punch_out(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    ip: str | None = None,
    notes: str | None = None,
) -> AttendanceRecord:
    """Close the open attendance record.

    The row is locked FOR UPDATE so two concurrent punch-outs serialise and the
    second sees the record already closed.
    """
    now = now or datetime.now(UTC)

    # An overnight shift is punched out on the following calendar day, so the
    # open record is found purely by "still open" -- no date bound, which would
    # only reintroduce the question of whose timezone defines "today". Only
    # punch_in writes work_date, and it never writes a future one.
    record = (
        await session.execute(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.user_id == user.id,
                AttendanceRecord.check_in_at.is_not(None),
                AttendanceRecord.check_out_at.is_(None),
            )
            .order_by(AttendanceRecord.work_date.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if record is None:
        # "No open punch-in" is true but unhelpful to someone who just punched
        # out -- they did punch in, and telling them otherwise reads like the
        # punch was lost. Distinguish the two by looking for a completed day,
        # which is also what a double-click or a retried request produces.
        completed = (
            await session.execute(
                select(AttendanceRecord)
                .where(
                    AttendanceRecord.user_id == user.id,
                    AttendanceRecord.check_out_at.is_not(None),
                )
                .order_by(AttendanceRecord.work_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if completed is not None:
            raise AlreadyPunchedOutError(
                f"attendance for {completed.work_date} is already complete"
            )
        raise NotPunchedInError("no open punch-in to close")

    # The query filters on check_in_at IS NOT NULL; this narrows the type and
    # also guards against a row written by some future manual-entry path.
    if record.check_in_at is None:  # pragma: no cover - unreachable via the query
        raise NotPunchedInError("open record has no check-in time")

    shift = (
        await _load_shift(session, record.shift_id)
        if record.shift_id is not None
        else None
    )
    if shift is None:
        raise NoActiveShiftError("record has no shift to evaluate against")

    # Evaluated against the shift that was recorded on the row, so editing or
    # reassigning a shift afterwards cannot retroactively change how an already
    # closed day was judged.
    rules = ShiftRules.from_shift(shift)
    outcome = evaluate_punch_out(
        checked_in_utc=record.check_in_at,
        punched_at_utc=now,
        work_date=record.work_date,
        rules=rules,
        tz=rules.zone,
        late_minutes=record.late_minutes,
    )

    record.check_out_at = now
    record.check_out_ip = ip
    record.status = outcome.status
    record.worked_minutes = outcome.worked_minutes
    record.early_leave_minutes = outcome.early_leave_minutes
    record.overtime_minutes = outcome.overtime_minutes
    if notes:
        record.notes = f"{record.notes}\n{notes}" if record.notes else notes

    await session.flush()
    session.add(_audit(user, AuditAction.UPDATE, record, "Punched out", ip))
    await session.commit()
    await session.refresh(record)
    return record


def _own_records(user_id: UUID) -> Select:
    """Every read is scoped to one user; callers cannot widen it."""
    return select(AttendanceRecord).where(AttendanceRecord.user_id == user_id)


async def get_today(
    session: AsyncSession, user: User, now: datetime | None = None
) -> AttendanceRecord | None:
    """The record for the business day currently in progress.

    "Today" is the shift's business day, not the calendar day, so during the
    post-midnight tail of an overnight shift this returns the record that is
    actually open rather than nothing.
    """
    now = now or datetime.now(UTC)
    lookup_tz = resolve_timezone(user.timezone)
    local_today = to_local(now, lookup_tz).date()

    assignment = await get_active_assignment(session, user.id, local_today)
    if assignment is not None:
        rules = ShiftRules.from_shift(await _load_shift(session, assignment.shift_id))
        local_today = resolve_work_date(to_local(now, rules.zone), rules)

    stmt = (
        _own_records(user.id).where(AttendanceRecord.work_date == local_today).limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_history(
    session: AsyncSession,
    user: User,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AttendanceRecord], int]:
    """Return ``(records, total)`` for the authenticated user only."""
    from sqlalchemy import func

    conditions = [AttendanceRecord.user_id == user.id]
    if start is not None:
        conditions.append(AttendanceRecord.work_date >= start)
    if end is not None:
        conditions.append(AttendanceRecord.work_date <= end)

    where = and_(*conditions)

    total = await session.scalar(
        select(func.count()).select_from(AttendanceRecord).where(where)
    )
    rows = await session.execute(
        select(AttendanceRecord)
        .where(where)
        .order_by(AttendanceRecord.work_date.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all()), int(total or 0)

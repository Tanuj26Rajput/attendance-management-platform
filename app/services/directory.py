"""Admin management of departments, shifts and shift assignments.

Nothing here deletes. Departments and shifts are deactivated, and assignments
are closed with an end date, because attendance history points at all three:
an ``attendance_records`` row stores the ``shift_id`` it was judged against, so
a shift that has been worked must remain resolvable forever.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department
from app.models.enums import AuditAction
from app.models.shift import EmployeeShiftAssignment, Shift
from app.models.user import User
from app.schemas.admin import validate_shift_window
from app.services.audit import record_audit

# Stands in for an open-ended assignment in Python-side comparisons; the
# database uses 'infinity' in the exclusion constraint.
_OPEN_ENDED = date(9999, 12, 31)

_T = TypeVar("_T", Department, Shift, EmployeeShiftAssignment, User)


class DirectoryError(Exception):
    """Base class for directory management failures."""


class NotFoundError(DirectoryError):
    """The referenced row does not exist."""


class DuplicateError(DirectoryError):
    """A unique name or code is already taken."""


class InvalidShiftWindowError(DirectoryError):
    """start/end/is_overnight do not describe a coherent window."""


class AssignmentConflictError(DirectoryError):
    """The employee already has an assignment covering part of this period."""


class InvalidAssignmentError(DirectoryError):
    """The assignment references something unusable."""


def _contains(search: str) -> str:
    """Build a LIKE pattern that matches the text the user actually typed.

    ``%`` and ``_`` are wildcards to LIKE, so an unescaped search for ``_``
    matches every single-character name and a search for ``%`` matches
    everything. Escaping them makes the search box mean "contains this text",
    which is what someone typing in it expects. The backslash goes first, or
    it would escape the escapes added after it.

    Callers must pass ``escape="\\\\"`` to the matching ``ilike``.
    """
    escaped = (
        search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


async def _paginate(
    session: AsyncSession, stmt: Select, *, limit: int, offset: int
) -> tuple[list[Any], int]:
    """Run a count and a page off the same filters.

    The count reuses the statement's WHERE clause via a subquery rather than
    being rebuilt by hand, so the two can never drift apart.
    """
    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = await session.execute(stmt.limit(limit).offset(offset))
    return list(rows.scalars().all()), int(total or 0)


async def _get_or_404(session: AsyncSession, model: type[_T], row_id: UUID) -> _T:
    row = await session.get(model, row_id)
    if row is None:
        raise NotFoundError(f"{model.__tablename__} {row_id} not found")
    return row


def _snapshot(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """A JSON-safe view of the fields an audit row cares about."""

    def _plain(value: Any) -> Any:
        if isinstance(value, UUID | date):
            return str(value)
        return value

    return {f: _plain(getattr(row, f)) for f in fields}


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Only the fields that actually changed, for the audit row."""
    changed = {k: v for k, v in after.items() if before.get(k) != v}
    return {"before": {k: before.get(k) for k in changed}, "after": changed}


# ------------------------------------------------------------- departments
_DEPARTMENT_FIELDS = ("name", "code", "description", "head_id", "is_active")


async def create_department(
    session: AsyncSession, actor: User, data: dict[str, Any], *, ip: str | None = None
) -> Department:
    if data.get("head_id") is not None:
        await _get_or_404(session, User, data["head_id"])

    department = Department(**data)
    session.add(department)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateError(
            "a department with that name or code already exists"
        ) from exc

    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.CREATE,
        entity_type="department",
        entity_id=department.id,
        summary=f"Created department {department.code}",
        changes={"after": _snapshot(department, _DEPARTMENT_FIELDS)},
        ip=ip,
    )
    await session.commit()
    await session.refresh(department)
    return department


async def list_departments(
    session: AsyncSession,
    *,
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Department], int]:
    stmt = select(Department)
    if is_active is not None:
        stmt = stmt.where(Department.is_active.is_(is_active))
    if search:
        pattern = _contains(search)
        stmt = stmt.where(
            or_(
                Department.name.ilike(pattern, escape="\\"),
                Department.code.ilike(pattern, escape="\\"),
            )
        )
    return await _paginate(
        session, stmt.order_by(Department.name), limit=limit, offset=offset
    )


async def update_department(
    session: AsyncSession,
    actor: User,
    department_id: UUID,
    changes: dict[str, Any],
    *,
    ip: str | None = None,
) -> Department:
    department = await _get_or_404(session, Department, department_id)
    if changes.get("head_id") is not None:
        await _get_or_404(session, User, changes["head_id"])

    before = _snapshot(department, _DEPARTMENT_FIELDS)
    for key, value in changes.items():
        setattr(department, key, value)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateError(
            "a department with that name or code already exists"
        ) from exc

    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.UPDATE,
        entity_type="department",
        entity_id=department.id,
        summary=f"Updated department {department.code}",
        changes=_diff(before, _snapshot(department, _DEPARTMENT_FIELDS)),
        ip=ip,
    )
    await session.commit()
    await session.refresh(department)
    return department


async def deactivate_department(
    session: AsyncSession, actor: User, department_id: UUID, *, ip: str | None = None
) -> Department:
    """Deactivate, never delete: employees keep pointing at the row."""
    return await update_department(
        session, actor, department_id, {"is_active": False}, ip=ip
    )


# ------------------------------------------------------------------ shifts
_SHIFT_FIELDS = (
    "name",
    "code",
    "description",
    "timezone",
    "is_overnight",
    "break_minutes",
    "grace_period_minutes",
    "half_day_threshold_minutes",
    "working_days",
    "is_active",
)


async def create_shift(
    session: AsyncSession, actor: User, data: dict[str, Any], *, ip: str | None = None
) -> Shift:
    shift = Shift(**data)
    session.add(shift)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateError("a shift with that name or code already exists") from exc

    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.CREATE,
        entity_type="shift",
        entity_id=shift.id,
        summary=f"Created shift {shift.code}",
        changes={"after": _snapshot(shift, _SHIFT_FIELDS)},
        ip=ip,
    )
    await session.commit()
    await session.refresh(shift)
    return shift


async def list_shifts(
    session: AsyncSession,
    *,
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Shift], int]:
    stmt = select(Shift)
    if is_active is not None:
        stmt = stmt.where(Shift.is_active.is_(is_active))
    if search:
        pattern = _contains(search)
        stmt = stmt.where(
            or_(
                Shift.name.ilike(pattern, escape="\\"),
                Shift.code.ilike(pattern, escape="\\"),
            )
        )
    return await _paginate(
        session, stmt.order_by(Shift.name), limit=limit, offset=offset
    )


async def update_shift(
    session: AsyncSession,
    actor: User,
    shift_id: UUID,
    changes: dict[str, Any],
    *,
    ip: str | None = None,
) -> Shift:
    """Edit a shift in place.

    Already-recorded attendance is unaffected: a record stores the status it
    was given at punch time, and ``punch_out`` evaluates against the shift on
    the record, so widening a grace period today never retroactively un-lates
    anyone.
    """
    shift = await _get_or_404(session, Shift, shift_id)

    # Validate the merged window, not the patch: moving end_time alone can
    # still produce an incoherent shift.
    try:
        validate_shift_window(
            changes.get("start_time", shift.start_time),
            changes.get("end_time", shift.end_time),
            changes.get("is_overnight", shift.is_overnight),
        )
    except ValueError as exc:
        raise InvalidShiftWindowError(str(exc)) from exc

    before = _snapshot(shift, _SHIFT_FIELDS)
    for key, value in changes.items():
        setattr(shift, key, value)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateError("a shift with that name or code already exists") from exc

    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.UPDATE,
        entity_type="shift",
        entity_id=shift.id,
        summary=f"Updated shift {shift.code}",
        changes=_diff(before, _snapshot(shift, _SHIFT_FIELDS)),
        ip=ip,
    )
    await session.commit()
    await session.refresh(shift)
    return shift


async def deactivate_shift(
    session: AsyncSession, actor: User, shift_id: UUID, *, ip: str | None = None
) -> Shift:
    """Retire a shift.

    Existing assignments are left in place deliberately.
    ``get_active_assignment`` skips inactive shifts, so nobody can punch
    against a retired shift, while past records stay attributable to it.
    """
    return await update_shift(session, actor, shift_id, {"is_active": False}, ip=ip)


# ------------------------------------------------------------- assignments
_ASSIGNMENT_FIELDS = (
    "user_id",
    "shift_id",
    "effective_from",
    "effective_to",
    "is_primary",
)


async def find_overlapping_assignment(
    session: AsyncSession,
    user_id: UUID,
    effective_from: date,
    effective_to: date | None,
    *,
    exclude_id: UUID | None = None,
) -> EmployeeShiftAssignment | None:
    """An existing assignment whose period intersects the given one.

    Two closed ranges overlap when each starts on or before the other ends. A
    NULL end date means open-ended, hence the ``_OPEN_ENDED`` stand-in.
    """
    stmt = select(EmployeeShiftAssignment).where(
        EmployeeShiftAssignment.user_id == user_id,
        EmployeeShiftAssignment.effective_from <= (effective_to or _OPEN_ENDED),
        or_(
            EmployeeShiftAssignment.effective_to.is_(None),
            EmployeeShiftAssignment.effective_to >= effective_from,
        ),
    )
    if exclude_id is not None:
        stmt = stmt.where(EmployeeShiftAssignment.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def create_assignment(
    session: AsyncSession, actor: User, data: dict[str, Any], *, ip: str | None = None
) -> EmployeeShiftAssignment:
    """Roster an employee onto a shift for a period.

    Overlap is rejected twice over, as with punches: an explicit check for a
    useful error message, and the ``ex_employee_shift_assignments_no_overlap``
    gist exclusion constraint, which is what actually holds under concurrency.
    """
    user = await _get_or_404(session, User, data["user_id"])
    shift = await _get_or_404(session, Shift, data["shift_id"])

    if not shift.is_active:
        raise InvalidAssignmentError("cannot assign employees to an inactive shift")
    if not user.is_active:
        raise InvalidAssignmentError("cannot assign a shift to an inactive user")

    clash = await find_overlapping_assignment(
        session, data["user_id"], data["effective_from"], data.get("effective_to")
    )
    if clash is not None:
        raise AssignmentConflictError(
            "the employee already has a shift assignment covering that period"
        )

    assignment = EmployeeShiftAssignment(**data)
    session.add(assignment)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise AssignmentConflictError(
            "the employee already has a shift assignment covering that period"
        ) from exc

    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.CREATE,
        entity_type="employee_shift_assignment",
        entity_id=assignment.id,
        summary=f"Assigned shift {shift.code} to employee {user.employee_code}",
        changes={"after": _snapshot(assignment, _ASSIGNMENT_FIELDS)},
        ip=ip,
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


async def list_assignments(
    session: AsyncSession,
    *,
    user_id: UUID | None = None,
    shift_id: UUID | None = None,
    active_on: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[EmployeeShiftAssignment], int]:
    """List assignments, newest period first.

    ``active_on`` filters to the assignments in force on a given date, which is
    how the history of who worked what is read back.
    """
    stmt = select(EmployeeShiftAssignment)
    if user_id is not None:
        stmt = stmt.where(EmployeeShiftAssignment.user_id == user_id)
    if shift_id is not None:
        stmt = stmt.where(EmployeeShiftAssignment.shift_id == shift_id)
    if active_on is not None:
        stmt = stmt.where(
            EmployeeShiftAssignment.effective_from <= active_on,
            or_(
                EmployeeShiftAssignment.effective_to.is_(None),
                EmployeeShiftAssignment.effective_to >= active_on,
            ),
        )
    return await _paginate(
        session,
        stmt.order_by(EmployeeShiftAssignment.effective_from.desc()),
        limit=limit,
        offset=offset,
    )


async def update_assignment(
    session: AsyncSession,
    actor: User,
    assignment_id: UUID,
    changes: dict[str, Any],
    *,
    ip: str | None = None,
) -> EmployeeShiftAssignment:
    """Close or re-flag an assignment.

    Attendance already recorded under this assignment is untouched: records
    carry their own ``shift_id``, so ending an assignment changes who may punch
    from now on, never what was logged before.
    """
    assignment = await _get_or_404(session, EmployeeShiftAssignment, assignment_id)

    new_to = changes.get("effective_to", assignment.effective_to)
    if new_to is not None and new_to < assignment.effective_from:
        raise InvalidAssignmentError("effective_to must not be before effective_from")

    # Extending the end date (or reopening it) can collide with a later
    # assignment, so re-check rather than trusting the original period.
    if "effective_to" in changes:
        clash = await find_overlapping_assignment(
            session,
            assignment.user_id,
            assignment.effective_from,
            new_to,
            exclude_id=assignment.id,
        )
        if clash is not None:
            raise AssignmentConflictError(
                "that period would overlap another assignment for this employee"
            )

    before = _snapshot(assignment, _ASSIGNMENT_FIELDS)
    for key, value in changes.items():
        setattr(assignment, key, value)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise AssignmentConflictError(
            "that period would overlap another assignment for this employee"
        ) from exc

    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.UPDATE,
        entity_type="employee_shift_assignment",
        entity_id=assignment.id,
        summary="Updated shift assignment",
        changes=_diff(before, _snapshot(assignment, _ASSIGNMENT_FIELDS)),
        ip=ip,
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


async def end_assignment(
    session: AsyncSession,
    actor: User,
    assignment_id: UUID,
    *,
    effective_to: date | None = None,
    ip: str | None = None,
) -> EmployeeShiftAssignment:
    """Stop an assignment from a date, defaulting to today."""
    return await update_assignment(
        session,
        actor,
        assignment_id,
        {"effective_to": effective_to or date.today()},
        ip=ip,
    )

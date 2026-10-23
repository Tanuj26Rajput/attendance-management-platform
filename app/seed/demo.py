"""Repeatable demo-data seeding for assessment walkthroughs.

Run it with ``python -m app.seed --yes``. It is deliberately awkward to run by
accident:

* it refuses to run unless ``APP_ENV`` is a development value, and
* it refuses to run without an explicit ``--yes`` (or ``SEED_DEMO_CONFIRM=yes``).

Nothing calls it from application startup or from a migration.

It only ever *adds*. Rows are matched on their natural keys (email, department
code, shift code, ``user_id``+``work_date``), so a second run fills gaps rather
than duplicating or wiping anything. The one exception is the password of a
demo account, which is re-set on every run so ``docs/demo-credentials.md``
cannot drift out of date; no account outside the fictional ``@example.com``
demo roster is ever touched.

Attendance is not hand-written. Punch times are generated, then handed to the
real rules engine in ``app.services.attendance_rules`` so every stored status,
``late_minutes`` and ``worked_minutes`` is exactly what the application itself
would have computed from those punches. Absences are *gaps*, not rows, because
that is how the reporting layer defines them: an expected working day with no
attendance record and no approved leave.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import dispose_engine, get_sessionmaker
from app.models.attendance import AttendanceRecord
from app.models.audit import AuditLog
from app.models.department import Department
from app.models.enums import (
    AttendanceSource,
    AttendanceStatus,
    AuditAction,
    EmploymentStatus,
    LeaveStatus,
    LeaveType,
    UserRole,
)
from app.models.leave import LeaveRequest
from app.models.shift import EmployeeShiftAssignment, Shift
from app.models.user import User
from app.services import attendance_rules as rules
from app.services.audit import record_audit

# Environments the seeder is willing to touch. "prod" and "staging" are absent
# on purpose.
ALLOWED_ENVS = frozenset({"local", "dev", "test"})

# How much history to generate, and how far back the shift assignments reach.
HISTORY_DAYS = 30
ASSIGNMENT_LEAD_DAYS = 120

# A fixed seed: two runs on the same day produce the same demo, so a
# screenshot taken yesterday still matches what the reviewer sees.
RANDOM_SEED = "polynomial-demo"

# The marker row that tells a second run the audit trail was already written.
# Audit rows are append-only, so they cannot be matched and updated like the
# rest; a marker is the only way to stay idempotent without touching them.
AUDIT_MARKER_ENTITY = "demo_seed"

ABSENCE_RATE = 0.06
LATE_RATE = 0.18


# ---------------------------------------------------------------------------
# the roster
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeptSpec:
    code: str
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class ShiftSpec:
    code: str
    name: str
    description: str
    timezone: str
    start: time
    end: time
    is_overnight: bool
    grace: int
    break_minutes: int
    working_days: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PersonSpec:
    code: str
    full_name: str
    email: str
    password: str
    role: UserRole
    dept: str
    shift: str
    job_title: str
    # True for the two department heads. The platform has no manager role (see
    # UserRole), so "manager" is modelled as Department.head_id plus
    # User.manager_id -- reporting lines, not extra privileges.
    is_head: bool = False


DEPARTMENTS: tuple[DeptSpec, ...] = (
    DeptSpec("ENG", "Engineering", "Platform and product engineering."),
    DeptSpec("SAL", "Sales", "New business and account management."),
    DeptSpec("SUP", "Customer Support", "Round-the-clock customer support."),
    DeptSpec("POP", "People Operations", "Hiring, onboarding and HR."),
)

SHIFTS: tuple[ShiftSpec, ...] = (
    ShiftSpec(
        "GEN",
        "General 09:00-18:00 (IST)",
        "Standard weekday shift for the India office.",
        "Asia/Kolkata",
        time(9, 0),
        time(18, 0),
        False,
        grace=15,
        break_minutes=60,
        working_days=(1, 2, 3, 4, 5),
    ),
    ShiftSpec(
        "EUR",
        "Europe Day 09:00-17:30 (London)",
        "Weekday shift for the London office; a tighter grace period.",
        "Europe/London",
        time(9, 0),
        time(17, 30),
        False,
        grace=10,
        break_minutes=45,
        working_days=(1, 2, 3, 4, 5),
    ),
    ShiftSpec(
        "NGT",
        "Night Support 22:00-06:00 (UTC)",
        "Overnight support cover; the shift belongs to the day it starts on.",
        "UTC",
        time(22, 0),
        time(6, 0),
        True,
        grace=20,
        break_minutes=45,
        working_days=(1, 2, 3, 4, 5),
    ),
    ShiftSpec(
        "WKD",
        "Weekend Support 10:00-18:00 (IST)",
        "Saturday and Sunday support cover.",
        "Asia/Kolkata",
        time(10, 0),
        time(18, 0),
        False,
        grace=30,
        break_minutes=60,
        working_days=(6, 7),
    ),
)

# Fictional people, reserved example.com addresses, demo-only passwords.
PEOPLE: tuple[PersonSpec, ...] = (
    PersonSpec(
        "ADM-DEMO-01",
        "Ada Whitfield",
        "ada.whitfield@example.com",
        "Demo!Admin-7fQ2vXpL",
        UserRole.ADMIN,
        "POP",
        "GEN",
        "Head of People Operations",
        is_head=True,
    ),
    PersonSpec(
        "MGR-DEMO-01",
        "Marcus Delacroix",
        "marcus.delacroix@example.com",
        "Demo!Lead-4hT9zRmK",
        UserRole.EMPLOYEE,
        "ENG",
        "GEN",
        "Engineering Manager",
        is_head=True,
    ),
    PersonSpec(
        "MGR-DEMO-02",
        "Priya Raghunathan",
        "priya.raghunathan@example.com",
        "Demo!Lead-6bW3nYcD",
        UserRole.EMPLOYEE,
        "SAL",
        "GEN",
        "Regional Sales Manager",
        is_head=True,
    ),
    PersonSpec(
        "EMP-DEMO-01",
        "Tomas Lindqvist",
        "tomas.lindqvist@example.com",
        "Demo!Staff-2kP8sVnR",
        UserRole.EMPLOYEE,
        "ENG",
        "GEN",
        "Backend Engineer",
    ),
    PersonSpec(
        "EMP-DEMO-02",
        "Chidinma Okonkwo",
        "chidinma.okonkwo@example.com",
        "Demo!Staff-9xL4jHtB",
        UserRole.EMPLOYEE,
        "ENG",
        "EUR",
        "Frontend Engineer",
    ),
    PersonSpec(
        "EMP-DEMO-03",
        "Rafael Monteiro",
        "rafael.monteiro@example.com",
        "Demo!Staff-5nG7qZwM",
        UserRole.EMPLOYEE,
        "ENG",
        "NGT",
        "Site Reliability Engineer",
    ),
    PersonSpec(
        "EMP-DEMO-04",
        "Hana Sugiyama",
        "hana.sugiyama@example.com",
        "Demo!Staff-3cR6dFyT",
        UserRole.EMPLOYEE,
        "SAL",
        "GEN",
        "Account Executive",
    ),
    PersonSpec(
        "EMP-DEMO-05",
        "Oliver Brennan",
        "oliver.brennan@example.com",
        "Demo!Staff-8vJ2mQsN",
        UserRole.EMPLOYEE,
        "SAL",
        "EUR",
        "Account Executive",
    ),
    PersonSpec(
        "EMP-DEMO-06",
        "Noor Al-Hassani",
        "noor.alhassani@example.com",
        "Demo!Staff-1zK5tBpW",
        UserRole.EMPLOYEE,
        "SUP",
        "NGT",
        "Support Specialist",
    ),
    PersonSpec(
        "EMP-DEMO-07",
        "Gabriel Osei",
        "gabriel.osei@example.com",
        "Demo!Staff-7yD3wLxC",
        UserRole.EMPLOYEE,
        "SUP",
        "WKD",
        "Support Specialist",
    ),
    PersonSpec(
        "EMP-DEMO-08",
        "Elena Castellanos",
        "elena.castellanos@example.com",
        "Demo!Staff-4qN9rGvH",
        UserRole.EMPLOYEE,
        "POP",
        "GEN",
        "People Partner",
    ),
)

# Who the live punch-in demo belongs to: this account is left without a
# check-in for today so the reviewer can press the button themselves.
LIVE_PUNCH_CODE = "EMP-DEMO-01"
# ...and this one is left checked in but not out, so punch-*out* is also live.
OPEN_RECORD_CODE = "EMP-DEMO-04"


@dataclass(slots=True)
class Counts:
    """What the run actually inserted, for the closing summary."""

    departments: int = 0
    shifts: int = 0
    users: int = 0
    assignments: int = 0
    attendance: int = 0
    leave: int = 0
    audit: int = 0
    skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# upserts -- each matches on a natural key and never deletes
# ---------------------------------------------------------------------------
async def _departments(session: AsyncSession, counts: Counts) -> dict[str, Department]:
    out: dict[str, Department] = {}
    for spec in DEPARTMENTS:
        row = await session.scalar(
            select(Department).where(Department.code == spec.code)
        )
        if row is None:
            row = Department(
                code=spec.code,
                name=spec.name,
                description=spec.description,
                is_active=True,
            )
            session.add(row)
            counts.departments += 1
        out[spec.code] = row
    await session.flush()
    return out


async def _shifts(session: AsyncSession, counts: Counts) -> dict[str, Shift]:
    out: dict[str, Shift] = {}
    for spec in SHIFTS:
        row = await session.scalar(select(Shift).where(Shift.code == spec.code))
        if row is None:
            row = Shift(
                code=spec.code,
                name=spec.name,
                description=spec.description,
                timezone=spec.timezone,
                start_time=spec.start,
                end_time=spec.end,
                is_overnight=spec.is_overnight,
                break_minutes=spec.break_minutes,
                grace_period_minutes=spec.grace,
                half_day_threshold_minutes=240,
                working_days=list(spec.working_days),
                is_active=True,
            )
            session.add(row)
            counts.shifts += 1
        out[spec.code] = row
    await session.flush()
    return out


async def _users(
    session: AsyncSession,
    departments: dict[str, Department],
    shifts: dict[str, Shift],
    today: date,
    counts: Counts,
) -> dict[str, User]:
    joined = today - timedelta(days=ASSIGNMENT_LEAD_DAYS)
    out: dict[str, User] = {}

    for spec in PEOPLE:
        row = await session.scalar(select(User).where(User.email == spec.email))
        if row is None:
            row = User(
                employee_code=spec.code,
                email=spec.email,
                full_name=spec.full_name,
                job_title=spec.job_title,
                role=spec.role,
                employment_status=EmploymentStatus.ACTIVE,
                is_active=True,
                department_id=departments[spec.dept].id,
                date_of_joining=joined,
                timezone=shifts[spec.shift].timezone,
            )
            session.add(row)
            counts.users += 1
        # Re-set on every run so the credentials document is never stale. Only
        # ever reached for the fictional example.com roster above.
        row.hashed_password = hash_password(spec.password)
        out[spec.code] = row

    await session.flush()

    # Reporting lines, now that everyone exists. The platform has no manager
    # role, so this is the whole of "department manager" that it supports.
    for spec in DEPARTMENTS:
        head = next(
            (out[p.code] for p in PEOPLE if p.is_head and p.dept == spec.code), None
        )
        if head is not None and departments[spec.code].head_id is None:
            departments[spec.code].head_id = head.id

    for spec in PEOPLE:
        if spec.is_head:
            continue
        head = next(
            (out[p.code] for p in PEOPLE if p.is_head and p.dept == spec.dept), None
        )
        manager = head or out["ADM-DEMO-01"]
        if out[spec.code].manager_id is None and manager.id != out[spec.code].id:
            out[spec.code].manager_id = manager.id

    await session.flush()
    return out


async def _assignments(
    session: AsyncSession,
    users: dict[str, User],
    shifts: dict[str, Shift],
    today: date,
    counts: Counts,
) -> dict[str, Shift]:
    """Give everyone one open-ended primary assignment. Returns code -> shift."""
    effective_from = today - timedelta(days=ASSIGNMENT_LEAD_DAYS)
    assigned: dict[str, Shift] = {}

    for spec in PEOPLE:
        user = users[spec.code]
        shift = shifts[spec.shift]
        existing = await session.scalar(
            select(EmployeeShiftAssignment).where(
                EmployeeShiftAssignment.user_id == user.id,
                EmployeeShiftAssignment.is_primary.is_(True),
                EmployeeShiftAssignment.effective_to.is_(None),
            )
        )
        if existing is None:
            session.add(
                EmployeeShiftAssignment(
                    user_id=user.id,
                    shift_id=shift.id,
                    effective_from=effective_from,
                    effective_to=None,
                    is_primary=True,
                )
            )
            counts.assignments += 1
            assigned[spec.code] = shift
        else:
            # Somebody already has a live assignment -- respect it and judge
            # their generated attendance against *that* shift, not ours.
            assigned[spec.code] = await session.get(Shift, existing.shift_id) or shift

    await session.flush()
    return assigned


# ---------------------------------------------------------------------------
# attendance
# ---------------------------------------------------------------------------
def _last_weekday(today: date, iso_weekday: int) -> date:
    """The most recent strictly-past date falling on ``iso_weekday``."""
    delta = (today.isoweekday() - iso_weekday) % 7 or 7
    return today - timedelta(days=delta)


def _build_record(
    user: User,
    shift: Shift,
    work_date: date,
    *,
    late: bool,
    leave_open: bool,
    rng: random.Random,
) -> AttendanceRecord | None:
    """Generate one day's punches and let the rules engine grade them."""
    rule_set = rules.ShiftRules.from_shift(shift)
    tz = rule_set.zone

    start_local = datetime.combine(work_date, shift.start_time, tzinfo=tz)
    if late:
        offset = rng.randint(shift.grace_period_minutes + 4, 75)
    elif shift.is_overnight:
        # An early arrival on an overnight shift would resolve to the previous
        # business day, so these start on time or a touch after.
        offset = rng.randint(0, max(0, shift.grace_period_minutes - 3))
    else:
        offset = rng.randint(-18, max(0, shift.grace_period_minutes - 3))

    check_in_utc = (start_local + timedelta(minutes=offset)).astimezone(UTC)
    punched_in = rules.evaluate_punch_in(check_in_utc, rule_set, tz)
    if punched_in.work_date != work_date:  # pragma: no cover - guarded above
        return None

    record = AttendanceRecord(
        user_id=user.id,
        shift_id=shift.id,
        work_date=work_date,
        check_in_at=check_in_utc,
        status=punched_in.status,
        late_minutes=punched_in.late_minutes,
        source=AttendanceSource.WEB,
    )

    if leave_open:
        return record

    end_local = rules.scheduled_end(work_date, rule_set, tz)
    check_out_utc = (end_local + timedelta(minutes=rng.randint(-22, 50))).astimezone(
        UTC
    )
    if check_out_utc <= check_in_utc:
        check_out_utc = check_in_utc + timedelta(hours=6)

    punched_out = rules.evaluate_punch_out(
        check_in_utc,
        check_out_utc,
        work_date,
        rule_set,
        tz,
        punched_in.late_minutes,
    )
    record.check_out_at = check_out_utc
    record.status = punched_out.status
    record.worked_minutes = punched_out.worked_minutes
    record.early_leave_minutes = punched_out.early_leave_minutes
    record.overtime_minutes = punched_out.overtime_minutes
    return record


async def _attendance(
    session: AsyncSession,
    users: dict[str, User],
    assigned: dict[str, Shift],
    leave_days: dict[str, set[date]],
    today: date,
    counts: Counts,
) -> list[AttendanceRecord]:
    days = [today - timedelta(days=n) for n in range(HISTORY_DAYS, -1, -1)]
    last_monday = _last_weekday(today, 1)
    month_start = today.replace(day=1)
    created: list[AttendanceRecord] = []

    for spec in PEOPLE:
        user = users[spec.code]
        shift = assigned[spec.code]
        rule_set = rules.ShiftRules.from_shift(shift)
        rng = random.Random(f"{RANDOM_SEED}:{spec.code}")

        existing = set(
            (
                await session.scalars(
                    select(AttendanceRecord.work_date).where(
                        AttendanceRecord.user_id == user.id,
                        AttendanceRecord.work_date >= days[0],
                    )
                )
            ).all()
        )

        for day in days:
            if day in existing:
                continue
            if not rules.is_working_day(day, rule_set):
                continue
            if day in leave_days.get(spec.code, set()):
                continue

            # Today is special: one account is left with nothing so punch-in
            # can be demonstrated live, one is left open so punch-out can be.
            if day == today:
                if spec.code == LIVE_PUNCH_CODE:
                    continue
                if spec.code != OPEN_RECORD_CODE:
                    continue

            roll = rng.random()
            # Two Sales people are absent last Monday so "how many employees
            # were absent last Monday?" has a non-zero, checkable answer.
            if day == last_monday and spec.code in {"EMP-DEMO-04", "EMP-DEMO-07"}:
                continue
            if day != today and roll < ABSENCE_RATE:
                continue

            # Sales gets a guaranteed cluster of late arrivals this month so
            # "show late arrivals for the Sales department this month" is not
            # an empty table.
            forced_late = (
                spec.dept == "SAL" and day >= month_start and day.isoweekday() in (2, 4)
            )
            late = forced_late or roll < ABSENCE_RATE + LATE_RATE

            record = _build_record(
                user,
                shift,
                day,
                late=late,
                leave_open=(day == today),
                rng=rng,
            )
            if record is None:
                continue
            session.add(record)
            created.append(record)
            counts.attendance += 1

    await session.flush()
    return created


# ---------------------------------------------------------------------------
# leave
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LeaveSpec:
    code: str
    leave_type: LeaveType
    start_offset: int  # days from today; negative is in the past
    length: int
    status: LeaveStatus
    reason: str


LEAVE: tuple[LeaveSpec, ...] = (
    LeaveSpec(
        "EMP-DEMO-02",
        LeaveType.ANNUAL,
        -18,
        3,
        LeaveStatus.APPROVED,
        "Family holiday booked at the start of the quarter.",
    ),
    LeaveSpec(
        "EMP-DEMO-06",
        LeaveType.SICK,
        -9,
        2,
        LeaveStatus.APPROVED,
        "Flu; doctor's note provided.",
    ),
    LeaveSpec(
        "EMP-DEMO-03",
        LeaveType.CASUAL,
        4,
        1,
        LeaveStatus.PENDING,
        "Moving house.",
    ),
    LeaveSpec(
        "EMP-DEMO-05",
        LeaveType.ANNUAL,
        11,
        5,
        LeaveStatus.PENDING,
        "Annual leave over the long weekend.",
    ),
    LeaveSpec(
        "EMP-DEMO-08",
        LeaveType.UNPAID,
        7,
        2,
        LeaveStatus.PENDING,
        "Unpaid leave for a training course.",
    ),
)


def _leave_dates(today: date) -> dict[str, set[date]]:
    """The days attendance generation must leave alone, per employee.

    Only approved leave counts: a pending request has not excused anybody from
    turning up yet, which is precisely what makes it worth demonstrating.
    """
    out: dict[str, set[date]] = {}
    for spec in LEAVE:
        if spec.status is not LeaveStatus.APPROVED:
            continue
        start = today + timedelta(days=spec.start_offset)
        out.setdefault(spec.code, set()).update(
            start + timedelta(days=n) for n in range(spec.length)
        )
    return out


async def _leave(
    session: AsyncSession,
    users: dict[str, User],
    admin: User,
    today: date,
    counts: Counts,
) -> None:
    for spec in LEAVE:
        user = users[spec.code]
        start = today + timedelta(days=spec.start_offset)
        end = start + timedelta(days=spec.length - 1)

        existing = await session.scalar(
            select(LeaveRequest).where(
                LeaveRequest.user_id == user.id,
                LeaveRequest.start_date == start,
            )
        )
        if existing is not None:
            continue

        row = LeaveRequest(
            user_id=user.id,
            leave_type=spec.leave_type,
            start_date=start,
            end_date=end,
            total_days=Decimal(spec.length),
            reason=spec.reason,
            status=spec.status,
        )
        # The decided_requires_reviewer check constraint: an approved or
        # rejected request must name who decided it and when.
        if spec.status in (LeaveStatus.APPROVED, LeaveStatus.REJECTED):
            row.reviewed_by_id = admin.id
            row.reviewed_at = datetime.combine(
                start - timedelta(days=2), time(10, 30), tzinfo=UTC
            )
            row.review_comment = "Approved; cover arranged within the team."
        session.add(row)
        counts.leave += 1

    await session.flush()


# ---------------------------------------------------------------------------
# a correction, and the audit trail that goes with it
# ---------------------------------------------------------------------------
async def _correction_and_audit(
    session: AsyncSession,
    users: dict[str, User],
    admin: User,
    records: list[AttendanceRecord],
    counts: Counts,
) -> None:
    marker = await session.scalar(
        select(AuditLog).where(AuditLog.entity_type == AUDIT_MARKER_ENTITY).limit(1)
    )
    if marker is not None:
        counts.skipped.append("audit trail (already seeded)")
        return

    # One administrative correction: an employee who forgot to punch out and
    # had the day fixed by an admin. Done through the model, recorded in the
    # trail with a before/after, exactly as the API would.
    target = next(
        (
            r
            for r in records
            if r.status is AttendanceStatus.HALF_DAY and r.check_out_at is not None
        ),
        None,
    ) or next((r for r in records if r.check_out_at is not None), None)

    if target is not None:
        before = {
            "status": str(target.status),
            "worked_minutes": target.worked_minutes,
            "is_manual_entry": target.is_manual_entry,
        }
        target.status = AttendanceStatus.PRESENT
        target.worked_minutes = max(target.worked_minutes or 0, 480)
        target.is_manual_entry = True
        target.approved_by_id = admin.id
        target.notes = "Corrected by People Ops: employee forgot to punch out."
        record_audit(
            session,
            actor_id=admin.id,
            action=AuditAction.UPDATE,
            entity_type="attendance_record",
            entity_id=target.id,
            summary=f"Corrected attendance for {target.work_date}",
            changes={
                "before": before,
                "after": {
                    "status": str(target.status),
                    "worked_minutes": target.worked_minutes,
                    "is_manual_entry": True,
                },
            },
        )
        counts.audit += 1

    # A handful of ordinary administrative events. No passwords, no tokens, no
    # free-text personal detail -- ids and field names only, which is what the
    # audit service itself writes.
    events: list[dict[str, object]] = [
        {
            "action": AuditAction.LOGIN,
            "entity_type": "user",
            "entity_id": admin.id,
            "summary": "Administrator signed in",
        },
        {
            "action": AuditAction.CREATE,
            "entity_type": "department",
            "entity_id": None,
            "summary": "Created department Customer Support",
            "changes": {"after": {"code": "SUP"}},
        },
        {
            "action": AuditAction.CREATE,
            "entity_type": "shift",
            "entity_id": None,
            "summary": "Created shift Night Support 22:00-06:00 (UTC)",
            "changes": {"after": {"code": "NGT", "is_overnight": True}},
        },
        {
            "action": AuditAction.UPDATE,
            "entity_type": "shift",
            "entity_id": None,
            "summary": "Raised the grace period on Weekend Support",
            "changes": {"before": {"grace_period_minutes": 15},
                        "after": {"grace_period_minutes": 30}},
        },
        {
            "action": AuditAction.EXPORT,
            "entity_type": "report",
            "entity_id": None,
            "summary": "Exported the monthly attendance summary",
            "changes": {"report": "attendance_summary", "parameters": {"scope": "all"}},
        },
    ]
    for spec in LEAVE:
        if spec.status is LeaveStatus.APPROVED:
            events.append(
                {
                    "action": AuditAction.APPROVE,
                    "entity_type": "leave_request",
                    "entity_id": users[spec.code].id,
                    "summary": f"Approved {spec.leave_type} leave",
                }
            )

    for event in events:
        record_audit(session, actor_id=admin.id, **event)  # type: ignore[arg-type]
        counts.audit += 1

    record_audit(
        session,
        actor_id=admin.id,
        action=AuditAction.CREATE,
        entity_type=AUDIT_MARKER_ENTITY,
        summary="Demo data seeded; the audit trail above is synthetic.",
    )
    counts.audit += 1
    await session.flush()


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------
async def seed(session: AsyncSession, today: date | None = None) -> Counts:
    """Populate the demo dataset inside the caller's transaction."""
    today = today or datetime.now(UTC).date()
    counts = Counts()

    departments = await _departments(session, counts)
    shifts = await _shifts(session, counts)
    users = await _users(session, departments, shifts, today, counts)
    assigned = await _assignments(session, users, shifts, today, counts)

    admin = users["ADM-DEMO-01"]
    await _leave(session, users, admin, today, counts)
    records = await _attendance(
        session, users, assigned, _leave_dates(today), today, counts
    )
    await _correction_and_audit(session, users, admin, records, counts)
    return counts


def _guard(confirmed: bool) -> str | None:
    """Return a refusal message, or None if the run may proceed."""
    env = get_settings().APP_ENV
    if env not in ALLOWED_ENVS:
        return (
            f"Refusing to seed: APP_ENV is {env!r}. Demo data may only be "
            f"inserted when APP_ENV is one of {sorted(ALLOWED_ENVS)}."
        )
    if not confirmed:
        return (
            "Refusing to seed without explicit confirmation. Re-run with "
            "--yes (or set SEED_DEMO_CONFIRM=yes). This writes demo "
            "accounts and attendance history into the configured database."
        )
    return None


async def _run(today: date | None) -> int:
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            try:
                counts = await seed(session, today)
                await session.commit()
            except Exception:
                # One transaction for the whole dataset: a partial failure
                # leaves the database exactly as it was found.
                await session.rollback()
                raise
    finally:
        await dispose_engine()

    print("Demo data seeded.")
    print(f"  departments : {counts.departments:>4} new")
    print(f"  shifts      : {counts.shifts:>4} new")
    print(f"  users       : {counts.users:>4} new")
    print(f"  assignments : {counts.assignments:>4} new")
    print(f"  attendance  : {counts.attendance:>4} new")
    print(f"  leave       : {counts.leave:>4} new")
    print(f"  audit       : {counts.audit:>4} new")
    for note in counts.skipped:
        print(f"  skipped     : {note}")
    print("\nCredentials: docs/demo-credentials.md (development use only).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="Insert demo accounts, shifts and attendance history.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm that demo data may be written to the configured database",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="pretend today is this date (YYYY-MM-DD); for reproducible demos",
    )
    args = parser.parse_args(argv)

    confirmed = args.yes or os.getenv("SEED_DEMO_CONFIRM", "").lower() == "yes"
    refusal = _guard(confirmed)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2

    return asyncio.run(_run(args.today))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

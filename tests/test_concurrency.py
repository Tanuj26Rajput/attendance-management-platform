"""Concurrency tests for the punch workflow.

Every other integration test in this suite runs inside one rolled-back
transaction, which is what keeps them isolated and fast -- but it also means
they can only ever exercise *sequential* behaviour. A second connection cannot
see uncommitted rows, so a race against them is unobservable.

These tests therefore commit their fixtures and clean up after themselves in a
``finally``. They are the only tests here that do, and they are slower because
of it, so there are deliberately few of them: just enough to show that the
duplicate-punch guard is the database's unique constraint rather than the
application's read-then-write, which can always be raced.

Cleanup cannot remove the audit rows these punches produce -- the trail is
append-only and the database refuses -- so each run leaves a handful of audit
records behind for a user that no longer... exists is too strong: see
``test_a_user_with_audit_history_cannot_be_deleted``. Users are deactivated,
never deleted, which is the same thing the API does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.attendance import AttendanceRecord
from app.models.enums import UserRole
from app.models.shift import EmployeeShiftAssignment, Shift
from app.models.user import User
from app.services import attendance as attendance_service
from app.services.attendance import AlreadyPunchedInError, AlreadyPunchedOutError

pytestmark = pytest.mark.integration

EVERY_DAY = [1, 2, 3, 4, 5, 6, 7]


class Committed:
    """A user with a shift assignment, visible to every connection.

    Committed on purpose: the point of these tests is that two *separate*
    connections collide, which cannot happen inside one test transaction.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.maker = async_sessionmaker(engine, expire_on_commit=False)
        self.user_id: uuid.UUID | None = None
        self.shift_id: uuid.UUID | None = None

    async def __aenter__(self) -> Committed:
        tag = uuid.uuid4().hex[:8].upper()
        async with self.maker() as session:
            shift = Shift(
                name=f"Race {tag}",
                code=f"RACE{tag[:4]}",
                timezone="UTC",
                # Open all day, so the test never depends on the clock.
                start_time=dt.time(0, 0),
                end_time=dt.time(23, 59),
                is_overnight=False,
                working_days=EVERY_DAY,
                grace_period_minutes=1440,
            )
            user = User(
                email=f"race-{tag.lower()}@example.com",
                employee_code=f"RC-{tag}",
                full_name="Race Probe",
                hashed_password="not-a-real-hash",
                role=UserRole.EMPLOYEE,
                timezone="UTC",
            )
            session.add_all([shift, user])
            await session.flush()
            session.add(
                EmployeeShiftAssignment(
                    user_id=user.id,
                    shift_id=shift.id,
                    effective_from=dt.date.today() - dt.timedelta(days=1),
                    is_primary=True,
                )
            )
            await session.commit()
            self.user_id, self.shift_id = user.id, shift.id
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        async with self.maker() as session:
            await session.execute(
                delete(AttendanceRecord).where(AttendanceRecord.user_id == self.user_id)
            )
            await session.execute(
                delete(EmployeeShiftAssignment).where(
                    EmployeeShiftAssignment.user_id == self.user_id
                )
            )
            # The user and shift stay: audit rows reference the user, and the
            # append-only trigger blocks the ON DELETE SET NULL that removing
            # them would trigger. Deactivating is what the API does anyway.
            user = await session.get(User, self.user_id)
            if user is not None:
                user.is_active = False
            shift = await session.get(Shift, self.shift_id)
            if shift is not None:
                shift.is_active = False
            await session.commit()

    async def punch_in(self) -> object:
        """A punch on its own connection, as a separate request would be."""
        async with self.maker() as session:
            user = await session.get(User, self.user_id)
            return await attendance_service.punch_in(session, user)

    async def punch_out(self) -> object:
        async with self.maker() as session:
            user = await session.get(User, self.user_id)
            return await attendance_service.punch_out(session, user)

    async def record_count(self) -> int:
        async with self.maker() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(AttendanceRecord)
                    .where(AttendanceRecord.user_id == self.user_id)
                )
                or 0
            )


async def test_two_simultaneous_punch_ins_create_exactly_one_record(engine) -> None:
    """The real guard is the unique constraint, not the preceding SELECT.

    ``punch_in`` reads the day's record before inserting, but on a day with no
    record yet there is nothing for ``FOR UPDATE`` to lock, so both callers see
    "nothing here" and both insert. Only one survives, and the loser is
    translated into the same error a sequential duplicate would get.
    """
    async with Committed(engine) as fixture:
        results = await asyncio.gather(
            fixture.punch_in(), fixture.punch_in(), return_exceptions=True
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        successes = [r for r in results if not isinstance(r, BaseException)]

        assert len(successes) == 1, f"expected one winner, got {results}"
        assert len(failures) == 1
        assert isinstance(failures[0], AlreadyPunchedInError), failures[0]
        assert await fixture.record_count() == 1


async def test_many_simultaneous_punch_ins_still_create_one_record(engine) -> None:
    """Five at once, as a double-clicked button on a slow connection would be."""
    async with Committed(engine) as fixture:
        results = await asyncio.gather(
            *(fixture.punch_in() for _ in range(5)), return_exceptions=True
        )

        successes = [r for r in results if not isinstance(r, BaseException)]
        assert len(successes) == 1
        assert all(
            isinstance(r, AlreadyPunchedInError)
            for r in results
            if isinstance(r, BaseException)
        ), results
        assert await fixture.record_count() == 1


async def test_two_simultaneous_punch_outs_close_the_day_once(engine) -> None:
    """Here the row *does* exist, so FOR UPDATE genuinely serialises them."""
    async with Committed(engine) as fixture:
        await fixture.punch_in()

        results = await asyncio.gather(
            fixture.punch_out(), fixture.punch_out(), return_exceptions=True
        )

        successes = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(successes) == 1, f"expected one winner, got {results}"
        assert isinstance(failures[0], AlreadyPunchedOutError), failures[0]

        async with fixture.maker() as session:
            record = (
                await session.execute(
                    select(AttendanceRecord).where(
                        AttendanceRecord.user_id == fixture.user_id
                    )
                )
            ).scalar_one()
            assert record.check_out_at is not None


async def test_a_duplicate_punch_in_after_the_first_commits_is_refused(engine) -> None:
    """The sequential case, on separate connections rather than one session."""
    async with Committed(engine) as fixture:
        await fixture.punch_in()
        with pytest.raises(AlreadyPunchedInError):
            await fixture.punch_in()
        assert await fixture.record_count() == 1


async def test_punching_in_again_after_punching_out_is_refused(engine) -> None:
    """A completed day stays completed; it does not reopen."""
    async with Committed(engine) as fixture:
        await fixture.punch_in()
        await fixture.punch_out()
        with pytest.raises(AlreadyPunchedOutError):
            await fixture.punch_in()
        assert await fixture.record_count() == 1

"""Shift definitions and the employee-to-shift assignment history."""

from __future__ import annotations

from datetime import date, time
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.attendance import AttendanceRecord
    from app.models.user import User


class Shift(UUIDMixin, TimestampMixin, Base):
    """A named working pattern, e.g. "General 9-6" or "Night 10-7"."""

    __tablename__ = "shifts"
    __table_args__ = (
        CheckConstraint(
            "break_minutes >= 0 AND grace_period_minutes >= 0",
            name="non_negative_minutes",
        ),
        CheckConstraint(
            "half_day_threshold_minutes > 0",
            name="positive_half_day_threshold",
        ),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    # Shift times are naive wall-clock values; this is the zone they are read
    # in. Authoritative for attendance evaluation -- an employee's own
    # User.timezone is only a fallback when no shift is assigned.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )

    start_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    # end_time < start_time on its own is ambiguous, so overnight shifts are
    # flagged rather than inferred.
    is_overnight: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    break_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default=text("60")
    )
    # Minutes after start_time before a check-in counts as late.
    grace_period_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default=text("15")
    )
    # Worked minutes below this mark the day as a half day.
    half_day_threshold_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=240, server_default=text("240")
    )

    # ISO weekday numbers the shift runs on: 1 = Monday ... 7 = Sunday.
    working_days: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger),
        nullable=False,
        server_default=text("'{1,2,3,4,5}'::smallint[]"),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), index=True
    )

    assignments: Mapped[list[EmployeeShiftAssignment]] = relationship(
        "EmployeeShiftAssignment",
        back_populates="shift",
        passive_deletes=True,
    )
    attendance_records: Mapped[list[AttendanceRecord]] = relationship(
        "AttendanceRecord", back_populates="shift"
    )

    def __repr__(self) -> str:
        return f"<Shift {self.code}>"


class EmployeeShiftAssignment(UUIDMixin, TimestampMixin, Base):
    """Which shift an employee works, over a date range.

    Rows are kept rather than overwritten so historical attendance can be
    judged against the shift that was actually in force at the time.
    """

    __tablename__ = "employee_shift_assignments"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "effective_from",
            name="uq_employee_shift_assignments_user_effective_from",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="valid_date_range",
        ),
        Index(
            "ix_employee_shift_assignments_user_period",
            "user_id",
            "effective_from",
            "effective_to",
        ),
        # Overlapping assignments are additionally blocked by a gist EXCLUDE
        # constraint (ex_employee_shift_assignments_no_overlap), added by
        # migration: Alembic cannot autogenerate or diff exclusion
        # constraints, so it is written out there rather than declared here.
        # At most one open-ended primary shift per employee.
        Index(
            "uq_employee_shift_assignments_one_open_primary",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary AND effective_to IS NULL"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    shift_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # RESTRICT: a shift that has been worked must not vanish.
        ForeignKey("shifts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    user: Mapped[User] = relationship("User", back_populates="shift_assignments")
    shift: Mapped[Shift] = relationship("Shift", back_populates="assignments")

    def __repr__(self) -> str:
        return f"<EmployeeShiftAssignment user={self.user_id} shift={self.shift_id}>"

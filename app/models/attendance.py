"""Daily attendance records."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import AttendanceSource, AttendanceStatus, pg_enum

if TYPE_CHECKING:
    from app.models.shift import Shift
    from app.models.user import User


class AttendanceRecord(UUIDMixin, TimestampMixin, Base):
    """One row per employee per working day.

    The daily grain (rather than one row per punch) is what makes
    ``(user_id, work_date)`` uniquely enforceable and keeps reporting queries
    simple. Multiple punches in a day are folded into first-in / last-out.
    """

    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "work_date", name="uq_attendance_records_user_work_date"
        ),
        CheckConstraint(
            "check_out_at IS NULL OR check_in_at IS NULL "
            "OR check_out_at >= check_in_at",
            name="checkout_after_checkin",
        ),
        CheckConstraint(
            "worked_minutes IS NULL OR worked_minutes >= 0",
            name="non_negative_worked_minutes",
        ),
        CheckConstraint(
            "anomaly_score IS NULL OR (anomaly_score >= 0 AND anomaly_score <= 1)",
            name="anomaly_score_range",
        ),
        # Covers the two dominant reads: one employee's history, and
        # "who was late on date X".
        Index("ix_attendance_records_user_date", "user_id", "work_date"),
        Index("ix_attendance_records_date_status", "work_date", "status"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # The shift in force that day, captured so later reassignment does not
    # retroactively change whether someone was late.
    shift_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("shifts.id", ondelete="SET NULL")
    )

    check_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[AttendanceStatus] = mapped_column(
        pg_enum(AttendanceStatus, "attendance_status"),
        nullable=False,
        default=AttendanceStatus.ABSENT,
        server_default=AttendanceStatus.ABSENT.value,
    )
    source: Mapped[AttendanceSource] = mapped_column(
        pg_enum(AttendanceSource, "attendance_source"),
        nullable=False,
        default=AttendanceSource.WEB,
        server_default=AttendanceSource.WEB.value,
    )

    # --- derived totals, written by the attendance service ---
    worked_minutes: Mapped[int | None] = mapped_column(Integer)
    late_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    early_leave_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    overtime_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # --- provenance ---
    check_in_ip: Mapped[str | None] = mapped_column(INET)
    check_out_ip: Mapped[str | None] = mapped_column(INET)
    check_in_latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    check_in_longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    is_manual_entry: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)

    # --- AI anomaly detection ---
    anomaly_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    anomaly_reason: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(
        "User", foreign_keys=[user_id], back_populates="attendance_records"
    )
    approved_by: Mapped[User | None] = relationship(
        "User", foreign_keys=[approved_by_id]
    )
    shift: Mapped[Shift | None] = relationship(
        "Shift", back_populates="attendance_records"
    )

    def __repr__(self) -> str:
        return f"<AttendanceRecord {self.user_id} {self.work_date} {self.status}>"

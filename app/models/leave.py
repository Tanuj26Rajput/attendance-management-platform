"""Leave requests and their approval trail."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import LeaveStatus, LeaveType, pg_enum

if TYPE_CHECKING:
    from app.models.user import User


class LeaveRequest(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="valid_date_range"),
        CheckConstraint("total_days > 0", name="positive_total_days"),
        # A decided request must record who decided it and when.
        CheckConstraint(
            "status NOT IN ('approved', 'rejected') "
            "OR (reviewed_by_id IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="decided_requires_reviewer",
        ),
        Index("ix_leave_requests_user_start_date", "user_id", "start_date"),
        Index("ix_leave_requests_status_start_date", "status", "start_date"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    leave_type: Mapped[LeaveType] = mapped_column(
        pg_enum(LeaveType, "leave_type"), nullable=False, index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Numeric, not Integer: half days are the common case.
    total_days: Mapped[Decimal] = mapped_column(Numeric(5, 1), nullable=False)

    reason: Mapped[str | None] = mapped_column(Text)
    attachment_url: Mapped[str | None] = mapped_column(String(512))

    status: Mapped[LeaveStatus] = mapped_column(
        pg_enum(LeaveStatus, "leave_status"),
        nullable=False,
        default=LeaveStatus.PENDING,
        server_default=LeaveStatus.PENDING.value,
    )

    # --- review trail ---
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(
        "User", foreign_keys=[user_id], back_populates="leave_requests"
    )
    reviewed_by: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[reviewed_by_id],
        back_populates="reviewed_leave_requests",
    )

    @property
    def is_decided(self) -> bool:
        return self.status in (LeaveStatus.APPROVED, LeaveStatus.REJECTED)

    def __repr__(self) -> str:
        return (
            f"<LeaveRequest {self.user_id} "
            f"{self.start_date}..{self.end_date} {self.status}>"
        )

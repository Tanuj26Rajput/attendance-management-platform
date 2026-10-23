"""Employees and administrators.

A single table holds both; ``role`` distinguishes them. Admins are rare enough
that a separate table would buy nothing and complicate every join.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import EmploymentStatus, UserRole, pg_enum

if TYPE_CHECKING:
    from app.models.attendance import AttendanceRecord
    from app.models.audit import AuditLog
    from app.models.department import Department
    from app.models.leave import LeaveRequest
    from app.models.shift import EmployeeShiftAssignment
    from app.models.token import RevokedToken


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "email = lower(email)",
            name="email_lowercase",
        ),
        CheckConstraint(
            "manager_id IS NULL OR manager_id <> id",
            name="manager_not_self",
        ),
        Index("ix_users_department_role", "department_id", "role"),
    )

    # --- identity ---
    employee_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    job_title: Mapped[str | None] = mapped_column(String(120))

    # --- authorisation ---
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"),
        nullable=False,
        default=UserRole.EMPLOYEE,
        server_default=UserRole.EMPLOYEE.value,
        index=True,
    )
    employment_status: Mapped[EmploymentStatus] = mapped_column(
        pg_enum(EmploymentStatus, "employment_status"),
        nullable=False,
        default=EmploymentStatus.ACTIVE,
        server_default=EmploymentStatus.ACTIVE.value,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # --- org placement ---
    department_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        index=True,
    )
    manager_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )

    date_of_joining: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- relationships ---
    department: Mapped[Department | None] = relationship(
        "Department", foreign_keys=[department_id], back_populates="employees"
    )
    headed_department: Mapped[Department | None] = relationship(
        "Department",
        foreign_keys="Department.head_id",
        back_populates="head",
        uselist=False,
    )

    manager: Mapped[User | None] = relationship(
        "User", remote_side="User.id", back_populates="reports"
    )
    reports: Mapped[list[User]] = relationship(
        "User", back_populates="manager", foreign_keys=[manager_id]
    )

    shift_assignments: Mapped[list[EmployeeShiftAssignment]] = relationship(
        "EmployeeShiftAssignment",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    attendance_records: Mapped[list[AttendanceRecord]] = relationship(
        "AttendanceRecord",
        foreign_keys="AttendanceRecord.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    leave_requests: Mapped[list[LeaveRequest]] = relationship(
        "LeaveRequest",
        foreign_keys="LeaveRequest.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reviewed_leave_requests: Mapped[list[LeaveRequest]] = relationship(
        "LeaveRequest",
        foreign_keys="LeaveRequest.reviewed_by_id",
        back_populates="reviewed_by",
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        "AuditLog", foreign_keys="AuditLog.actor_id", back_populates="actor"
    )
    revoked_tokens: Mapped[list[RevokedToken]] = relationship(
        "RevokedToken",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    def __repr__(self) -> str:
        return f"<User {self.employee_code} {self.email}>"

"""Organisational units."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.user import User


class Department(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "departments"

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    # Departments are deactivated, never deleted: employees and attendance
    # history reference them.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"), index=True
    )

    # departments.head_id and users.department_id reference each other, so this
    # FK is created with ALTER after both tables exist, breaking the cycle.
    head_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", use_alter=True),
        index=True,
    )

    head: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[head_id],
        back_populates="headed_department",
        post_update=True,
    )
    employees: Mapped[list[User]] = relationship(
        "User",
        foreign_keys="User.department_id",
        back_populates="department",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Department {self.code}>"

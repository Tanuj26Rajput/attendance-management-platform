"""Enumerations shared by the ORM models.

Each is mapped to a native PostgreSQL ENUM type. ``values_callable`` makes
SQLAlchemy persist the *values* ("admin") rather than the member *names*
("ADMIN"), so what lands in the database stays readable.
"""

import enum
from typing import TypeVar

from sqlalchemy import Enum as SAEnum

_E = TypeVar("_E", bound=enum.Enum)


def pg_enum(enum_cls: type[_E], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class UserRole(enum.StrEnum):
    EMPLOYEE = "employee"
    ADMIN = "admin"


class EmploymentStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


class AttendanceStatus(enum.StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    LATE = "late"
    HALF_DAY = "half_day"
    ON_LEAVE = "on_leave"
    HOLIDAY = "holiday"
    WEEKEND = "weekend"


class AttendanceSource(enum.StrEnum):
    """How a punch reached the system."""

    WEB = "web"
    MOBILE = "mobile"
    BIOMETRIC = "biometric"
    IMPORT = "import"
    MANUAL = "manual"


class LeaveType(enum.StrEnum):
    ANNUAL = "annual"
    SICK = "sick"
    CASUAL = "casual"
    UNPAID = "unpaid"
    MATERNITY = "maternity"
    PATERNITY = "paternity"
    BEREAVEMENT = "bereavement"


class LeaveStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AuditAction(enum.StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGOUT = "logout"
    APPROVE = "approve"
    REJECT = "reject"
    EXPORT = "export"
    # Reads worth recording: who looked at whose attendance, and who asked the
    # assistant about it. DELETE exists for completeness only -- nothing in the
    # application deletes an audited entity.
    READ = "read"
    QUERY = "query"

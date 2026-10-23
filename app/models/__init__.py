"""ORM models.

Every model module must be imported here so that ``Base.metadata`` is fully
populated before Alembic autogenerates a migration.

Tables are created by migrations only -- nothing calls ``create_all`` at
startup.
"""

from app.db.base import Base
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
from app.models.token import RevokedToken
from app.models.user import User

__all__ = [
    "AttendanceRecord",
    "AttendanceSource",
    "AttendanceStatus",
    "AuditAction",
    "AuditLog",
    "Base",
    "Department",
    "EmployeeShiftAssignment",
    "EmploymentStatus",
    "LeaveRequest",
    "LeaveStatus",
    "LeaveType",
    "RevokedToken",
    "Shift",
    "User",
    "UserRole",
]

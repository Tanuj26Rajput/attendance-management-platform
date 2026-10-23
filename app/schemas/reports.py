"""Response schemas for attendance reporting.

These are read-only projections of the dataclasses in ``services.reports``;
the rates are recomputed there, never in the schema, so there is exactly one
definition of what "attendance rate" means.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import AttendanceStatus


class Granularity(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class DailySummaryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    work_date: date
    expected: int
    present: int
    late: int
    half_day: int
    absent: int
    on_leave: int
    attendance_rate: float
    absence_rate: float


class TrendPointPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period_start: date
    period_end: date
    expected: int
    attended: int
    late: int
    absent: int
    on_leave: int
    attendance_rate: float
    absence_rate: float
    average_late_minutes: float


class EmployeeDayPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    employee_code: str
    full_name: str
    department_id: UUID | None
    department_name: str | None
    work_date: date
    status: AttendanceStatus
    late_minutes: int
    worked_minutes: int | None


class AbsenteeismPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    group_id: UUID | None
    group_label: str
    expected_days: int
    absent_days: int
    on_leave_days: int
    late_days: int
    attended_days: int
    absence_rate: float
    attendance_rate: float

"""Request/response schemas for the attendance workflows."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import AttendanceSource, AttendanceStatus


class PunchInRequest(BaseModel):
    """Optional context for a punch.

    There is no ``user_id``: identity comes from the bearer token, never from
    the body, so one employee cannot punch for another.
    """

    model_config = ConfigDict(extra="forbid")

    source: AttendanceSource = AttendanceSource.WEB
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    notes: str | None = Field(default=None, max_length=1000)


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, max_length=1000)


class AttendanceRecordPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    work_date: date
    shift_id: UUID | None
    check_in_at: datetime | None
    check_out_at: datetime | None
    status: AttendanceStatus
    source: AttendanceSource
    worked_minutes: int | None
    late_minutes: int
    early_leave_minutes: int
    overtime_minutes: int
    notes: str | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.check_in_at is not None and self.check_out_at is None


class AttendanceHistoryResponse(BaseModel):
    items: list[AttendanceRecordPublic]
    total: int
    limit: int
    offset: int

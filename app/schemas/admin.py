"""Request/response schemas for the admin directory APIs.

Every payload forbids unknown keys: an admin typo like ``grace_minutes``
should be a 422, not a silently ignored field that leaves a shift mis-scored.
"""

from __future__ import annotations

from datetime import date, time
from typing import Annotated, Self
from uuid import UUID
from zoneinfo import available_timezones

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Code = Annotated[str, Field(min_length=2, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Description = Annotated[str | None, Field(default=None, max_length=2000)]
WorkingDays = Annotated[list[int], Field(min_length=0, max_length=7)]


def _normalise_code(value: str) -> str:
    return value.strip().upper()


def _validate_working_days(value: list[int]) -> list[int]:
    """ISO weekday numbers, 1 = Monday .. 7 = Sunday, no duplicates.

    An empty list is allowed and means "every day", matching
    ``attendance_rules.is_working_day``.
    """
    if any(day < 1 or day > 7 for day in value):
        raise ValueError("working_days must contain ISO weekdays 1-7")
    if len(set(value)) != len(value):
        raise ValueError("working_days must not contain duplicates")
    return sorted(value)


def _validate_timezone(value: str) -> str:
    """Reject unknown zones at the edge.

    The rules layer degrades a bad zone to UTC so nobody is blocked from
    clocking in, but accepting one here would mean silently scoring a whole
    shift in the wrong zone, so writes are strict where reads are forgiving.
    """
    if value not in available_timezones():
        raise ValueError(f"unknown IANA timezone: {value!r}")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _AtLeastOneField(_Strict):
    """A PATCH body that sets nothing is a mistake, not a no-op."""

    @model_validator(mode="after")
    def _require_a_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self


# ------------------------------------------------------------- departments
class DepartmentCreate(_Strict):
    name: Name
    code: Code
    description: Description
    head_id: UUID | None = None

    _norm_code = field_validator("code")(_normalise_code)


class DepartmentUpdate(_AtLeastOneField):
    name: Name | None = None
    code: Code | None = None
    description: Description
    head_id: UUID | None = None

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str | None) -> str | None:
        return _normalise_code(v) if v is not None else None


class DepartmentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    code: str
    description: str | None
    head_id: UUID | None
    is_active: bool


# ------------------------------------------------------------------ shifts
class ShiftCreate(_Strict):
    name: Name
    code: Code
    description: Description
    timezone: str = "UTC"
    start_time: time
    end_time: time
    is_overnight: bool = False
    break_minutes: int = Field(default=60, ge=0, le=1440)
    grace_period_minutes: int = Field(default=15, ge=0, le=1440)
    half_day_threshold_minutes: int = Field(default=240, gt=0, le=1440)
    working_days: WorkingDays = [1, 2, 3, 4, 5]

    _norm_code = field_validator("code")(_normalise_code)
    _check_days = field_validator("working_days")(_validate_working_days)
    _check_tz = field_validator("timezone")(_validate_timezone)

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        validate_shift_window(self.start_time, self.end_time, self.is_overnight)
        return self


class ShiftUpdate(_AtLeastOneField):
    """Partial update.

    ``start_time``/``end_time``/``is_overnight`` are checked for consistency in
    the service, against the merged result rather than the patch alone: moving
    only ``end_time`` can still invert a window.
    """

    name: Name | None = None
    code: Code | None = None
    description: Description
    timezone: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    is_overnight: bool | None = None
    break_minutes: int | None = Field(default=None, ge=0, le=1440)
    grace_period_minutes: int | None = Field(default=None, ge=0, le=1440)
    half_day_threshold_minutes: int | None = Field(default=None, gt=0, le=1440)
    working_days: WorkingDays | None = None

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str | None) -> str | None:
        return _normalise_code(v) if v is not None else None

    @field_validator("working_days")
    @classmethod
    def _check_days(cls, v: list[int] | None) -> list[int] | None:
        return _validate_working_days(v) if v is not None else None

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        return _validate_timezone(v) if v is not None else None


class ShiftPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    code: str
    description: str | None
    timezone: str
    start_time: time
    end_time: time
    is_overnight: bool
    break_minutes: int
    grace_period_minutes: int
    half_day_threshold_minutes: int
    working_days: list[int]
    is_active: bool


# ------------------------------------------------------------- assignments
class AssignmentCreate(_Strict):
    user_id: UUID
    shift_id: UUID
    effective_from: date
    effective_to: date | None = None
    is_primary: bool = True

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not be before effective_from")
        return self


class AssignmentUpdate(_AtLeastOneField):
    """Only the end date and primary flag may change.

    Repointing an assignment at a different shift is deliberately not offered:
    the honest way to move someone is to close the current assignment and open
    a new one, which leaves the history showing what was actually in force.
    """

    effective_to: date | None = None
    is_primary: bool | None = None


class AssignmentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    shift_id: UUID
    effective_from: date
    effective_to: date | None
    is_primary: bool


def validate_shift_window(start: time, end: time, is_overnight: bool) -> None:
    """A shift window must be unambiguous.

    ``is_overnight`` is stored rather than inferred, so the two must agree:
    22:00-06:00 is only coherent as an overnight shift, and 09:00-18:00 is only
    coherent as a same-day one.
    """
    if is_overnight:
        if end > start:
            raise ValueError(
                "an overnight shift must end at or before its start time, "
                "since the end falls on the following day"
            )
    elif end <= start:
        raise ValueError(
            "end_time must be after start_time; set is_overnight for a shift "
            "that runs past midnight"
        )

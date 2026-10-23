"""Admin attendance reporting and analytics.

Like the directory routes, every endpoint takes ``CurrentAdmin`` -- the single
centralized dependency that resolves the token, reloads the user, rejects
inactive accounts and requires the admin role. Reports expose other people's
attendance, so there is no employee-facing variant here; an employee reads
their own data through ``/attendance/me``.

All arithmetic lives in ``services.reports``. These handlers only validate
input, call it, and project the result.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.deps import CurrentAdmin, SessionDep
from app.schemas.common import Page
from app.schemas.reports import (
    AbsenteeismPublic,
    DailySummaryPublic,
    EmployeeDayPublic,
    Granularity,
    TrendPointPublic,
)
from app.services import audit as audit_service
from app.services import reports

router = APIRouter(prefix="/reports", tags=["reports"])


async def _note_access(
    session: SessionDep, admin, request: Request, report: str, **params
) -> None:
    """Record who read which report, with which filters.

    Reports expose other people's attendance, so the read itself is an
    auditable event, not just the writes.
    """
    await audit_service.record_report_access(
        session,
        admin,
        report=report,
        parameters={k: str(v) for k, v in params.items() if v is not None},
        ip=request.client.host if request.client else None,
    )


class GroupBy(StrEnum):
    DEPARTMENT = "department"
    EMPLOYEE = "employee"


StartQuery = Query(..., description="Inclusive first day of the range")
EndQuery = Query(..., description="Inclusive last day of the range")


async def _grid(
    session: SessionDep,
    start: date,
    end: date,
    *,
    department_id: UUID | None = None,
    user_id: UUID | None = None,
) -> reports.Grid:
    """Build the grid, turning range errors into 422s."""
    try:
        return await reports.build_grid(
            session,
            start,
            end,
            department_id=department_id,
            user_ids=[user_id] if user_id is not None else None,
        )
    except reports.InvalidDateRangeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def _page(rows: list, projector, limit: int, offset: int) -> Page:
    items, total = reports.paginate(rows, limit, offset)
    return Page(
        items=[projector.model_validate(row) for row in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/daily",
    response_model=DailySummaryPublic,
    summary="Attendance summary for a single day",
)
async def daily_summary(
    admin: CurrentAdmin,
    session: SessionDep,
    request: Request,
    on: date = Query(..., description="The day to summarise"),
    department_id: UUID | None = Query(None),
) -> DailySummaryPublic:
    """Zero-filled when nobody was expected, rather than 404.

    "No one was scheduled" is a real answer to "how did Sunday go", and a
    dashboard tile should not have to special-case an error to show it.
    """
    grid = await _grid(session, on, on, department_id=department_id)
    await _note_access(
        session, admin, request, "daily", on=on, department_id=department_id
    )
    return DailySummaryPublic.model_validate(reports.single_day_summary(grid, on))


@router.get(
    "/daily-summaries",
    response_model=Page[DailySummaryPublic],
    summary="Per-day attendance summaries over a range",
)
async def daily_summaries(
    admin: CurrentAdmin,
    session: SessionDep,
    request: Request,
    start: date = StartQuery,
    end: date = EndQuery,
    department_id: UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[DailySummaryPublic]:
    grid = await _grid(session, start, end, department_id=department_id)
    await _note_access(
        session,
        admin,
        request,
        "daily-summaries",
        start=start,
        end=end,
        department_id=department_id,
    )
    return _page(reports.daily_summaries(grid), DailySummaryPublic, limit, offset)


@router.get(
    "/trends",
    response_model=Page[TrendPointPublic],
    summary="Attendance trend bucketed by day, week or month",
)
async def trends(
    admin: CurrentAdmin,
    session: SessionDep,
    request: Request,
    start: date = StartQuery,
    end: date = EndQuery,
    granularity: Granularity = Query(Granularity.DAY),
    department_id: UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=400),
    offset: int = Query(0, ge=0),
) -> Page[TrendPointPublic]:
    grid = await _grid(session, start, end, department_id=department_id)
    points = reports.attendance_trend(grid, granularity.value)
    await _note_access(
        session,
        admin,
        request,
        "trends",
        start=start,
        end=end,
        granularity=granularity.value,
        department_id=department_id,
    )
    return _page(points, TrendPointPublic, limit, offset)


@router.get(
    "/late-arrivals",
    response_model=Page[EmployeeDayPublic],
    summary="Late arrivals over a range",
)
async def late_arrivals(
    admin: CurrentAdmin,
    session: SessionDep,
    request: Request,
    start: date = StartQuery,
    end: date = EndQuery,
    department_id: UUID | None = Query(None),
    user_id: UUID | None = Query(None),
    min_late_minutes: int = Query(1, ge=1, le=1440),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[EmployeeDayPublic]:
    grid = await _grid(
        session, start, end, department_id=department_id, user_id=user_id
    )
    rows = reports.late_arrivals(grid, min_late_minutes=min_late_minutes)
    await _note_access(
        session,
        admin,
        request,
        "late-arrivals",
        start=start,
        end=end,
        department_id=department_id,
        user_id=user_id,
    )
    return _page(rows, EmployeeDayPublic, limit, offset)


@router.get(
    "/absences",
    response_model=Page[EmployeeDayPublic],
    summary="Derived absences over a range",
)
async def absences(
    admin: CurrentAdmin,
    session: SessionDep,
    request: Request,
    start: date = StartQuery,
    end: date = EndQuery,
    department_id: UUID | None = Query(None),
    user_id: UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[EmployeeDayPublic]:
    """An absence is an expected working day with no record and no approved leave."""
    grid = await _grid(
        session, start, end, department_id=department_id, user_id=user_id
    )
    await _note_access(
        session,
        admin,
        request,
        "absences",
        start=start,
        end=end,
        department_id=department_id,
        user_id=user_id,
    )
    return _page(reports.absences(grid), EmployeeDayPublic, limit, offset)


@router.get(
    "/absenteeism",
    response_model=Page[AbsenteeismPublic],
    summary="Absenteeism grouped by department or employee",
)
async def absenteeism(
    admin: CurrentAdmin,
    session: SessionDep,
    request: Request,
    start: date = StartQuery,
    end: date = EndQuery,
    group_by: GroupBy = Query(GroupBy.DEPARTMENT),
    department_id: UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[AbsenteeismPublic]:
    grid = await _grid(session, start, end, department_id=department_id)
    rows = (
        reports.absenteeism_by_department(grid)
        if group_by is GroupBy.DEPARTMENT
        else reports.absenteeism_by_employee(grid)
    )
    await _note_access(
        session,
        admin,
        request,
        "absenteeism",
        start=start,
        end=end,
        group_by=group_by.value,
        department_id=department_id,
    )
    return _page(rows, AbsenteeismPublic, limit, offset)

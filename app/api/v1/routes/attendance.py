"""Employee attendance workflows.

Every route is scoped to the authenticated user. None of them accept a user id,
so there is no cross-user surface to guard: identity comes from the token.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.deps import CurrentUser, SessionDep
from app.schemas.attendance import (
    AttendanceHistoryResponse,
    AttendanceRecordPublic,
    PunchInRequest,
    PunchOutRequest,
)
from app.services import attendance as attendance_service

router = APIRouter(prefix="/attendance", tags=["attendance"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/punch-in",
    response_model=AttendanceRecordPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Start the working day",
)
async def punch_in(
    payload: PunchInRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> AttendanceRecordPublic:
    try:
        record = await attendance_service.punch_in(
            session,
            user,
            source=payload.source,
            ip=_client_ip(request),
            latitude=payload.latitude,
            longitude=payload.longitude,
            notes=payload.notes,
        )
    except attendance_service.AlreadyPunchedInError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already punched in for today",
        ) from exc
    except attendance_service.AlreadyPunchedOutError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Attendance for today is already complete",
        ) from exc
    except attendance_service.NotAWorkingDayError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Today is not a working day for your assigned shift",
        ) from exc
    except attendance_service.NoActiveShiftError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="You have no active shift assignment",
        ) from exc

    return AttendanceRecordPublic.model_validate(record)


@router.post(
    "/punch-out",
    response_model=AttendanceRecordPublic,
    summary="End the working day",
)
async def punch_out(
    payload: PunchOutRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> AttendanceRecordPublic:
    try:
        record = await attendance_service.punch_out(
            session, user, ip=_client_ip(request), notes=payload.notes
        )
    except attendance_service.AlreadyPunchedOutError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already punched out for that day",
        ) from exc
    except attendance_service.NotPunchedInError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have no open punch-in to close",
        ) from exc
    except attendance_service.NoActiveShiftError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The open record has no shift to evaluate against",
        ) from exc

    return AttendanceRecordPublic.model_validate(record)


@router.get(
    "/me",
    response_model=AttendanceRecordPublic,
    summary="Today's attendance record",
)
async def read_today(user: CurrentUser, session: SessionDep) -> AttendanceRecordPublic:
    record = await attendance_service.get_today(session, user)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No attendance record for today",
        )
    return AttendanceRecordPublic.model_validate(record)


@router.get(
    "/me/history",
    response_model=AttendanceHistoryResponse,
    summary="Own attendance history",
)
async def read_history(
    user: CurrentUser,
    session: SessionDep,
    start: date | None = Query(None, description="Inclusive lower bound"),
    end: date | None = Query(None, description="Inclusive upper bound"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AttendanceHistoryResponse:
    if start and end and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must not be after end",
        )

    records, total = await attendance_service.list_history(
        session, user, start=start, end=end, limit=limit, offset=offset
    )
    return AttendanceHistoryResponse(
        items=[AttendanceRecordPublic.model_validate(r) for r in records],
        total=total,
        limit=limit,
        offset=offset,
    )

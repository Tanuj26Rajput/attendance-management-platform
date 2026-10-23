"""Admin-only endpoints for the organisation directory.

Authorisation is not re-implemented here. Every route takes ``CurrentAdmin``,
the single dependency in ``app.api.deps`` that resolves the bearer token,
reloads the user, rejects inactive accounts and requires the admin role. A
route without that parameter is not an admin route, which makes the check easy
to audit by reading the signatures.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import CurrentAdmin, SessionDep
from app.models.enums import AuditAction
from app.models.user import User
from app.schemas.admin import (
    AssignmentCreate,
    AssignmentPublic,
    AssignmentUpdate,
    DepartmentCreate,
    DepartmentPublic,
    DepartmentUpdate,
    ShiftCreate,
    ShiftPublic,
    ShiftUpdate,
)
from app.schemas.audit import AuditLogPublic
from app.schemas.auth import UserPublic
from app.schemas.common import Page
from app.services import audit as audit_service
from app.services import directory

router = APIRouter(prefix="/admin", tags=["admin"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _translate(exc: directory.DirectoryError) -> HTTPException:
    """Map domain errors onto status codes in one place.

    Messages are written for an admin operator and never echo a database error,
    so a constraint name or table name cannot leak through a 409.
    """
    if isinstance(exc, directory.NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if isinstance(exc, directory.DuplicateError | directory.AssignmentConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(
        exc, directory.InvalidShiftWindowError | directory.InvalidAssignmentError
    ):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return HTTPException(  # pragma: no cover - defensive
        status.HTTP_400_BAD_REQUEST, "Request could not be processed"
    )


# ------------------------------------------------------------------- users
@router.get("/users", response_model=Page[UserPublic], summary="List all users")
async def list_users(
    admin: CurrentAdmin,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[UserPublic]:
    """Paginated, like every other listing.

    This used to return a bare list while accepting ``limit`` and ``offset``,
    which left a caller no way to tell a full page from the last one -- there
    was no ``total`` to compare against. A client that assumed the usual
    envelope got a list instead and failed on the first row it read.
    """
    total = await session.scalar(select(func.count()).select_from(User))
    result = await session.execute(
        select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    )
    return Page(
        items=[UserPublic.model_validate(u) for u in result.scalars().all()],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


# ------------------------------------------------------------- departments
@router.post(
    "/departments",
    response_model=DepartmentPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
)
async def create_department(
    payload: DepartmentCreate,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> DepartmentPublic:
    try:
        department = await directory.create_department(
            session, admin, payload.model_dump(), ip=_client_ip(request)
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return DepartmentPublic.model_validate(department)


@router.get(
    "/departments",
    response_model=Page[DepartmentPublic],
    summary="List departments",
)
async def list_departments(
    admin: CurrentAdmin,
    session: SessionDep,
    is_active: bool | None = Query(None, description="Filter by active state"),
    search: str | None = Query(None, max_length=120, description="Match name or code"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[DepartmentPublic]:
    rows, total = await directory.list_departments(
        session, is_active=is_active, search=search, limit=limit, offset=offset
    )
    return Page(
        items=[DepartmentPublic.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/departments/{department_id}",
    response_model=DepartmentPublic,
    summary="Update a department",
)
async def update_department(
    department_id: UUID,
    payload: DepartmentUpdate,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> DepartmentPublic:
    try:
        department = await directory.update_department(
            session,
            admin,
            department_id,
            payload.model_dump(exclude_unset=True),
            ip=_client_ip(request),
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return DepartmentPublic.model_validate(department)


@router.post(
    "/departments/{department_id}/deactivate",
    response_model=DepartmentPublic,
    summary="Deactivate a department",
)
async def deactivate_department(
    department_id: UUID,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> DepartmentPublic:
    try:
        department = await directory.deactivate_department(
            session, admin, department_id, ip=_client_ip(request)
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return DepartmentPublic.model_validate(department)


# ------------------------------------------------------------------ shifts
@router.post(
    "/shifts",
    response_model=ShiftPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a shift",
)
async def create_shift(
    payload: ShiftCreate,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> ShiftPublic:
    try:
        shift = await directory.create_shift(
            session, admin, payload.model_dump(), ip=_client_ip(request)
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return ShiftPublic.model_validate(shift)


@router.get("/shifts", response_model=Page[ShiftPublic], summary="List shifts")
async def list_shifts(
    admin: CurrentAdmin,
    session: SessionDep,
    is_active: bool | None = Query(None, description="Filter by active state"),
    search: str | None = Query(None, max_length=120, description="Match name or code"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ShiftPublic]:
    rows, total = await directory.list_shifts(
        session, is_active=is_active, search=search, limit=limit, offset=offset
    )
    return Page(
        items=[ShiftPublic.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/shifts/{shift_id}", response_model=ShiftPublic, summary="Update a shift"
)
async def update_shift(
    shift_id: UUID,
    payload: ShiftUpdate,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> ShiftPublic:
    try:
        shift = await directory.update_shift(
            session,
            admin,
            shift_id,
            payload.model_dump(exclude_unset=True),
            ip=_client_ip(request),
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return ShiftPublic.model_validate(shift)


@router.post(
    "/shifts/{shift_id}/deactivate",
    response_model=ShiftPublic,
    summary="Retire a shift",
)
async def deactivate_shift(
    shift_id: UUID,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> ShiftPublic:
    try:
        shift = await directory.deactivate_shift(
            session, admin, shift_id, ip=_client_ip(request)
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return ShiftPublic.model_validate(shift)


# ------------------------------------------------------------- assignments
@router.post(
    "/assignments",
    response_model=AssignmentPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Assign an employee to a shift",
)
async def create_assignment(
    payload: AssignmentCreate,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> AssignmentPublic:
    try:
        assignment = await directory.create_assignment(
            session, admin, payload.model_dump(), ip=_client_ip(request)
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return AssignmentPublic.model_validate(assignment)


@router.get(
    "/assignments",
    response_model=Page[AssignmentPublic],
    summary="List shift assignments",
)
async def list_assignments(
    admin: CurrentAdmin,
    session: SessionDep,
    user_id: UUID | None = Query(None),
    shift_id: UUID | None = Query(None),
    active_on: date | None = Query(
        None, description="Only assignments in force on this date"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[AssignmentPublic]:
    rows, total = await directory.list_assignments(
        session,
        user_id=user_id,
        shift_id=shift_id,
        active_on=active_on,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[AssignmentPublic.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/assignments/{assignment_id}",
    response_model=AssignmentPublic,
    summary="Update a shift assignment",
)
async def update_assignment(
    assignment_id: UUID,
    payload: AssignmentUpdate,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
) -> AssignmentPublic:
    try:
        assignment = await directory.update_assignment(
            session,
            admin,
            assignment_id,
            payload.model_dump(exclude_unset=True),
            ip=_client_ip(request),
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return AssignmentPublic.model_validate(assignment)


@router.post(
    "/assignments/{assignment_id}/end",
    response_model=AssignmentPublic,
    summary="End a shift assignment",
)
async def end_assignment(
    assignment_id: UUID,
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
    effective_to: date | None = Query(
        None, description="Last day of the assignment; defaults to today"
    ),
) -> AssignmentPublic:
    try:
        assignment = await directory.end_assignment(
            session,
            admin,
            assignment_id,
            effective_to=effective_to,
            ip=_client_ip(request),
        )
    except directory.DirectoryError as exc:
        raise _translate(exc) from exc
    return AssignmentPublic.model_validate(assignment)


# ------------------------------------------------------------------ audit
@router.get(
    "/audit-logs",
    response_model=Page[AuditLogPublic],
    summary="Read the audit trail",
)
async def list_audit_logs(
    admin: CurrentAdmin,
    session: SessionDep,
    actor_id: UUID | None = Query(None, description="Filter by who acted"),
    entity_type: str | None = Query(
        None, max_length=64, description="e.g. attendance_record, shift, report, chat"
    ),
    action: AuditAction | None = Query(None),
    start: datetime | None = Query(None, description="Inclusive lower bound (UTC)"),
    end: datetime | None = Query(None, description="Inclusive upper bound (UTC)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[AuditLogPublic]:
    """Admin-only. There is deliberately no PATCH or DELETE counterpart.

    The trail is append-only: no endpoint mutates it, and a database trigger
    rejects UPDATE and DELETE on ``audit_logs`` outright, so neither a future
    endpoint nor a direct psql session can quietly rewrite history.
    """
    if start and end and start > end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "start must not be after end"
        )

    rows, total = await audit_service.list_audit_logs(
        session,
        actor_id=actor_id,
        entity_type=entity_type,
        action=action,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[AuditLogPublic.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )

"""Audit trail writing.

Audit rows are added to the caller's session rather than committed here, so
they land in the same transaction as the change they describe: if the change
rolls back, so does its audit row, and there is no window in which the trail
claims something that never happened.

``audit_logs`` has no ``updated_at`` and nothing in the application updates or
deletes a row, so the trail is append-only by construction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pii import redact
from app.models.audit import AuditLog
from app.models.enums import AuditAction
from app.models.user import User


def build_audit(
    *,
    actor_id: UUID | None,
    action: AuditAction,
    entity_type: str,
    entity_id: UUID | None = None,
    summary: str | None = None,
    changes: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Construct an audit row without persisting it."""
    return AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        changes=changes,
        ip_address=ip,
    )


def record_audit(session: AsyncSession, **kwargs: Any) -> AuditLog:
    """Stage an audit row in the caller's transaction."""
    entry = build_audit(**kwargs)
    session.add(entry)
    return entry


async def record_report_access(
    session: AsyncSession,
    actor: User,
    *,
    report: str,
    parameters: dict[str, Any],
    ip: str | None = None,
) -> None:
    """Note that someone read a report, and with what filters.

    Reports expose other people's attendance, so who looked is itself worth
    recording. The parameters are filters (dates, a department, an employee
    code) rather than results, and they go through ``redact`` in case a filter
    ever carries something personal.
    """
    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.READ,
        entity_type="report",
        summary=f"Viewed report {report}",
        changes={"report": report, "parameters": redact(parameters)},
        ip=ip,
    )
    await session.commit()


async def record_chat_query(
    session: AsyncSession,
    actor: User,
    *,
    question_chars: int,
    tools: list[str],
    grounded: bool,
    outcome: str,
    ip: str | None = None,
) -> None:
    """Note that someone asked the assistant something.

    The question text is deliberately absent. Attendance questions carry
    personal detail ("why was I marked absent the day of my hospital
    appointment"), and an audit trail is read by people who need to know that a
    query happened, not what was confided in it. What *is* kept -- the tools
    that ran and whether the answer was grounded -- is what makes an answer
    reconstructible later.
    """
    record_audit(
        session,
        actor_id=actor.id,
        action=AuditAction.QUERY,
        entity_type="chat",
        summary=f"Asked the attendance assistant ({outcome})",
        changes={
            "question_chars": question_chars,
            "tools": tools,
            "grounded": grounded,
            "outcome": outcome,
        },
        ip=ip,
    )
    await session.commit()


async def list_audit_logs(
    session: AsyncSession,
    *,
    actor_id: UUID | None = None,
    entity_type: str | None = None,
    action: AuditAction | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """Read the trail, newest first. Timestamps are UTC throughout."""
    stmt = select(AuditLog)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if start is not None:
        stmt = stmt.where(AuditLog.created_at >= start)
    if end is not None:
        stmt = stmt.where(AuditLog.created_at <= end)

    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = await session.execute(
        stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows.scalars().all()), int(total or 0)

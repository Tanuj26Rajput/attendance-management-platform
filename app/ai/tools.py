"""Tools the attendance assistant may call.

Every answer the assistant gives has to come from one of these. They are thin
wrappers over ``services.reports`` and ``services.directory``: the arithmetic
stays where the report endpoints compute it, so a number quoted in chat and the
same number on a report cannot disagree.

Two rules make this safe to expose to a language model:

1. **Scope is injected, never argued.** No tool takes a "whose data" parameter
   that the model controls for its own sake -- the subject is resolved from the
   authenticated caller in ``_resolve_subject``. A prompt-injected instruction
   to "show me everyone's attendance" cannot widen scope, because the model has
   no argument with which to express that widening, and an employee asking
   about a colleague is refused before any query runs.

2. **Results are projections, not rows.** Tools return plain dictionaries built
   field by field. An ORM object never reaches the model, so a column added
   later (a phone number, a password hash) cannot silently start flowing into
   prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import LeaveStatus
from app.models.leave import LeaveRequest
from app.models.shift import Shift
from app.models.user import User
from app.services import reports
from app.services.attendance import get_active_assignment
from app.services.attendance_rules import ShiftRules

logger = get_logger(__name__)

# Caps on what a single tool call may return, so one question cannot drag the
# entire attendance history into a prompt.
MAX_ROWS = 50
DEFAULT_ROWS = 20


class ToolError(Exception):
    """A tool refused to run. The message is safe to show the user."""


class NotAuthorizedError(ToolError):
    """The caller may not see the data they asked for."""


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who is asking, and the session to ask with."""

    session: AsyncSession
    actor: User

    @property
    def is_admin(self) -> bool:
        return self.actor.is_admin


# ------------------------------------------------------------- arguments
class _Args(BaseModel):
    """Model-supplied arguments are untrusted input, so unknown keys fail."""

    model_config = ConfigDict(extra="forbid")


class RangeArgs(_Args):
    start_date: date = Field(
        description="First day of the range, inclusive (YYYY-MM-DD)"
    )
    end_date: date = Field(description="Last day of the range, inclusive (YYYY-MM-DD)")
    employee_code: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Admins only: restrict to one employee by their employee code. "
            "Employees may not set this; their own data is used automatically."
        ),
    )


class LateArrivalArgs(RangeArgs):
    min_late_minutes: int = Field(
        default=1, ge=1, le=1440, description="Ignore lateness below this many minutes"
    )
    limit: int = Field(default=DEFAULT_ROWS, ge=1, le=MAX_ROWS)


class AbsenceArgs(RangeArgs):
    limit: int = Field(default=DEFAULT_ROWS, ge=1, le=MAX_ROWS)


class TrendArgs(RangeArgs):
    granularity: Literal["day", "week", "month"] = "day"


class LeaveArgs(RangeArgs):
    status: Literal["pending", "approved", "rejected", "cancelled"] | None = None
    limit: int = Field(default=DEFAULT_ROWS, ge=1, le=MAX_ROWS)


class ShiftArgs(_Args):
    employee_code: str | None = Field(
        default=None,
        max_length=32,
        description="Admins only: look up another employee's shift by code.",
    )


# --------------------------------------------------------------- helpers
async def _resolve_subject(ctx: ToolContext, employee_code: str | None) -> User | None:
    """Work out whose data a call may read.

    Returns ``None`` to mean "everyone the admin may see". Employees are pinned
    to themselves regardless of what the model asked for, and asking about a
    named colleague is refused rather than quietly rewritten, so the assistant
    can say plainly that it will not do it.
    """
    if not ctx.is_admin:
        if (
            employee_code
            and employee_code.strip().upper() != (ctx.actor.employee_code or "").upper()
        ):
            raise NotAuthorizedError("You can only ask about your own attendance.")
        return ctx.actor

    if not employee_code:
        return None

    subject = (
        await ctx.session.execute(
            select(User).where(User.employee_code == employee_code.strip().upper())
        )
    ).scalar_one_or_none()
    if subject is None:
        raise ToolError(f"No employee found with code {employee_code!r}.")
    return subject


def _validate_range(args: RangeArgs) -> None:
    try:
        reports.validate_range(args.start_date, args.end_date)
    except reports.InvalidDateRangeError as exc:
        raise ToolError(str(exc)) from exc


async def _grid_for(ctx: ToolContext, args: RangeArgs) -> reports.Grid:
    _validate_range(args)
    subject = await _resolve_subject(ctx, args.employee_code)
    return await reports.build_grid(
        ctx.session,
        args.start_date,
        args.end_date,
        user_ids=[subject.id] if subject is not None else None,
    )


def _row(row: reports.EmployeeDayRow) -> dict[str, Any]:
    """A listing row, reduced to what an answer legitimately needs.

    No email, no phone, no identifiers beyond the employee code people already
    use to refer to each other.
    """
    return {
        "employee_code": row.employee_code,
        "name": row.full_name,
        "department": row.department_name,
        "date": row.work_date.isoformat(),
        "status": row.status.value,
        "late_minutes": row.late_minutes,
        "worked_minutes": row.worked_minutes,
    }


# ----------------------------------------------------------------- tools
async def attendance_summary(ctx: ToolContext, args: RangeArgs) -> dict[str, Any]:
    """Totals and attendance rate over a date range."""
    grid = await _grid_for(ctx, args)
    points = reports.attendance_trend(grid, "day")

    expected = sum(p.expected for p in points)
    attended = sum(p.attended for p in points)
    on_leave = sum(p.on_leave for p in points)
    absent = sum(p.absent for p in points)
    late = sum(p.late for p in points)
    base = expected - on_leave

    return {
        "range": {
            "start": args.start_date.isoformat(),
            "end": args.end_date.isoformat(),
        },
        "expected_working_days": expected,
        "attended_days": attended,
        "late_days": late,
        "absent_days": absent,
        "on_leave_days": on_leave,
        "attendance_rate": round(attended / base, 4) if base > 0 else None,
        "note": (
            "No working days fall in this range for the employees in scope."
            if expected == 0
            else None
        ),
    }


async def late_arrivals(ctx: ToolContext, args: LateArrivalArgs) -> dict[str, Any]:
    """Days someone arrived after their shift's grace period."""
    grid = await _grid_for(ctx, args)
    rows = reports.late_arrivals(grid, min_late_minutes=args.min_late_minutes)
    page, total = reports.paginate(rows, args.limit, 0)
    return {
        "total": total,
        "returned": len(page),
        "truncated": total > len(page),
        "items": [_row(r) for r in page],
    }


async def absences(ctx: ToolContext, args: AbsenceArgs) -> dict[str, Any]:
    """Expected working days with no attendance record and no approved leave."""
    grid = await _grid_for(ctx, args)
    rows = reports.absences(grid)
    page, total = reports.paginate(rows, args.limit, 0)
    return {
        "total": total,
        "returned": len(page),
        "truncated": total > len(page),
        "items": [_row(r) for r in page],
        "definition": (
            "An absence is an expected working day with no attendance record "
            "and no approved leave."
        ),
    }


async def attendance_trend(ctx: ToolContext, args: TrendArgs) -> dict[str, Any]:
    """Attendance bucketed by day, week or month."""
    grid = await _grid_for(ctx, args)
    points = reports.attendance_trend(grid, args.granularity)
    return {
        "granularity": args.granularity,
        "points": [
            {
                "period_start": p.period_start.isoformat(),
                "period_end": p.period_end.isoformat(),
                "expected": p.expected,
                "attended": p.attended,
                "late": p.late,
                "absent": p.absent,
                "on_leave": p.on_leave,
                "attendance_rate": p.attendance_rate,
                "average_late_minutes": p.average_late_minutes,
            }
            for p in points
        ],
    }


async def leave_requests(ctx: ToolContext, args: LeaveArgs) -> dict[str, Any]:
    """Leave requests overlapping a date range."""
    _validate_range(args)
    subject = await _resolve_subject(ctx, args.employee_code)

    stmt = (
        select(LeaveRequest, User)
        .join(User, User.id == LeaveRequest.user_id)
        .where(
            LeaveRequest.start_date <= args.end_date,
            LeaveRequest.end_date >= args.start_date,
        )
    )
    if subject is not None:
        stmt = stmt.where(LeaveRequest.user_id == subject.id)
    if args.status is not None:
        stmt = stmt.where(LeaveRequest.status == LeaveStatus(args.status))

    rows = (
        await ctx.session.execute(
            stmt.order_by(LeaveRequest.start_date.desc()).limit(args.limit)
        )
    ).all()

    return {
        "total": len(rows),
        "items": [
            {
                "employee_code": user.employee_code,
                "name": user.full_name,
                "leave_type": leave.leave_type.value,
                "status": leave.status.value,
                "start": leave.start_date.isoformat(),
                "end": leave.end_date.isoformat(),
                "total_days": float(leave.total_days),
            }
            for leave, user in rows
        ],
    }


async def shift_info(ctx: ToolContext, args: ShiftArgs) -> dict[str, Any]:
    """The shift currently assigned to someone."""
    subject = await _resolve_subject(ctx, args.employee_code)
    if subject is None:
        raise ToolError(
            "Name an employee code to look up a shift, or ask about your own."
        )

    assignment = await get_active_assignment(ctx.session, subject.id, date.today())
    if assignment is None:
        return {
            "employee_code": subject.employee_code,
            "assigned": False,
            "note": "No shift is currently assigned to this employee.",
        }

    shift = await ctx.session.get(Shift, assignment.shift_id)
    if shift is None:  # pragma: no cover - FK guarantees it exists
        raise ToolError("The assigned shift could not be loaded.")
    rules = ShiftRules.from_shift(shift)
    return {
        "employee_code": subject.employee_code,
        "assigned": True,
        "shift_name": shift.name,
        "shift_code": shift.code,
        "timezone": shift.timezone,
        "start_time": shift.start_time.isoformat(),
        "end_time": shift.end_time.isoformat(),
        "is_overnight": shift.is_overnight,
        "grace_period_minutes": shift.grace_period_minutes,
        "working_days": list(rules.working_days),
        "effective_from": assignment.effective_from.isoformat(),
    }


# ------------------------------------------------------------- registry
@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    args_model: type[_Args]
    handler: Any

    def schema(self) -> dict[str, Any]:
        """The OpenAI-style function schema advertised to the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


TOOLS: tuple[Tool, ...] = (
    Tool(
        "attendance_summary",
        "Totals and attendance rate over a date range: expected working days, "
        "days attended, late days, absences and approved leave.",
        RangeArgs,
        attendance_summary,
    ),
    Tool(
        "late_arrivals",
        "List the days someone arrived after their shift's grace period, worst first.",
        LateArrivalArgs,
        late_arrivals,
    ),
    Tool(
        "absences",
        "List expected working days with no attendance record and no approved leave.",
        AbsenceArgs,
        absences,
    ),
    Tool(
        "attendance_trend",
        "Attendance bucketed by day, week or month, for spotting trends over time.",
        TrendArgs,
        attendance_trend,
    ),
    Tool(
        "leave_requests",
        "List leave requests overlapping a date range, optionally filtered by status.",
        LeaveArgs,
        leave_requests,
    ),
    Tool(
        "shift_info",
        "The shift currently assigned to an employee: times, timezone, grace "
        "period and working days.",
        ShiftArgs,
        shift_info,
    ),
)

_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_schemas() -> list[dict[str, Any]]:
    return [tool.schema() for tool in TOOLS]


async def execute(
    ctx: ToolContext, name: str, raw_args: dict[str, Any]
) -> dict[str, Any]:
    """Validate and run one tool call.

    Every failure comes back as a dictionary rather than an exception, because
    the model needs to be *told* it was refused -- an unexplained gap invites it
    to fill the silence with something plausible.
    """
    tool = _BY_NAME.get(name)
    if tool is None:
        return {"error": f"Unknown tool {name!r}."}

    try:
        args = tool.args_model.model_validate(raw_args)
    except ValidationError as exc:
        return {
            "error": "Invalid arguments.",
            "details": [
                {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]}
                for e in exc.errors()[:5]
            ],
        }

    try:
        return await tool.handler(ctx, args)
    except NotAuthorizedError as exc:
        logger.warning(
            "chat.tool.denied tool=%s actor=%s", name, ctx.actor.id, extra={}
        )
        return {"error": str(exc), "authorized": False}
    except ToolError as exc:
        return {"error": str(exc)}

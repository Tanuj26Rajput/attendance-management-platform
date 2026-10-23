"""Admin: the audit trail.

Read-only, and only reachable by an admin -- twice over. The navigation never
offers this page to an employee, and the endpoint behind it answers 403
regardless of what the navigation does, which is the check that actually
matters.

The trail is append-only in the database itself, so there is nothing to edit
here and no button that could pretend otherwise.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import streamlit as st

from dashboard import session
from dashboard.components import (
    PAGE_SIZE,
    empty_state,
    format_timestamp,
    paginator,
    reset_page,
    table,
)

KEY = "audit"

ENTITY_TYPES = [
    "attendance_record",
    "user",
    "department",
    "shift",
    "shift_assignment",
    "report",
    "chat",
]

ACTIONS = ["create", "update", "delete", "login", "logout", "read", "query"]


def render() -> None:
    st.title("Audit trail")
    st.caption(
        "Every recorded action, in UTC. Records are append-only -- the "
        "database rejects an update or a delete, so this is evidence rather "
        "than a log."
    )

    client = session.get_client()

    left, middle, right = st.columns(3)
    entity_type = left.selectbox(
        "Entity",
        [None, *ENTITY_TYPES],
        format_func=lambda v: "All" if v is None else v.replace("_", " "),
        key=f"{KEY}_entity",
        on_change=reset_page,
        args=(KEY,),
    )
    action = middle.selectbox(
        "Action",
        [None, *ACTIONS],
        format_func=lambda v: "All" if v is None else v,
        key=f"{KEY}_action",
        on_change=reset_page,
        args=(KEY,),
    )
    days = right.number_input(
        "Days back", min_value=1, max_value=365, value=7, key=f"{KEY}_days"
    )

    # The endpoint takes instants, not dates: a trail entry is a moment.
    start = datetime.combine(date.today() - timedelta(days=int(days) - 1), time.min)
    filters = {
        "entity_type": entity_type,
        "action": action,
        "start": start.isoformat() + "Z",
    }

    head = session.call(client.list_audit_logs, limit=1, **filters)
    total = head["total"] if head else 0
    offset = paginator(KEY, total, limit=PAGE_SIZE)

    data = session.call(
        client.list_audit_logs, limit=PAGE_SIZE, offset=offset, **filters
    )
    if data is None:
        return

    if not data["items"]:
        empty_state(
            "No audit records match these filters.",
            "Widen the date range, or clear the entity and action filters.",
        )
        return

    table(
        [
            {
                **row,
                "when": format_timestamp(row.get("created_at")),
                "entity": (row.get("entity_type") or "").replace("_", " "),
                # changes is already redacted server-side; it is rendered as
                # text rather than parsed, so nothing here can re-interpret it.
                "detail": _summarise(row),
            }
            for row in data["items"]
        ],
        {
            "when": "When (UTC)",
            "actor_id": "Actor",
            "action": "Action",
            "entity": "Entity",
            "detail": "Detail",
            "ip_address": "IP",
        },
    )


def _summarise(row: dict) -> str:
    """One readable cell from the summary and the recorded change set."""
    summary = row.get("summary") or ""
    changes = row.get("changes") or {}
    if not changes:
        return summary or "--"
    rendered = ", ".join(f"{k}={v}" for k, v in list(changes.items())[:4])
    return f"{summary} ({rendered})" if summary else rendered

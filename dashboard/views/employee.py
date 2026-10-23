"""The employee's own view: today, their history, and their leave.

Everything here is scoped by the token, not by a parameter -- there is no
employee picker, because the endpoints behind this page (``/attendance/me``,
``/attendance/me/history``) answer only for the caller. An employee cannot
widen this page into someone else's.
"""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from dashboard import session
from dashboard.api_client import ApiError
from dashboard.components import (
    PAGE_SIZE,
    date_range_filter,
    empty_state,
    format_minutes,
    format_timestamp,
    humanise_status,
    metric_row,
    page_header,
    paginator,
    punch_state,
    table,
)

HISTORY_KEY = "employee_history"


def render() -> None:
    user = session.current_user() or {}
    st.title(f"Hello, {user.get('full_name', 'there')}")
    st.caption(f"Employee {user.get('employee_code', '--')}")

    today_tab, history_tab, leave_tab = st.tabs(["Today", "History", "Leave"])
    with today_tab:
        _today()
    with history_tab:
        _history()
    with leave_tab:
        _leave()


# ------------------------------------------------------------------ today
def _today() -> None:
    client = session.get_client()
    page_header("Today", "Your punch record for the current business day.")

    record = session.call(client.today)
    state = punch_state(record)

    left, right = st.columns(2)
    if left.button(
        "Punch in",
        type="primary",
        disabled=state != "not_started",
        width="stretch",
    ):
        _punch(client.punch_in, "Punched in.")
    if right.button(
        "Punch out",
        type="primary",
        disabled=state != "open",
        width="stretch",
    ):
        _punch(client.punch_out, "Punched out.")

    if record is None:
        empty_state(
            "No punch recorded for today yet.",
            "Use **Punch in** above to start your day.",
        )
        return

    metric_row(
        [
            ("Status", humanise_status(record.get("status")), None),
            ("Checked in", format_timestamp(record.get("check_in_at")), None),
            (
                "Checked out",
                format_timestamp(record.get("check_out_at"))
                if record.get("check_out_at")
                else "Still in",
                None,
            ),
            (
                "Worked",
                format_minutes(record.get("worked_minutes")),
                "Counted once you punch out.",
            ),
        ]
    )

    if record.get("late_minutes"):
        st.warning(
            f"You arrived {format_minutes(record['late_minutes'])} after your "
            "shift's grace period."
        )
    if record.get("early_leave_minutes"):
        st.warning(f"You left {format_minutes(record['early_leave_minutes'])} early.")
    if record.get("overtime_minutes"):
        st.success(f"Overtime today: {format_minutes(record['overtime_minutes'])}.")


def _punch(action, success: str) -> None:
    """Run a punch, then rerun so every figure on the page reflects it.

    Failures here are ordinary and expected -- no shift assigned, already
    punched in -- so the API's own sentence is shown rather than a generic one.
    """
    with st.spinner("Recording..."):
        try:
            action()
        except ApiError as exc:
            st.error(exc.detail)
            return
    st.success(success)
    st.rerun()


# ---------------------------------------------------------------- history
def _history() -> None:
    client = session.get_client()
    page_header("Attendance history", "Your own records, newest first.")

    chosen = date_range_filter(HISTORY_KEY, default_days=30)
    if chosen is None:
        return
    start, end = chosen

    offset = paginator(HISTORY_KEY, _total(client, start, end), limit=PAGE_SIZE)
    data = session.call(
        client.my_history, start=start, end=end, limit=PAGE_SIZE, offset=offset
    )
    if data is None:
        return

    rows = data["items"]
    if not rows:
        empty_state(
            "No attendance records in this range.",
            "Try widening the dates above.",
        )
        return

    table(
        [
            {
                **row,
                "status_label": humanise_status(row.get("status")),
                "in_at": format_timestamp(row.get("check_in_at")),
                "out_at": format_timestamp(row.get("check_out_at")),
                "worked": format_minutes(row.get("worked_minutes")),
                "late": format_minutes(row.get("late_minutes")),
            }
            for row in rows
        ],
        {
            "work_date": "Date",
            "status_label": "Status",
            "in_at": "In",
            "out_at": "Out",
            "worked": "Worked",
            "late": "Late by",
            "notes": "Notes",
        },
    )


def _total(client, start: date, end: date) -> int:
    """The row count for the pagination caption.

    Asked for with ``limit=1`` because only ``total`` is wanted; the page
    itself is fetched once the offset is known.
    """
    head = session.call(client.my_history, start=start, end=end, limit=1, offset=0)
    return head["total"] if head else 0


# ------------------------------------------------------------------ leave
def _leave() -> None:
    page_header("Leave", "Approved leave already affects the figures above.")
    st.info(
        "This build exposes no REST endpoint for leave requests, so there is "
        "nothing for this page to call. Leave is recorded in the database and "
        "is honoured everywhere it matters -- a day on approved leave is not "
        "counted as an absence in any report -- and the assistant can read it: "
        'ask **Attendance assistant** something like *"what leave do I have '
        'on record this year?"*.'
    )
    st.caption(
        "Applying for and approving leave through the UI needs new API "
        "endpoints; inventing them in the dashboard would put business rules "
        "in the wrong place."
    )
    if st.button("Ask the assistant about my leave"):
        st.session_state["nav"] = "Attendance assistant"
        st.session_state["assistant_prefill"] = (
            f"What leave do I have on record between "
            f"{date.today() - timedelta(days=180)} and {date.today()}?"
        )
        st.rerun()

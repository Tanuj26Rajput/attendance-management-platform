"""Admin: departments, shifts, and who works which shift.

Nothing here deletes. The API offers deactivate and end-dating instead, which
is what keeps past attendance interpretable -- a record scored against a shift
whose row had been deleted could never be explained again -- and this page
offers exactly what the API offers.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

import streamlit as st

from dashboard import session
from dashboard.api_client import ApiError, ValidationFailedError
from dashboard.components import (
    PAGE_SIZE,
    empty_state,
    form_errors,
    page_header,
    paginator,
    reset_page,
    table,
)

WEEKDAYS = {
    1: "Mon",
    2: "Tue",
    3: "Wed",
    4: "Thu",
    5: "Fri",
    6: "Sat",
    7: "Sun",
}

# A short list beats a free-text box for the zones a shift is likely to use;
# the API validates the value regardless.
COMMON_TIMEZONES = [
    "UTC",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Singapore",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Los_Angeles",
    "Australia/Sydney",
]


def render() -> None:
    st.title("Directory")
    departments, shifts, assignments = st.tabs(
        ["Departments", "Shifts", "Shift assignments"]
    )
    with departments:
        _departments()
    with shifts:
        _shifts()
    with assignments:
        _assignments()


def _submit(action, payload: dict[str, Any], success: str) -> None:
    """Run a write, report what the API said, and reload the list on success."""
    try:
        action(payload)
    except ValidationFailedError as exc:
        form_errors(exc.errors or {"_": exc.detail})
        return
    except ApiError as exc:
        st.error(exc.detail)
        return
    st.success(success)
    st.rerun()


# ------------------------------------------------------------ departments
def _departments() -> None:
    client = session.get_client()
    page_header("Departments", "Used to group employees in every report.")

    search = st.text_input(
        "Search", key="dept_search", on_change=reset_page, args=("departments",)
    )
    include_inactive = st.toggle("Include deactivated", key="dept_inactive")

    is_active = None if include_inactive else True
    head = session.call(
        client.list_departments, is_active=is_active, search=search or None, limit=1
    )
    total = head["total"] if head else 0
    offset = paginator("departments", total, limit=PAGE_SIZE)

    data = session.call(
        client.list_departments,
        is_active=is_active,
        search=search or None,
        limit=PAGE_SIZE,
        offset=offset,
    )
    if data is None:
        return

    if not data["items"]:
        empty_state(
            "No departments match.",
            "Clear the search, or create one below.",
        )
    else:
        table(
            [
                {**row, "state": "Active" if row["is_active"] else "Deactivated"}
                for row in data["items"]
            ],
            {
                "code": "Code",
                "name": "Name",
                "description": "Description",
                "state": "State",
            },
        )
        _deactivate_control(
            "department",
            data["items"],
            client.deactivate_department,
        )

    with st.expander("Create a department"), st.form("create_department"):
        name = st.text_input("Name")
        code = st.text_input("Code", help="Letters, digits, dash or underscore.")
        description = st.text_area("Description", value="")
        if st.form_submit_button("Create"):
            if not name.strip() or not code.strip():
                st.error("Name and code are both required.")
            else:
                _submit(
                    client.create_department,
                    {
                        "name": name.strip(),
                        "code": code.strip(),
                        "description": description.strip() or None,
                    },
                    f"Department {code.strip().upper()} created.",
                )


# ----------------------------------------------------------------- shifts
def _shifts() -> None:
    client = session.get_client()
    page_header(
        "Shifts",
        "A shift's own timezone decides when its day starts, ends and counts "
        "as late -- not the employee's.",
    )

    search = st.text_input(
        "Search", key="shift_search", on_change=reset_page, args=("shifts",)
    )
    include_inactive = st.toggle("Include deactivated", key="shift_inactive")

    is_active = None if include_inactive else True
    head = session.call(
        client.list_shifts, is_active=is_active, search=search or None, limit=1
    )
    total = head["total"] if head else 0
    offset = paginator("shifts", total, limit=PAGE_SIZE)

    data = session.call(
        client.list_shifts,
        is_active=is_active,
        search=search or None,
        limit=PAGE_SIZE,
        offset=offset,
    )
    if data is None:
        return

    if not data["items"]:
        empty_state("No shifts match.", "Clear the search, or create one below.")
    else:
        table(
            [
                {
                    **row,
                    "window": f"{row['start_time'][:5]}-{row['end_time'][:5]}"
                    + (" (+1d)" if row["is_overnight"] else ""),
                    "days": _weekdays(row["working_days"]),
                    "grace": f"{row['grace_period_minutes']} min",
                    "state": "Active" if row["is_active"] else "Deactivated",
                }
                for row in data["items"]
            ],
            {
                "code": "Code",
                "name": "Name",
                "window": "Window",
                "timezone": "Timezone",
                "days": "Days",
                "grace": "Grace",
                "state": "State",
            },
        )
        _deactivate_control("shift", data["items"], client.deactivate_shift)

    with st.expander("Create a shift"):
        _shift_form(client)


def _weekdays(days: list[int]) -> str:
    """An empty list means every day, matching how the rules layer reads it."""
    if not days:
        return "Every day"
    return " ".join(WEEKDAYS[d] for d in sorted(days))


def _shift_form(client) -> None:
    with st.form("create_shift"):
        name = st.text_input("Name")
        code = st.text_input("Code", key="shift_code")
        timezone = st.selectbox("Timezone", COMMON_TIMEZONES)

        left, right = st.columns(2)
        start_time = left.time_input("Start", value=time(9, 0))
        end_time = right.time_input("End", value=time(18, 0))

        overnight = st.checkbox(
            "Runs past midnight",
            help="Tick when the shift ends on the following day, e.g. 22:00-06:00.",
        )
        grace = st.number_input(
            "Grace period (minutes)", min_value=0, max_value=1440, value=15
        )
        break_minutes = st.number_input(
            "Break (minutes)", min_value=0, max_value=1440, value=60
        )
        half_day = st.number_input(
            "Half-day threshold (minutes)", min_value=1, max_value=1440, value=240
        )
        working_days = st.multiselect(
            "Working days",
            options=list(WEEKDAYS),
            default=[1, 2, 3, 4, 5],
            format_func=lambda d: WEEKDAYS[d],
        )

        if not st.form_submit_button("Create"):
            return

    if not name.strip() or not code.strip():
        st.error("Name and code are both required.")
        return
    # The same rule the API enforces, stated before the round trip so the
    # mistake is explained next to the checkbox that caused it.
    if overnight and end_time > start_time:
        st.error(
            "An overnight shift must end at or before its start time -- "
            "its end falls on the next day."
        )
        return
    if not overnight and end_time <= start_time:
        st.error("End must be after start, or tick 'Runs past midnight'.")
        return

    _submit(
        client.create_shift,
        {
            "name": name.strip(),
            "code": code.strip(),
            "timezone": timezone,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "is_overnight": overnight,
            "grace_period_minutes": int(grace),
            "break_minutes": int(break_minutes),
            "half_day_threshold_minutes": int(half_day),
            "working_days": sorted(working_days),
        },
        f"Shift {code.strip().upper()} created.",
    )


# ------------------------------------------------------------ assignments
def _assignments() -> None:
    client = session.get_client()
    page_header(
        "Shift assignments",
        "Who works which shift, and from when. Ending an assignment leaves "
        "past attendance scored against the shift that was actually in force.",
    )

    users = session.call(client.list_users, limit=200)
    shifts = session.call(client.list_shifts, is_active=True, limit=200)
    if users is None or shifts is None:
        return

    by_user = {u["id"]: u for u in users["items"]}
    by_shift = {s["id"]: s for s in shifts["items"]}

    chosen_user = st.selectbox(
        "Filter by employee",
        options=[None, *by_user],
        format_func=lambda uid: (
            "Everyone"
            if uid is None
            else f"{by_user[uid]['full_name']} ({by_user[uid]['employee_code']})"
        ),
        key="assign_user",
        on_change=reset_page,
        args=("assignments",),
    )

    head = session.call(client.list_assignments, user_id=chosen_user, limit=1)
    total = head["total"] if head else 0
    offset = paginator("assignments", total, limit=PAGE_SIZE)

    data = session.call(
        client.list_assignments, user_id=chosen_user, limit=PAGE_SIZE, offset=offset
    )
    if data is None:
        return

    if not data["items"]:
        empty_state(
            "No assignments match.",
            "Assign someone to a shift below -- an employee with no active "
            "assignment cannot punch in.",
        )
    else:
        table(
            [
                {
                    "employee": _label(by_user.get(row["user_id"]), row["user_id"]),
                    "shift": _label(by_shift.get(row["shift_id"]), row["shift_id"]),
                    "effective_from": row["effective_from"],
                    "effective_to": row["effective_to"] or "Open-ended",
                    "primary": "Yes" if row["is_primary"] else "No",
                }
                for row in data["items"]
            ],
            {
                "employee": "Employee",
                "shift": "Shift",
                "effective_from": "From",
                "effective_to": "To",
                "primary": "Primary",
            },
        )
        _end_assignment(client, data["items"], by_user, by_shift)

    with st.expander("Assign an employee to a shift"):
        with st.form("create_assignment"):
            user_id = st.selectbox(
                "Employee",
                options=list(by_user),
                format_func=lambda uid: (
                    f"{by_user[uid]['full_name']} ({by_user[uid]['employee_code']})"
                ),
            )
            shift_id = st.selectbox(
                "Shift",
                options=list(by_shift),
                format_func=lambda sid: (
                    f"{by_shift[sid]['name']} ({by_shift[sid]['code']})"
                ),
            )
            left, right = st.columns(2)
            effective_from = left.date_input("From", value=date.today())
            open_ended = right.checkbox("Open-ended", value=True)
            effective_to = (
                None
                if open_ended
                else st.date_input(
                    "To", value=date.today() + timedelta(days=90), key="assign_to"
                )
            )
            is_primary = st.checkbox("Primary assignment", value=True)

            if not st.form_submit_button("Assign"):
                return

        if effective_to is not None and effective_to < effective_from:
            st.error("The end date must not be before the start date.")
            return

        _submit(
            client.create_assignment,
            {
                "user_id": user_id,
                "shift_id": shift_id,
                "effective_from": effective_from.isoformat(),
                "effective_to": effective_to.isoformat() if effective_to else None,
                "is_primary": is_primary,
            },
            "Assignment created.",
        )


def _label(row: dict[str, Any] | None, fallback: str) -> str:
    """Fall back to the id when a row is outside the page we fetched."""
    if row is None:
        return fallback
    return row.get("full_name") or row.get("name") or fallback


def _end_assignment(client, rows, by_user, by_shift) -> None:
    open_rows = [r for r in rows if r["effective_to"] is None]
    if not open_rows:
        return
    with st.expander("End an open-ended assignment"):
        chosen = st.selectbox(
            "Assignment",
            options=[r["id"] for r in open_rows],
            format_func=lambda aid: _assignment_label(
                next(r for r in open_rows if r["id"] == aid), by_user, by_shift
            ),
            key="end_assignment_pick",
        )
        effective_to = st.date_input(
            "Last day on this shift", value=date.today(), key="end_assignment_date"
        )
        st.caption(
            "Attendance already recorded against this shift is untouched; only "
            "days after the date above stop being covered."
        )
        if st.button("End assignment"):
            try:
                client.end_assignment(chosen, effective_to)
            except ApiError as exc:
                st.error(exc.detail)
                return
            st.success("Assignment ended.")
            st.rerun()


def _assignment_label(row, by_user, by_shift) -> str:
    return (
        f"{_label(by_user.get(row['user_id']), row['user_id'])} -> "
        f"{_label(by_shift.get(row['shift_id']), row['shift_id'])} "
        f"(from {row['effective_from']})"
    )


def _deactivate_control(noun: str, rows, action) -> None:
    active = [r for r in rows if r["is_active"]]
    if not active:
        return
    with st.expander(f"Deactivate a {noun}"):
        st.caption(
            f"Deactivating hides the {noun} from new work. Nothing is deleted, "
            "so past attendance stays readable."
        )
        chosen = st.selectbox(
            noun.capitalize(),
            options=[r["id"] for r in active],
            format_func=lambda rid: next(
                f"{r['name']} ({r['code']})" for r in active if r["id"] == rid
            ),
            key=f"deactivate_{noun}_pick",
        )
        if st.button(f"Deactivate {noun}", key=f"deactivate_{noun}_go"):
            try:
                action(chosen)
            except ApiError as exc:
                st.error(exc.detail)
                return
            st.success(f"{noun.capitalize()} deactivated.")
            st.rerun()

"""Admin: reporting and analytics.

Every figure on this page was computed by ``/reports/*``. The dashboard adds
no arithmetic of its own beyond turning a fraction into a percentage for an
axis label, so what a chart shows and what the table beneath it shows cannot
disagree.

Each chart ships with the rows it was drawn from, one expander away. That is
the accessibility relief for anyone who cannot read a colour difference, and
it is also how a sceptical reader checks a figure before repeating it.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from dashboard import session
from dashboard.charts import (
    absenteeism_by_group,
    attendance_trend,
    status_composition,
)
from dashboard.components import (
    PAGE_SIZE,
    date_range_filter,
    empty_state,
    format_minutes,
    humanise_status,
    metric_row,
    page_header,
    paginator,
    percent,
    reset_page,
    table,
)
from dashboard.theme import palette

GRANULARITIES = ["day", "week", "month"]


def _palette():
    """Match the chart surface to whichever theme Streamlit is rendering.

    The dark steps are their own validated values, not an inversion of the
    light ones -- see ``theme``.
    """
    try:
        base = st.get_option("theme.base")
    except Exception:  # pragma: no cover - option missing in older versions
        base = "light"
    return palette(dark=base == "dark")


def _departments() -> dict[str, str]:
    """``{id: name}`` for the filter, plus an "All departments" entry."""
    data = session.call(
        session.get_client().list_departments, is_active=True, limit=200
    )
    return {row["id"]: row["name"] for row in (data or {"items": []})["items"]}


def render() -> None:
    st.title("Reports")
    st.caption(
        "A day counts as expected only once that employee's shift has "
        "finished in the shift's own timezone. Approved leave is never "
        "counted as an absence."
    )

    departments = _departments()
    department_id = st.selectbox(
        "Department",
        options=[None, *departments],
        format_func=lambda d: "All departments" if d is None else departments[d],
        key="reports_department",
        on_change=_reset_all,
    )

    overview, trends, late, absences, by_group = st.tabs(
        ["Overview", "Trends", "Late arrivals", "Absences", "Absenteeism"]
    )
    with overview:
        _overview(department_id)
    with trends:
        _trends(department_id)
    with late:
        _late_arrivals(department_id)
    with absences:
        _absences(department_id)
    with by_group:
        _absenteeism(department_id)


def _reset_all() -> None:
    for key in ("late_arrivals", "absences"):
        reset_page(key)


# --------------------------------------------------------------- overview
def _overview(department_id: str | None) -> None:
    client = session.get_client()
    page_header("Overview", "One day at a glance, and the days around it.")

    on = st.date_input("Day", value=date.today(), key="overview_day")
    summary = session.call(client.report_daily, on, department_id=department_id)
    if summary is None:
        return

    if summary["expected"] == 0:
        empty_state(
            f"Nobody was expected on {on}.",
            "Either no shift covers that day, or the shifts covering it have "
            "not finished yet in their own timezone.",
        )
    else:
        metric_row(
            [
                ("Expected", summary["expected"], "People whose shift had finished."),
                ("Present", summary["present"], "Includes those who arrived late."),
                ("Late", summary["late"], None),
                ("Absent", summary["absent"], None),
                ("On leave", summary["on_leave"], "Excluded from the rate."),
                ("Attendance rate", percent(summary["attendance_rate"]), None),
            ]
        )

    chosen = date_range_filter("overview_range", default_days=14)
    if chosen is None:
        return
    start, end = chosen

    data = session.call(
        client.report_daily_summaries, start, end, department_id=department_id
    )
    if data is None:
        return
    rows = data["items"]

    st.plotly_chart(
        status_composition(rows, _palette()),
        width="stretch",
        config={"displayModeBar": False},
    )
    if rows:
        with st.expander("The numbers behind this chart"):
            table(
                [
                    {
                        **row,
                        "rate": percent(row["attendance_rate"]),
                        "on_time": row["present"] - row["late"],
                    }
                    for row in rows
                ],
                {
                    "work_date": "Date",
                    "expected": "Expected",
                    "on_time": "On time",
                    "late": "Late",
                    "on_leave": "On leave",
                    "absent": "Absent",
                    "rate": "Attendance rate",
                },
            )


# ----------------------------------------------------------------- trends
def _trends(department_id: str | None) -> None:
    client = session.get_client()
    page_header("Trends", "Attendance rate over time.")

    chosen = date_range_filter("trends_range", default_days=90)
    if chosen is None:
        return
    start, end = chosen
    granularity = st.radio(
        "Group by", GRANULARITIES, horizontal=True, key="trends_granularity"
    )

    data = session.call(
        client.report_trends,
        start,
        end,
        granularity=granularity,
        department_id=department_id,
    )
    if data is None:
        return
    rows = data["items"]

    st.plotly_chart(
        attendance_trend(rows, _palette()),
        width="stretch",
        config={"displayModeBar": False},
    )
    if not rows:
        empty_state("No completed periods in this range.")
        return

    with st.expander("The numbers behind this chart"):
        table(
            [
                {
                    **row,
                    "rate": percent(row["attendance_rate"]),
                    "avg_late": format_minutes(round(row["average_late_minutes"])),
                }
                for row in rows
            ],
            {
                "period_start": "From",
                "period_end": "To",
                "expected": "Expected",
                "attended": "Attended",
                "late": "Late",
                "absent": "Absent",
                "rate": "Attendance rate",
                "avg_late": "Average lateness",
            },
        )


# ---------------------------------------------------------- late arrivals
def _late_arrivals(department_id: str | None) -> None:
    client = session.get_client()
    page_header("Late arrivals", "Each late day, with how late it was.")

    chosen = date_range_filter("late_range", default_days=30)
    if chosen is None:
        return
    start, end = chosen
    threshold = st.slider(
        "At least this many minutes late",
        min_value=0,
        max_value=180,
        value=1,
        key="late_threshold",
        on_change=reset_page,
        args=("late_arrivals",),
    )

    head = session.call(
        client.report_late_arrivals,
        start,
        end,
        department_id=department_id,
        min_late_minutes=threshold,
        limit=1,
    )
    total = head["total"] if head else 0
    offset = paginator("late_arrivals", total, limit=PAGE_SIZE)

    data = session.call(
        client.report_late_arrivals,
        start,
        end,
        department_id=department_id,
        min_late_minutes=threshold,
        limit=PAGE_SIZE,
        offset=offset,
    )
    if data is None:
        return
    if not data["items"]:
        empty_state(
            "No late arrivals in this range.",
            "Lower the threshold, or widen the dates.",
        )
        return

    _employee_day_table(data["items"])


def _absences(department_id: str | None) -> None:
    client = session.get_client()
    page_header(
        "Absences",
        "Days an employee was expected and no attendance was recorded. "
        "Approved leave is excluded.",
    )

    chosen = date_range_filter("absence_range", default_days=30)
    if chosen is None:
        return
    start, end = chosen

    head = session.call(
        client.report_absences, start, end, department_id=department_id, limit=1
    )
    total = head["total"] if head else 0
    offset = paginator("absences", total, limit=PAGE_SIZE)

    data = session.call(
        client.report_absences,
        start,
        end,
        department_id=department_id,
        limit=PAGE_SIZE,
        offset=offset,
    )
    if data is None:
        return
    if not data["items"]:
        empty_state("No absences in this range.", "Nothing to chase up.")
        return

    _employee_day_table(data["items"])


def _employee_day_table(rows) -> None:
    table(
        [
            {
                **row,
                "status_label": humanise_status(row.get("status")),
                "late": format_minutes(row.get("late_minutes")),
                "worked": format_minutes(row.get("worked_minutes")),
                "department": row.get("department_name") or "--",
            }
            for row in rows
        ],
        {
            "work_date": "Date",
            "employee_code": "Code",
            "full_name": "Employee",
            "department": "Department",
            "status_label": "Status",
            "late": "Late by",
            "worked": "Worked",
        },
    )


# ------------------------------------------------------------ absenteeism
def _absenteeism(department_id: str | None) -> None:
    client = session.get_client()
    page_header("Absenteeism", "Absence rate, ranked worst first.")

    chosen = date_range_filter("absenteeism_range", default_days=90)
    if chosen is None:
        return
    start, end = chosen
    group_by = st.radio(
        "Group by", ["department", "employee"], horizontal=True, key="absenteeism_group"
    )

    data = session.call(
        client.report_absenteeism,
        start,
        end,
        group_by=group_by,
        department_id=department_id,
    )
    if data is None:
        return
    rows = data["items"]

    # A chart of two hundred employees is a wall, not a comparison; the table
    # below carries the rest.
    st.plotly_chart(
        absenteeism_by_group(rows[:15], _palette()),
        width="stretch",
        config={"displayModeBar": False},
    )
    if not rows:
        empty_state("No expected days in this range.")
        return
    if len(rows) > 15:
        st.caption(
            f"Chart shows the 15 worst of {len(rows)} groups; all are listed below."
        )

    with st.expander("The numbers behind this chart", expanded=len(rows) > 15):
        table(
            [
                {
                    **row,
                    "absence": percent(row["absence_rate"]),
                    "attendance": percent(row["attendance_rate"]),
                }
                for row in rows
            ],
            {
                "group_label": "Group",
                "expected_days": "Expected days",
                "attended_days": "Attended",
                "late_days": "Late",
                "on_leave_days": "On leave",
                "absent_days": "Absent",
                "absence": "Absence rate",
                "attendance": "Attendance rate",
            },
        )

"""Reusable pieces every page is assembled from.

The pure helpers at the top -- pagination arithmetic, formatting, range
validation -- are separated from the widgets that draw them so they can be
tested without a running Streamlit server. Anything that renders takes its
data already fetched; nothing here calls the API.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from dashboard.theme import STATUS_ICONS

# How many rows a list page asks for at a time.
PAGE_SIZE = 25

# Report endpoints cap a range at a year; the UI refuses earlier, with a
# sentence, rather than letting the API answer 422.
MAX_RANGE_DAYS = 366


# ----------------------------------------------------------- pure helpers
def page_bounds(total: int, limit: int, offset: int) -> tuple[int, int, int]:
    """``(first, last, pages)`` for a "showing 26-50 of 93" caption.

    One-indexed for display. An empty result is ``(0, 0, 1)``: zero pages
    would make "page 1 of 0" appear, which reads like a bug.
    """
    if total <= 0 or limit <= 0:
        return 0, 0, 1
    first = min(offset + 1, total)
    last = min(offset + limit, total)
    pages = max(1, -(-total // limit))  # ceiling division
    return first, last, pages


def validate_range(
    start: date, end: date, *, max_days: int = MAX_RANGE_DAYS
) -> str | None:
    """The complaint about a date range, or ``None`` if it is usable."""
    if start > end:
        return "The start date must be on or before the end date."
    span = (end - start).days + 1
    if span > max_days:
        return f"That range covers {span} days; the maximum is {max_days}."
    return None


def format_minutes(minutes: int | None) -> str:
    """``95`` -> ``1h 35m``. ``None`` is not zero, and must not read as zero."""
    if minutes is None:
        return "--"
    if minutes < 0:
        return "--"
    hours, rest = divmod(int(minutes), 60)
    if hours and rest:
        return f"{hours}h {rest}m"
    if hours:
        return f"{hours}h"
    return f"{rest}m"


def format_timestamp(value: str | None) -> str:
    """Render an ISO-8601 instant for reading, keeping the offset visible.

    The API serialises in UTC with an offset; dropping it would invite a
    reader in another zone to subtract the wrong number of hours.
    """
    if not value:
        return "--"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    return stamp.strftime("%Y-%m-%d %H:%M %Z") or stamp.strftime("%Y-%m-%d %H:%M")


def humanise_status(status: str | None) -> str:
    """``half_day`` -> ``Half day``, and an icon for anything with a status colour."""
    if not status:
        return "--"
    label = status.replace("_", " ").capitalize()
    canonical = {
        "present": "Present",
        "late": "Late",
        "absent": "Absent",
        "on_leave": "On leave",
    }.get(status)
    icon = STATUS_ICONS.get(canonical or "")
    return f"{icon} {label}" if icon else label


def punch_state(today: dict[str, Any] | None) -> str:
    """Which of the three things the punch buttons may be doing right now.

    ``not_started`` -- nothing recorded today, so the only move is punching in.
    ``open`` -- punched in and still in; the only move is punching out.
    ``closed`` -- both punches recorded; neither button does anything.

    Derived from the record rather than remembered in session state, so a
    punch made on a phone is reflected the moment this page reloads.
    """
    if not today or not today.get("check_in_at"):
        return "not_started"
    return "open" if today.get("check_out_at") is None else "closed"


def percent(fraction: float | None) -> str:
    """The API reports rates as fractions; people read them as percentages."""
    if fraction is None:
        return "--"
    return f"{fraction * 100:.1f}%"


# ---------------------------------------------------------------- widgets
def page_header(title: str, subtitle: str | None = None) -> None:
    st.subheader(title)
    if subtitle:
        st.caption(subtitle)


def empty_state(message: str, hint: str | None = None) -> None:
    """What a page shows when a query legitimately returned nothing.

    Distinct from an error: nothing went wrong, there is simply nothing to
    show, and the hint says what would change that.
    """
    st.info(message if not hint else f"{message}\n\n{hint}")


def metric_row(items: list[tuple[str, Any, str | None]]) -> None:
    """A row of headline figures. Wraps to one per line on a narrow screen."""
    for column, (label, value, help_text) in zip(
        st.columns(len(items)), items, strict=True
    ):
        column.metric(label, value, help=help_text)


def date_range_filter(
    key: str,
    *,
    default_days: int = 30,
    max_days: int = MAX_RANGE_DAYS,
) -> tuple[date, date] | None:
    """Two date inputs plus validation. ``None`` when the range is unusable.

    Returning ``None`` rather than a clamped range is deliberate: silently
    "fixing" a range would show the user figures for days they did not ask
    about.
    """
    today = date.today()
    left, right = st.columns(2)
    start = left.date_input(
        "From", value=today - timedelta(days=default_days - 1), key=f"{key}_start"
    )
    end = right.date_input("To", value=today, key=f"{key}_end")

    complaint = validate_range(start, end, max_days=max_days)
    if complaint:
        st.warning(complaint)
        return None
    return start, end


def paginator(key: str, total: int, *, limit: int = PAGE_SIZE) -> int:
    """Previous/next controls. Returns the offset to request next.

    The page number lives in session state under ``key`` so it survives the
    rerun that every Streamlit interaction causes.
    """
    state_key = f"{key}_page"
    page = st.session_state.setdefault(state_key, 0)

    _, last, pages = page_bounds(total, limit, page * limit)
    # A filter change can shrink the result below the page being viewed.
    if page >= pages:
        page = pages - 1
        st.session_state[state_key] = page

    first, last, pages = page_bounds(total, limit, page * limit)

    back, caption, forward = st.columns([1, 3, 1])
    if back.button("Previous", key=f"{key}_prev", disabled=page <= 0, width="stretch"):
        st.session_state[state_key] = page - 1
        st.rerun()
    caption.caption(
        f"Showing {first}-{last} of {total}  ·  page {page + 1} of {pages}"
        if total
        else "Nothing to show"
    )
    if forward.button(
        "Next",
        key=f"{key}_next",
        disabled=page >= pages - 1,
        width="stretch",
    ):
        st.session_state[state_key] = page + 1
        st.rerun()

    return page * limit


def reset_page(key: str) -> None:
    """Send a list back to its first page after a filter changes."""
    st.session_state[f"{key}_page"] = 0


def table(rows: list[dict[str, Any]], columns: dict[str, str]) -> None:
    """Render selected keys of a row list, in order, under readable headings.

    Naming the columns explicitly means a field added to an API response --
    an internal id, say -- does not silently appear on screen.
    """
    frame = pd.DataFrame(
        [{label: row.get(key) for key, label in columns.items()} for row in rows]
    )
    st.dataframe(frame, width="stretch", hide_index=True)


def form_errors(errors: dict[str, str]) -> None:
    """Show a 422's per-field complaints next to each other."""
    for field, message in errors.items():
        st.error(f"**{field}** -- {message}" if field != "_" else message)

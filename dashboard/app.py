"""Streamlit dashboard entrypoint.

Talks to the FastAPI backend over HTTP only -- it never imports the ORM or
touches the database directly. Run it with::

    streamlit run dashboard/app.py

Routing is role-based and computed from ``/auth/me``, not from anything the
client chose. That is a usability measure rather than a security one: every
admin endpoint enforces its own RBAC, so hiding a page only spares an employee
a 403 they could not have done anything with.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit Cloud runs `streamlit run dashboard/app.py` directly, which only
# puts this file's own directory on sys.path, not the repo root -- so the
# sibling `dashboard` package import below fails unless we add it ourselves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from dashboard import session
from dashboard.api_client import DEFAULT_BASE_URL
from dashboard.views import admin_audit, admin_directory, admin_reports, assistant
from dashboard.views import employee as employee_view
from dashboard.views import login as login_view

st.set_page_config(
    page_title="Attendance Management Platform",
    page_icon=":material/schedule:",
    layout="wide",
    # Collapsed by default so the content gets the whole width on a phone,
    # where the sidebar would otherwise cover it.
    initial_sidebar_state="auto",
)

MY_ATTENDANCE = "My attendance"
ASSISTANT = "Attendance assistant"
DIRECTORY = "Directory"
REPORTS = "Reports"
AUDIT = "Audit trail"

EMPLOYEE_PAGES = [MY_ATTENDANCE, ASSISTANT]
ADMIN_ONLY_PAGES = [REPORTS, DIRECTORY, AUDIT]

RENDERERS = {
    MY_ATTENDANCE: employee_view.render,
    ASSISTANT: assistant.render,
    DIRECTORY: admin_directory.render,
    REPORTS: admin_reports.render,
    AUDIT: admin_audit.render,
}


def pages_for(role: str | None) -> list[str]:
    """The pages a role may navigate to, in the order they are offered.

    An admin keeps the employee pages: they have attendance of their own, and
    dropping them would leave an admin unable to punch in.
    """
    if role == "admin":
        return EMPLOYEE_PAGES + ADMIN_ONLY_PAGES
    return list(EMPLOYEE_PAGES)


def _sidebar(user: dict, allowed: list[str]) -> str:
    with st.sidebar:
        st.markdown(f"**{user.get('full_name', 'Signed in')}**")
        st.caption(
            f"{user.get('role', 'employee').title()} · "
            f"{user.get('employee_code', '--')}"
        )
        st.divider()

        # A page remembered from a previous role -- or set by a cross-page
        # button -- must not survive into one that may not see it.
        current = st.session_state.get("nav")
        if current not in allowed:
            st.session_state["nav"] = allowed[0]

        choice = st.radio("Go to", allowed, key="nav", label_visibility="collapsed")

        st.divider()
        if st.button("Sign out", width="stretch"):
            session.sign_out(notice="You have been signed out.")
            st.rerun()
        st.caption(f"API: {DEFAULT_BASE_URL}")
    return choice


def main() -> None:
    user = session.current_user()
    if user is None:
        login_view.render()
        return

    allowed = pages_for(user.get("role"))
    choice = _sidebar(user, allowed)
    RENDERERS[choice]()


if __name__ == "__main__":
    main()

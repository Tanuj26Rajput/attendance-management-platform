"""Session state: who is signed in, and the client that acts for them.

The access token lives in ``st.session_state``, which is per-browser-session
and server-side -- it is never written to a cookie, a query parameter, a log
line or the page itself, so it cannot be read out of a shared URL or a
screenshot. It is held only for as long as the tab lives.

Two things end a session: the user signing out, which also revokes the token
at the API so it is dead even if it leaked; and the token expiring, which is
noticed twice -- locally from ``expires_at``, to avoid a pointless round trip,
and from the API's own 401, which is the authority.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import streamlit as st

from dashboard.api_client import ApiClient, ApiError, SessionExpiredError

TOKEN_KEY = "auth_token"
EXPIRES_KEY = "auth_expires_at"
USER_KEY = "auth_user"
NOTICE_KEY = "auth_notice"

# Treat a token as spent slightly before it actually is, so a request started
# just under the wire does not fail halfway through a form submission.
EXPIRY_MARGIN = timedelta(seconds=30)

T = TypeVar("T")


def expires_at(expires_in: int, *, now: datetime | None = None) -> datetime:
    """When a token obtained now should stop being used."""
    return (now or datetime.now(UTC)) + timedelta(seconds=expires_in) - EXPIRY_MARGIN


def is_expired(deadline: datetime | None, *, now: datetime | None = None) -> bool:
    """No deadline means nothing is signed in, which is not a live session."""
    if deadline is None:
        return True
    return (now or datetime.now(UTC)) >= deadline


# ------------------------------------------------------------------- state
def current_user() -> dict[str, Any] | None:
    if is_expired(st.session_state.get(EXPIRES_KEY)):
        return None
    return st.session_state.get(USER_KEY)


def is_admin() -> bool:
    user = current_user()
    return user is not None and user.get("role") == "admin"


def get_client() -> ApiClient:
    """A client carrying the current token, or none if nobody is signed in."""
    token = st.session_state.get(TOKEN_KEY)
    if is_expired(st.session_state.get(EXPIRES_KEY)):
        token = None
    return ApiClient(token=token)


def sign_in(email: str, password: str) -> dict[str, Any]:
    """Exchange credentials for a token, then confirm who it belongs to.

    The role comes from ``/auth/me`` rather than from anything the client
    chose, which is what the navigation is built from below.
    """
    client = ApiClient()
    tokens = client.login(email, password)

    st.session_state[TOKEN_KEY] = tokens["access_token"]
    st.session_state[EXPIRES_KEY] = expires_at(tokens["expires_in"])

    user = ApiClient(token=tokens["access_token"]).me()
    st.session_state[USER_KEY] = user
    return user


def sign_out(*, notice: str | None = None) -> None:
    """Revoke at the API, then forget locally.

    The local clear happens even if the revoke call fails -- a user who clicks
    "Sign out" must end up signed out of this browser regardless of whether
    the API was reachable.
    """
    token = st.session_state.get(TOKEN_KEY)
    if token:
        with contextlib.suppress(ApiError):
            ApiClient(token=token).logout()
    for key in (TOKEN_KEY, EXPIRES_KEY, USER_KEY):
        st.session_state.pop(key, None)
    if notice:
        st.session_state[NOTICE_KEY] = notice


def take_notice() -> str | None:
    """Read and clear the one-shot message shown on the login screen."""
    return st.session_state.pop(NOTICE_KEY, None)


# ------------------------------------------------------------------- calls
def call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T | None:
    """Run an API call, turning a failure into a message instead of a stack trace.

    Returns ``None`` on failure, so a caller writes::

        data = call(client.report_trends, start, end)
        if data is None:
            return

    An expired session is the one failure that is not just reported: it ends
    the session and reruns, which lands the user on the login screen with an
    explanation rather than on a page full of error boxes.
    """
    try:
        return fn(*args, **kwargs)
    except SessionExpiredError:
        sign_out(notice="Your session expired. Please sign in again.")
        st.rerun()
    except ApiError as exc:
        st.error(exc.detail)
        return None

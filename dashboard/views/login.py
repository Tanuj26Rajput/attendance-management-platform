"""The sign-in screen, and the only page an anonymous visitor can reach."""

from __future__ import annotations

import streamlit as st

from dashboard import session
from dashboard.api_client import ApiClient, ApiError, ValidationFailedError
from dashboard.components import form_errors

# Mirrors UserRegister's own minimum, so the obvious mistake is caught before
# a round trip. The API remains the authority -- this is a courtesy, not a rule.
MIN_PASSWORD_CHARS = 12


def render() -> None:
    st.title("Attendance Management Platform")

    notice = session.take_notice()
    if notice:
        st.warning(notice)

    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        sign_in_tab, register_tab = st.tabs(["Sign in", "Create an account"])
        with sign_in_tab:
            _sign_in_form()
        with register_tab:
            _register_form()

        _backend_status()


def _sign_in_form() -> None:
    with st.form("sign_in"):
        email = st.text_input("Email", autocomplete="username")
        password = st.text_input(
            "Password", type="password", autocomplete="current-password"
        )
        submitted = st.form_submit_button("Sign in", width="stretch")

    if not submitted:
        return
    if not email or not password:
        st.error("Enter your email and password.")
        return

    with st.spinner("Signing in..."):
        try:
            user = session.sign_in(email.strip(), password)
        except ApiError as exc:
            # The API answers the same way for an unknown address and a wrong
            # password; repeating its wording keeps it that way.
            st.error(exc.detail)
            return

    st.success(f"Welcome back, {user['full_name']}.")
    st.rerun()


def _register_form() -> None:
    with st.form("register"):
        full_name = st.text_input("Full name")
        email = st.text_input("Email", key="register_email")
        password = st.text_input(
            "Password",
            type="password",
            key="register_password",
            help=f"At least {MIN_PASSWORD_CHARS} characters.",
        )
        submitted = st.form_submit_button("Create account", width="stretch")

    if not submitted:
        return

    complaints = []
    if not full_name.strip():
        complaints.append("Enter your full name.")
    if "@" not in email:
        complaints.append("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_CHARS:
        complaints.append(f"Use at least {MIN_PASSWORD_CHARS} characters.")
    if complaints:
        for complaint in complaints:
            st.error(complaint)
        return

    with st.spinner("Creating your account..."):
        try:
            ApiClient().register(email.strip(), password, full_name.strip())
        except ValidationFailedError as exc:
            form_errors(exc.errors or {"_": exc.detail})
            return
        except ApiError as exc:
            st.error(exc.detail)
            return

    st.success("Account created. Sign in with it using the tab above.")


def _backend_status() -> None:
    """One line saying whether the API is up.

    Worth its space only here: "wrong password" and "the server is down" look
    identical from a login box otherwise.
    """
    try:
        health = ApiClient().health()
    except ApiError:
        st.error(
            f"The API at {ApiClient().base_url} is not responding. "
            "Signing in will not work until it is running."
        )
        return
    st.caption(
        f"API v{health.get('version', '?')} · {health.get('environment', 'unknown')}"
    )

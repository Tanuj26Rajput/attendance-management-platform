"""Boots the FastAPI backend in-process for single-deployment hosting.

Streamlit Cloud only runs one process, so there is nowhere to point
``API_BASE_URL`` at a separately hosted backend without standing up a second
deployment. This starts the real FastAPI app (``app.main:app``, unmodified)
on a background thread inside the same process, listening on loopback, so
the dashboard keeps talking to it over HTTP exactly as it does locally.

Streamlit secrets (configured in the Cloud dashboard, e.g. ``DATABASE_URL``,
``JWT_SECRET_KEY``) are copied into the environment first, since the FastAPI
settings are loaded from the environment, not ``st.secrets``.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import httpx
import streamlit as st


def _copy_secrets_to_env() -> None:
    try:
        secrets = dict(st.secrets)
    except Exception:
        return
    for key, value in secrets.items():
        if isinstance(value, (dict, list)):
            continue
        os.environ.setdefault(key, str(value))


def _run_server(host: str, port: int) -> None:
    import uvicorn

    from app.main import app as fastapi_app

    uvicorn.Server(
        uvicorn.Config(fastapi_app, host=host, port=port, log_level="warning")
    ).run()


@st.cache_resource(show_spinner="Starting API...")
def ensure_api_running() -> None:
    """Starts the FastAPI backend once per process, if it isn't already up."""
    _copy_secrets_to_env()

    base_url = os.environ.get("API_BASE_URL", "http://localhost:8000")
    parsed = urlparse(base_url)
    if parsed.hostname not in ("localhost", "127.0.0.1", "0.0.0.0"):
        return  # API_BASE_URL points at an externally hosted backend.

    host, port = "127.0.0.1", parsed.port or 8000
    threading.Thread(target=_run_server, args=(host, port), daemon=True).start()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://{host}:{port}/", timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.3)

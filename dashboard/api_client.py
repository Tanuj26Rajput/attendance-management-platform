"""HTTP client for the attendance API.

This module is the dashboard's only route to data. It holds no business logic
-- every rate, total and status it returns was computed by the API -- and it
deliberately imports nothing from ``app``: the dashboard is a client of the
service, not a second copy of it.

It also imports nothing from Streamlit, which is what makes it testable
without a browser or a server. Streamlit-specific concerns (where the token is
kept, how an error is drawn) live in ``session`` and ``components``.

Errors are translated once, here, into a small exception family, so a page can
write ``except SessionExpiredError`` instead of inspecting status codes. The
translation matters most for 401: an expired token looks identical to a wrong
one from the outside, and both mean "send them back to the login screen".
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any
from uuid import UUID

import httpx

DEFAULT_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
DEFAULT_TIMEOUT = 15.0

# The assistant answers from the database and sometimes takes several tool
# rounds to do it, so it gets a longer budget than an ordinary read.
CHAT_TIMEOUT = 60.0


class ApiError(Exception):
    """Any failed call. Carries a message already fit to show a user."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class SessionExpiredError(ApiError):
    """401. The token is missing, malformed, revoked or past its expiry."""


class ForbiddenError(ApiError):
    """403. Authenticated, but not allowed -- a non-admin reaching an admin page."""


class NotFoundError(ApiError):
    """404."""


class ConflictError(ApiError):
    """409. A duplicate code, or an overlapping shift assignment."""


class ValidationFailedError(ApiError):
    """422. ``errors`` maps a field path to the first complaint about it."""

    def __init__(self, detail: str, errors: dict[str, str] | None = None) -> None:
        super().__init__(detail, status_code=422)
        self.errors = errors or {}


class ApiUnavailableError(ApiError):
    """The API could not be reached, or failed on its own side."""


def _detail_of(response: httpx.Response) -> str:
    """Pull FastAPI's ``detail`` out, falling back to something printable.

    A proxy or a crash can return HTML or an empty body, so this never assumes
    the response is JSON.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:300] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)
    return f"HTTP {response.status_code}"


def _field_errors(response: httpx.Response) -> dict[str, str]:
    """Flatten FastAPI's 422 body into ``{"field.path": "message"}``.

    Only the first complaint per field is kept: showing a user three different
    reasons the same box is wrong helps nobody.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, list):
        return {}

    errors: dict[str, str] = {}
    for item in detail:
        if not isinstance(item, dict):
            continue
        # loc is ("body", "start_time"); the first element is the location
        # kind, which means nothing to a user looking at a form.
        parts = [str(p) for p in item.get("loc", ()) if str(p) not in {"body", "query"}]
        key = ".".join(parts) or "_"
        errors.setdefault(key, str(item.get("msg", "invalid value")))
    return errors


def _raise_for(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    if status == 401:
        raise SessionExpiredError(_detail_of(response), status_code=401)
    if status == 403:
        raise ForbiddenError(_detail_of(response), status_code=403)
    if status == 404:
        raise NotFoundError(_detail_of(response), status_code=404)
    if status == 409:
        raise ConflictError(_detail_of(response), status_code=409)
    if status == 422:
        raise ValidationFailedError(_detail_of(response), _field_errors(response))
    if status >= 500:
        raise ApiUnavailableError(_detail_of(response), status_code=status)
    raise ApiError(_detail_of(response), status_code=status)


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    """Drop unset filters and render the types httpx will not.

    A ``None`` filter means "no filter", and sending ``user_id=None`` would be
    read as the literal string.
    """
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, date):
            out[key] = value.isoformat()
        elif isinstance(value, UUID):
            out[key] = str(value)
        elif isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = value
    return out


class ApiClient:
    """A thin, typed wrapper over the v1 API.

    One method per endpoint, named after it. There is deliberately no generic
    ``get(path)`` escape hatch on the public surface: every call a page can
    make is listed here, so the set of endpoints the dashboard depends on is
    readable in one place.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._timeout = timeout
        self._transport = transport

    # --------------------------------------------------------------- plumbing
    @property
    def _headers(self) -> dict[str, str]:
        # No header at all when unauthenticated, rather than an empty Bearer:
        # the API answers "Not authenticated" to the former and "Could not
        # validate credentials" to the latter, and the first is the truth.
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        try:
            with httpx.Client(
                timeout=timeout or self._timeout, transport=self._transport
            ) as client:
                response = client.request(
                    method,
                    url,
                    params=_clean(params or {}),
                    json=json,
                    headers=self._headers,
                )
        except httpx.TimeoutException as exc:
            raise ApiUnavailableError(
                "The API did not respond in time. It may be busy -- try again."
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiUnavailableError(
                f"Could not reach the API at {self.base_url}. Is it running?"
            ) from exc

        _raise_for(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------- auth
    def login(self, email: str, password: str) -> dict:
        return self._request(
            "POST", "/auth/login", json={"email": email, "password": password}
        )

    def register(self, email: str, password: str, full_name: str) -> dict:
        return self._request(
            "POST",
            "/auth/register",
            json={"email": email, "password": password, "full_name": full_name},
        )

    def me(self) -> dict:
        return self._request("GET", "/auth/me")

    def logout(self) -> dict:
        return self._request("POST", "/auth/logout")

    def health(self) -> dict:
        return self._request("GET", "/health", timeout=5.0)

    # ------------------------------------------------------------- attendance
    def punch_in(self, *, notes: str | None = None) -> dict:
        payload: dict[str, Any] = {"source": "web"}
        if notes:
            payload["notes"] = notes
        return self._request("POST", "/attendance/punch-in", json=payload)

    def punch_out(self, *, notes: str | None = None) -> dict:
        payload = {"notes": notes} if notes else {}
        return self._request("POST", "/attendance/punch-out", json=payload)

    def today(self) -> dict | None:
        """``None`` when nothing has been recorded yet today.

        The API answers 404 before the first punch of the day, which is an
        empty state rather than an error worth showing.
        """
        try:
            return self._request("GET", "/attendance/me")
        except NotFoundError:
            return None

    def my_history(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/attendance/me/history",
            params={"start": start, "end": end, "limit": limit, "offset": offset},
        )

    # ------------------------------------------------------------------- chat
    def chat(self, message: str, history: list[dict] | None = None) -> dict:
        return self._request(
            "POST",
            "/chat",
            json={"message": message, "history": history or []},
            timeout=CHAT_TIMEOUT,
        )

    # ------------------------------------------------------------ departments
    def list_departments(
        self,
        *,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/admin/departments",
            params={
                "is_active": is_active,
                "search": search,
                "limit": limit,
                "offset": offset,
            },
        )

    def create_department(self, payload: dict) -> dict:
        return self._request("POST", "/admin/departments", json=payload)

    def update_department(self, department_id: str, payload: dict) -> dict:
        return self._request(
            "PATCH", f"/admin/departments/{department_id}", json=payload
        )

    def deactivate_department(self, department_id: str) -> dict:
        return self._request("POST", f"/admin/departments/{department_id}/deactivate")

    # ----------------------------------------------------------------- shifts
    def list_shifts(
        self,
        *,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/admin/shifts",
            params={
                "is_active": is_active,
                "search": search,
                "limit": limit,
                "offset": offset,
            },
        )

    def create_shift(self, payload: dict) -> dict:
        return self._request("POST", "/admin/shifts", json=payload)

    def update_shift(self, shift_id: str, payload: dict) -> dict:
        return self._request("PATCH", f"/admin/shifts/{shift_id}", json=payload)

    def deactivate_shift(self, shift_id: str) -> dict:
        return self._request("POST", f"/admin/shifts/{shift_id}/deactivate")

    # ------------------------------------------------------------ assignments
    def list_assignments(
        self,
        *,
        user_id: str | None = None,
        shift_id: str | None = None,
        active_on: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/admin/assignments",
            params={
                "user_id": user_id,
                "shift_id": shift_id,
                "active_on": active_on,
                "limit": limit,
                "offset": offset,
            },
        )

    def create_assignment(self, payload: dict) -> dict:
        return self._request("POST", "/admin/assignments", json=payload)

    def update_assignment(self, assignment_id: str, payload: dict) -> dict:
        return self._request(
            "PATCH", f"/admin/assignments/{assignment_id}", json=payload
        )

    def end_assignment(self, assignment_id: str, effective_to: date) -> dict:
        return self._request(
            "POST",
            f"/admin/assignments/{assignment_id}/end",
            params={"effective_to": effective_to},
        )

    # ------------------------------------------------------------------ users
    def list_users(self, *, limit: int = 100, offset: int = 0) -> dict:
        return self._request(
            "GET", "/admin/users", params={"limit": limit, "offset": offset}
        )

    # ------------------------------------------------------------- audit logs
    def list_audit_logs(
        self,
        *,
        actor_id: str | None = None,
        entity_type: str | None = None,
        action: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/admin/audit-logs",
            params={
                "actor_id": actor_id,
                "entity_type": entity_type,
                "action": action,
                "start": start,
                "end": end,
                "limit": limit,
                "offset": offset,
            },
        )

    # ---------------------------------------------------------------- reports
    def report_daily(self, on: date, *, department_id: str | None = None) -> dict:
        return self._request(
            "GET", "/reports/daily", params={"on": on, "department_id": department_id}
        )

    def report_daily_summaries(
        self,
        start: date,
        end: date,
        *,
        department_id: str | None = None,
        limit: int = 366,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/reports/daily-summaries",
            params={
                "start": start,
                "end": end,
                "department_id": department_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def report_trends(
        self,
        start: date,
        end: date,
        *,
        granularity: str = "day",
        department_id: str | None = None,
        limit: int = 366,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/reports/trends",
            params={
                "start": start,
                "end": end,
                "granularity": granularity,
                "department_id": department_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def report_late_arrivals(
        self,
        start: date,
        end: date,
        *,
        department_id: str | None = None,
        min_late_minutes: int | None = None,
        user_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/reports/late-arrivals",
            params={
                "start": start,
                "end": end,
                "department_id": department_id,
                "min_late_minutes": min_late_minutes,
                "user_id": user_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def report_absences(
        self,
        start: date,
        end: date,
        *,
        department_id: str | None = None,
        user_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/reports/absences",
            params={
                "start": start,
                "end": end,
                "department_id": department_id,
                "user_id": user_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def report_absenteeism(
        self,
        start: date,
        end: date,
        *,
        group_by: str = "department",
        department_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return self._request(
            "GET",
            "/reports/absenteeism",
            params={
                "start": start,
                "end": end,
                "group_by": group_by,
                "department_id": department_id,
                "limit": limit,
                "offset": offset,
            },
        )

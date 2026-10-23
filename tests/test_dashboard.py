"""Dashboard tests: the API client, and the pure logic behind the pages.

No database, no network, no browser. Every request is answered by an httpx
MockTransport, which is what lets these assert on the exact URL, query string
and headers the dashboard would really send -- the thing most likely to drift
away from the API without anyone noticing.

What is deliberately *not* tested here is rendering. Asserting that a button
exists tells you nothing about whether it works, and Streamlit's own test
harness would be a second framework to maintain for that payoff. The logic
those widgets depend on is extracted into pure functions and tested instead.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from dashboard import charts, components
from dashboard.api_client import (
    ApiClient,
    ApiError,
    ApiUnavailableError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    SessionExpiredError,
    ValidationFailedError,
    _clean,
)
from dashboard.app import ADMIN_ONLY_PAGES, EMPLOYEE_PAGES, RENDERERS, pages_for
from dashboard.theme import DARK, LIGHT, status_colours
from dashboard.views.assistant import trim_history

MON = dt.date(2026, 5, 4)
FRI = dt.date(2026, 5, 8)


class Recorder:
    """Answers every request with a canned response and remembers the request."""

    def __init__(self, status: int = 200, json: object = None, text: str | None = None):
        self.status = status
        self.json = json
        self.text = text
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(
            self.status, json=self.json if self.json is not None else {}
        )


def client_for(recorder: Recorder, token: str | None = "tok") -> ApiClient:
    return ApiClient("http://api.test", token=token, transport=recorder.transport)


# ------------------------------------------------------------------- auth
def test_the_token_is_sent_as_a_bearer_header() -> None:
    rec = Recorder(json={"id": "u1"})
    client_for(rec, token="abc123").me()
    assert rec.last.headers["authorization"] == "Bearer abc123"


def test_no_authorization_header_when_signed_out() -> None:
    """An empty Bearer would be a different, less honest, error at the API."""
    rec = Recorder(json={})
    client_for(rec, token=None).health()
    assert "authorization" not in rec.last.headers


def test_login_posts_the_credentials_to_the_login_endpoint() -> None:
    rec = Recorder(
        json={"access_token": "t", "token_type": "bearer", "expires_in": 900}
    )
    client_for(rec, token=None).login("a@b.com", "hunter2hunter2")
    assert rec.last.url.path == "/api/v1/auth/login"
    assert rec.last.method == "POST"


def test_a_password_never_reaches_the_query_string() -> None:
    """A URL ends up in proxy logs and browser history; a body does not."""
    rec = Recorder(json={"access_token": "t", "expires_in": 900})
    client_for(rec, token=None).login("a@b.com", "hunter2hunter2")
    assert "hunter2hunter2" not in str(rec.last.url)


# ------------------------------------------------------------ error mapping
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, SessionExpiredError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, ValidationFailedError),
        (500, ApiUnavailableError),
        (503, ApiUnavailableError),
        (418, ApiError),
    ],
)
def test_status_codes_become_their_own_exceptions(status, expected) -> None:
    rec = Recorder(status=status, json={"detail": "nope"})
    with pytest.raises(expected):
        client_for(rec).list_users()


def test_the_api_detail_is_carried_through_to_the_user() -> None:
    rec = Recorder(status=409, json={"detail": "A shift with that code exists"})
    with pytest.raises(ConflictError) as caught:
        client_for(rec).create_shift({})
    assert caught.value.detail == "A shift with that code exists"


def test_a_non_json_error_body_still_produces_a_message() -> None:
    """A crashing proxy returns HTML; the client must not raise on parsing it."""
    rec = Recorder(status=502, text="<html>Bad Gateway</html>")
    with pytest.raises(ApiUnavailableError) as caught:
        client_for(rec).list_users()
    assert caught.value.detail


def test_an_empty_error_body_falls_back_to_the_status_code() -> None:
    rec = Recorder(status=500, text="")
    with pytest.raises(ApiUnavailableError) as caught:
        client_for(rec).list_users()
    assert "500" in caught.value.detail


def test_validation_errors_are_flattened_per_field() -> None:
    rec = Recorder(
        status=422,
        json={
            "detail": [
                {"loc": ["body", "start_time"], "msg": "invalid time"},
                {"loc": ["body", "code"], "msg": "too short"},
            ]
        },
    )
    with pytest.raises(ValidationFailedError) as caught:
        client_for(rec).create_shift({})
    assert caught.value.errors == {"start_time": "invalid time", "code": "too short"}


def test_the_location_kind_is_stripped_from_a_field_name() -> None:
    """A user reading "body.code is too short" learns nothing from "body"."""
    rec = Recorder(
        status=422, json={"detail": [{"loc": ["body", "code"], "msg": "bad"}]}
    )
    with pytest.raises(ValidationFailedError) as caught:
        client_for(rec).create_department({})
    assert "code" in caught.value.errors
    assert not any("body" in key for key in caught.value.errors)


def test_only_the_first_complaint_per_field_is_kept() -> None:
    rec = Recorder(
        status=422,
        json={
            "detail": [
                {"loc": ["body", "code"], "msg": "first"},
                {"loc": ["body", "code"], "msg": "second"},
            ]
        },
    )
    with pytest.raises(ValidationFailedError) as caught:
        client_for(rec).create_department({})
    assert caught.value.errors == {"code": "first"}


def test_an_unreachable_api_names_the_address_it_tried() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = ApiClient("http://api.test", transport=httpx.MockTransport(refuse))
    with pytest.raises(ApiUnavailableError) as caught:
        client.health()
    assert "api.test" in caught.value.detail


def test_a_timeout_says_so_rather_than_looking_like_a_crash() -> None:
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = ApiClient("http://api.test", transport=httpx.MockTransport(stall))
    with pytest.raises(ApiUnavailableError) as caught:
        client.chat("hello")
    assert "time" in caught.value.detail.lower()


# ------------------------------------------------------------ query params
def test_unset_filters_are_omitted_rather_than_sent_as_none() -> None:
    """``user_id=None`` in a URL is read by the server as the string "None"."""
    rec = Recorder(json={"items": [], "total": 0, "limit": 25, "offset": 0})
    client_for(rec).report_absences(MON, FRI, department_id=None, user_id=None)
    query = dict(rec.last.url.params)
    assert "department_id" not in query
    assert "user_id" not in query
    assert query["start"] == "2026-05-04"


def test_dates_are_sent_in_iso_form() -> None:
    rec = Recorder(json={})
    client_for(rec).report_daily(MON)
    assert dict(rec.last.url.params)["on"] == "2026-05-04"


def test_booleans_are_sent_in_the_spelling_fastapi_parses() -> None:
    rec = Recorder(json={"items": [], "total": 0, "limit": 50, "offset": 0})
    client_for(rec).list_departments(is_active=True)
    assert dict(rec.last.url.params)["is_active"] == "true"


def test_a_false_filter_is_sent_and_not_dropped() -> None:
    """``is_active=False`` means "show the deactivated ones", not "no filter"."""
    rec = Recorder(json={"items": [], "total": 0, "limit": 50, "offset": 0})
    client_for(rec).list_departments(is_active=False)
    assert dict(rec.last.url.params)["is_active"] == "false"


def test_clean_drops_empty_strings() -> None:
    assert _clean({"search": "", "limit": 25}) == {"limit": 25}


def test_pagination_arguments_reach_the_url() -> None:
    rec = Recorder(json={"items": [], "total": 0, "limit": 25, "offset": 50})
    client_for(rec).my_history(limit=25, offset=50)
    query = dict(rec.last.url.params)
    assert query["limit"] == "25"
    assert query["offset"] == "50"


# ------------------------------------------------------- endpoints exist
# Every endpoint the dashboard depends on, in one list so the mocked test and
# the OpenAPI contract test below can never check different sets.
_DASHBOARD_CALLS = [
    (lambda c: c.me(), "GET", "/api/v1/auth/me"),
    (lambda c: c.logout(), "POST", "/api/v1/auth/logout"),
    (lambda c: c.punch_in(), "POST", "/api/v1/attendance/punch-in"),
    (lambda c: c.punch_out(), "POST", "/api/v1/attendance/punch-out"),
    (lambda c: c.my_history(), "GET", "/api/v1/attendance/me/history"),
    (lambda c: c.chat("hi"), "POST", "/api/v1/chat"),
    (lambda c: c.list_departments(), "GET", "/api/v1/admin/departments"),
    (lambda c: c.list_shifts(), "GET", "/api/v1/admin/shifts"),
    (lambda c: c.list_assignments(), "GET", "/api/v1/admin/assignments"),
    (lambda c: c.list_users(), "GET", "/api/v1/admin/users"),
    (lambda c: c.list_audit_logs(), "GET", "/api/v1/admin/audit-logs"),
    (lambda c: c.report_daily(MON), "GET", "/api/v1/reports/daily"),
    (
        lambda c: c.report_daily_summaries(MON, FRI),
        "GET",
        "/api/v1/reports/daily-summaries",
    ),
    (lambda c: c.report_trends(MON, FRI), "GET", "/api/v1/reports/trends"),
    (
        lambda c: c.report_late_arrivals(MON, FRI),
        "GET",
        "/api/v1/reports/late-arrivals",
    ),
    (lambda c: c.report_absences(MON, FRI), "GET", "/api/v1/reports/absences"),
    (
        lambda c: c.report_absenteeism(MON, FRI),
        "GET",
        "/api/v1/reports/absenteeism",
    ),
]


@pytest.mark.parametrize(("call", "method", "path"), _DASHBOARD_CALLS)
def test_each_method_targets_the_path_it_claims(call, method, path) -> None:
    """Guards against a typo'd path, which would otherwise surface as a 404
    only once someone opened that page."""
    rec = Recorder(json={"items": [], "total": 0, "limit": 1, "offset": 0})
    call(client_for(rec))
    assert rec.last.method == method
    assert rec.last.url.path == path


def test_punch_in_never_sends_a_user_id() -> None:
    """Identity comes from the token. A body field would be a second answer."""
    import json as jsonlib

    rec = Recorder(json={})
    client_for(rec).punch_in()
    assert "user_id" not in jsonlib.loads(rec.last.content)


def test_today_turns_a_404_into_an_empty_state() -> None:
    """Before the first punch of the day there is no record -- not an error."""
    rec = Recorder(status=404, json={"detail": "No attendance record for today"})
    assert client_for(rec).today() is None


def test_today_still_raises_on_a_real_failure() -> None:
    rec = Recorder(status=500, json={"detail": "boom"})
    with pytest.raises(ApiUnavailableError):
        client_for(rec).today()


def test_chat_sends_the_history_it_was_given() -> None:
    import json as jsonlib

    rec = Recorder(json={"answer": "ok", "grounded": True, "tools_used": []})
    client_for(rec).chat("and then?", [{"role": "user", "content": "hi"}])
    body = jsonlib.loads(rec.last.content)
    assert body["message"] == "and then?"
    assert body["history"] == [{"role": "user", "content": "hi"}]


def test_a_204_response_does_not_fail_on_an_empty_body() -> None:
    rec = Recorder(status=204, text="")
    assert client_for(rec).deactivate_shift("s1") is None


# --------------------------------------------------------- pure UI logic
@pytest.mark.parametrize(
    ("total", "limit", "offset", "expected"),
    [
        (93, 25, 25, (26, 50, 4)),
        (93, 25, 75, (76, 93, 4)),
        (0, 25, 0, (0, 0, 1)),
        (25, 25, 0, (1, 25, 1)),
        (1, 25, 0, (1, 1, 1)),
    ],
)
def test_page_bounds(total, limit, offset, expected) -> None:
    assert components.page_bounds(total, limit, offset) == expected


def test_an_empty_result_never_reports_zero_pages() -> None:
    """ "Page 1 of 0" reads like a bug to anyone who sees it."""
    assert components.page_bounds(0, 25, 0)[2] == 1


def test_a_valid_range_has_no_complaint() -> None:
    assert components.validate_range(MON, FRI) is None


def test_an_inverted_range_is_refused_before_the_round_trip() -> None:
    assert components.validate_range(FRI, MON) is not None


def test_a_single_day_range_is_valid() -> None:
    assert components.validate_range(MON, MON) is None


def test_a_range_longer_than_the_api_allows_is_refused_here() -> None:
    complaint = components.validate_range(MON, MON + dt.timedelta(days=400))
    assert complaint is not None and "366" in complaint


def test_the_range_limit_is_inclusive_of_both_ends() -> None:
    """365 days apart is 366 days of range, which is exactly the maximum."""
    assert components.validate_range(MON, MON + dt.timedelta(days=365)) is None
    assert components.validate_range(MON, MON + dt.timedelta(days=366)) is not None


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "0m"), (45, "45m"), (60, "1h"), (95, "1h 35m"), (480, "8h")],
)
def test_format_minutes(minutes, expected) -> None:
    assert components.format_minutes(minutes) == expected


def test_unknown_minutes_do_not_read_as_zero() -> None:
    """``worked_minutes`` is null until punch-out; "0m" would be a lie."""
    assert components.format_minutes(None) == "--"


def test_percent_renders_the_api_fraction_as_a_percentage() -> None:
    assert components.percent(0.9231) == "92.3%"
    assert components.percent(None) == "--"


def test_humanise_status_labels_and_marks_a_status() -> None:
    rendered = components.humanise_status("half_day")
    assert rendered == "Half day"
    assert "Late" in components.humanise_status("late")


def test_a_status_never_relies_on_colour_alone() -> None:
    """Every status with a colour also carries an icon wherever it is shown."""
    for status in ("present", "late", "absent", "on_leave"):
        assert ":material/" in components.humanise_status(status)


def test_format_timestamp_leaves_a_missing_value_blank() -> None:
    assert components.format_timestamp(None) == "--"


def test_format_timestamp_passes_through_an_unparseable_value() -> None:
    assert components.format_timestamp("not a date") == "not a date"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "not_started"),
        ({}, "not_started"),
        ({"check_in_at": None, "check_out_at": None}, "not_started"),
        ({"check_in_at": "2026-05-04T09:00:00Z", "check_out_at": None}, "open"),
        (
            {
                "check_in_at": "2026-05-04T09:00:00Z",
                "check_out_at": "2026-05-04T18:00:00Z",
            },
            "closed",
        ),
    ],
)
def test_punch_state(record, expected) -> None:
    assert components.punch_state(record) == expected


# ------------------------------------------------------------- navigation
def test_an_employee_is_never_offered_an_admin_page() -> None:
    offered = pages_for("employee")
    assert not set(offered) & set(ADMIN_ONLY_PAGES)


def test_an_unknown_role_gets_the_employee_pages_only() -> None:
    """A role this build does not know about must not fall open."""
    assert pages_for("manager") == EMPLOYEE_PAGES
    assert pages_for(None) == EMPLOYEE_PAGES


def test_an_admin_keeps_their_own_attendance_pages() -> None:
    offered = pages_for("admin")
    assert set(EMPLOYEE_PAGES) <= set(offered)
    assert set(ADMIN_ONLY_PAGES) <= set(offered)


def test_every_offered_page_has_something_to_render() -> None:
    for role in ("employee", "admin"):
        for page in pages_for(role):
            assert page in RENDERERS


# ---------------------------------------------------------- chat history
def test_history_is_trimmed_to_what_the_api_accepts() -> None:
    messages = [{"role": "user", "content": str(n)} for n in range(25)]
    assert len(trim_history(messages)) == 10


def test_trimming_keeps_the_most_recent_turns_in_order() -> None:
    messages = [{"role": "user", "content": str(n)} for n in range(12)]
    kept = trim_history(messages, max_turns=3)
    assert [m["content"] for m in kept] == ["9", "10", "11"]


def test_trimming_sends_only_role_and_content() -> None:
    """tools_used is for display here; asserting it back would let a client
    claim a lookup that never happened."""
    kept = trim_history(
        [{"role": "assistant", "content": "x", "grounded": True, "tools_used": [1]}]
    )
    assert kept == [{"role": "assistant", "content": "x"}]


def test_a_short_history_is_sent_whole() -> None:
    messages = [{"role": "user", "content": "one"}]
    assert trim_history(messages) == messages


# ----------------------------------------------------------------- charts
def _summaries(n: int = 3) -> list[dict]:
    return [
        {
            "work_date": (MON + dt.timedelta(days=i)).isoformat(),
            "expected": 10,
            "present": 8,
            "late": 2,
            "half_day": 0,
            "absent": 1,
            "on_leave": 1,
            "attendance_rate": 0.888,
            "absence_rate": 0.111,
        }
        for i in range(n)
    ]


def _trend(n: int = 3) -> list[dict]:
    return [
        {
            "period_start": (MON + dt.timedelta(days=i)).isoformat(),
            "period_end": (MON + dt.timedelta(days=i)).isoformat(),
            "expected": 10,
            "attended": 9,
            "late": 2,
            "absent": 1,
            "on_leave": 0,
            "attendance_rate": 0.9,
            "absence_rate": 0.1,
            "average_late_minutes": 12.5,
        }
        for i in range(n)
    ]


def test_the_trend_chart_plots_one_series_and_needs_no_legend() -> None:
    """Absence rate is 1 - attendance over the same base; a second line would
    carry no new information."""
    fig = charts.attendance_trend(_trend(), LIGHT)
    assert len(fig.data) == 1
    assert fig.layout.showlegend is False


def test_the_trend_chart_converts_fractions_to_percentages() -> None:
    fig = charts.attendance_trend(_trend(), LIGHT)
    assert list(fig.data[0].y) == [90.0, 90.0, 90.0]
    assert fig.layout.yaxis.range == (0, 100)


def test_the_trend_chart_uses_the_mark_specs() -> None:
    fig = charts.attendance_trend(_trend(), LIGHT)
    assert fig.data[0].line.width == charts.LINE_WIDTH
    assert fig.data[0].marker.size >= 8


def test_the_composition_stack_sums_to_the_expected_headcount() -> None:
    """Present includes the late, so stacking both raw would double-count."""
    fig = charts.status_composition(_summaries(1), LIGHT)
    stacked = sum(trace.y[0] for trace in fig.data)
    assert stacked == _summaries(1)[0]["expected"]


def test_the_composition_stack_labels_every_segment() -> None:
    fig = charts.status_composition(_summaries(), LIGHT)
    assert {trace.name for trace in fig.data} == {
        "Present",
        "Late",
        "Absent",
        "On leave",
    }


def test_the_composition_stack_uses_status_colours_not_series_colours() -> None:
    """Late is amber because late is a warning, not because it is series two."""
    colours = status_colours(LIGHT)
    for trace in charts.status_composition(_summaries(), LIGHT).data:
        assert trace.marker.color == colours[trace.name]
        assert trace.marker.color != LIGHT.series


def test_stacked_segments_are_separated_by_a_surface_gap() -> None:
    for trace in charts.status_composition(_summaries(), LIGHT).data:
        assert trace.marker.line.color == LIGHT.surface
        assert trace.marker.line.width == charts.SEGMENT_GAP


def test_absenteeism_ranks_the_worst_group_at_the_top() -> None:
    rows = [
        {
            "group_label": "Low",
            "absence_rate": 0.05,
            "absent_days": 1,
            "expected_days": 20,
            "attendance_rate": 0.95,
        },
        {
            "group_label": "High",
            "absence_rate": 0.4,
            "absent_days": 8,
            "expected_days": 20,
            "attendance_rate": 0.6,
        },
    ]
    fig = charts.absenteeism_by_group(rows, LIGHT)
    # Plotly draws the first category at the bottom, so the worst is last.
    assert fig.data[0].y[-1] == "High"


def test_absenteeism_uses_one_hue_for_every_bar() -> None:
    """Colouring by rank would repaint every bar when a filter changed."""
    rows = [
        {
            "group_label": c,
            "absence_rate": r,
            "absent_days": 1,
            "expected_days": 20,
            "attendance_rate": 1 - r,
        }
        for c, r in (("A", 0.1), ("B", 0.2), ("C", 0.3))
    ]
    fig = charts.absenteeism_by_group(rows, LIGHT)
    assert isinstance(fig.data[0].marker.color, str)
    assert fig.data[0].marker.color == LIGHT.series


def test_absenteeism_labels_every_bar_directly() -> None:
    """The relief rule: the value is readable without separating the colours."""
    rows = [
        {
            "group_label": "A",
            "absence_rate": 0.125,
            "absent_days": 2,
            "expected_days": 16,
            "attendance_rate": 0.875,
        }
    ]
    fig = charts.absenteeism_by_group(rows, LIGHT)
    assert list(fig.data[0].text) == ["12.5%"]


@pytest.mark.parametrize(
    "builder",
    [charts.attendance_trend, charts.status_composition, charts.absenteeism_by_group],
)
def test_every_chart_survives_having_no_data(builder) -> None:
    """An empty range is routine; it must say so, not raise."""
    fig = builder([], LIGHT)
    assert fig.layout.annotations


@pytest.mark.parametrize("pal", [LIGHT, DARK])
def test_charts_draw_against_their_own_surface_in_both_modes(pal) -> None:
    fig = charts.attendance_trend(_trend(), pal)
    assert fig.layout.paper_bgcolor == pal.surface
    assert fig.data[0].line.color == pal.series


def test_the_dark_mode_series_is_its_own_step_not_an_inversion() -> None:
    assert DARK.series != LIGHT.series


def test_status_colours_are_identical_in_both_modes() -> None:
    """A status colour means the same thing wherever it appears."""
    assert status_colours(LIGHT) == status_colours(DARK)


def test_a_legend_gets_its_own_room_above_the_plot() -> None:
    """Plotly puts the title and a legend in the same band above the plot, so
    without a wider top margin the legend is drawn over the title. Caught by
    rendering the chart and looking at it; pinned here so it stays fixed."""
    with_legend = charts.status_composition(_summaries(), LIGHT)
    assert with_legend.layout.showlegend is not False
    assert with_legend.layout.margin.t == charts.TOP_MARGIN_WITH_LEGEND


def test_a_chart_without_a_legend_does_not_pay_for_one() -> None:
    assert charts.attendance_trend(_trend(), LIGHT).layout.margin.t == charts.TOP_MARGIN


# ------------------------------------------------------- render smoke tests
# Streamlit's own harness runs the real script in-process against a stubbed
# client. These assert only that a page renders without raising -- which is
# exactly the class of mistake the pure-function tests above cannot catch,
# such as a widget argument Streamlit has since removed.
STUBBED_APP = """
from unittest.mock import MagicMock
from dashboard import session

EMPTY = {"items": [], "total": 0, "limit": 25, "offset": 0}

stub = MagicMock()
stub.today.return_value = {
    "id": "r1", "work_date": "2026-05-04", "shift_id": "s1",
    "check_in_at": "2026-05-04T09:05:00+00:00", "check_out_at": None,
    "status": "late", "source": "web", "worked_minutes": None,
    "late_minutes": 5, "early_leave_minutes": 0, "overtime_minutes": 0,
    "notes": None, "is_open": True,
}
stub.report_daily.return_value = {
    "work_date": "2026-05-04", "expected": 10, "present": 8, "late": 2,
    "half_day": 0, "absent": 1, "on_leave": 1,
    "attendance_rate": 0.888, "absence_rate": 0.111,
}
for name in (
    "my_history", "list_departments", "list_shifts", "list_assignments",
    "list_users", "list_audit_logs", "report_daily_summaries", "report_trends",
    "report_late_arrivals", "report_absences", "report_absenteeism",
):
    getattr(stub, name).return_value = EMPTY

session.get_client = lambda: stub
session.current_user = lambda: {
    "id": "u1", "full_name": "Ada Admin", "employee_code": "EMP-0001",
    "role": "admin", "email": "a@b.com", "employment_status": "active",
    "is_active": True, "department_id": None,
}

import dashboard.app as app
app.main()
"""


@pytest.mark.parametrize("page", pages_for("admin"))
def test_every_page_renders_without_raising(page) -> None:
    from streamlit.testing.v1 import AppTest

    app_test = AppTest.from_string(STUBBED_APP, default_timeout=60)
    app_test.session_state["nav"] = page
    app_test.run()

    assert not app_test.exception, [e.value for e in app_test.exception]
    assert not app_test.error, [e.value for e in app_test.error]


def test_the_login_screen_renders_for_an_anonymous_visitor() -> None:
    from streamlit.testing.v1 import AppTest

    app_test = AppTest.from_string(
        "from dashboard import session\n"
        "session.current_user = lambda: None\n"
        "from dashboard.api_client import ApiClient\n"
        "ApiClient.health = lambda self: {'version': '1', 'environment': 'test'}\n"
        "import dashboard.app as app\n"
        "app.main()\n",
        default_timeout=60,
    )
    app_test.run()

    assert not app_test.exception, [e.value for e in app_test.exception]
    assert [t.value for t in app_test.title] == ["Attendance Management Platform"]
    # The sign-in and registration forms, and nothing requiring a token.
    assert {"Sign in", "Create account"} <= {b.label for b in app_test.button}


# ------------------------------------------------------- API contract tests
# The mocked tests above prove the client sends what it means to; these prove
# the API actually offers it. Without them a stub can happily describe an
# endpoint that does not exist, or a response shape the server never returns
# -- which is exactly how the /admin/users mismatch survived its first round
# of tests. No database or server is needed: the schema comes from the app.
def _openapi() -> dict:
    from app.main import create_app

    return create_app().openapi()


def test_every_endpoint_the_dashboard_calls_exists_in_the_api() -> None:
    schema = _openapi()
    for _call, method, path in _DASHBOARD_CALLS:
        assert path in schema["paths"], f"{path} is not served by the API"
        assert method.lower() in schema["paths"][path], f"{method} {path} missing"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/users",
        "/api/v1/admin/departments",
        "/api/v1/admin/shifts",
        "/api/v1/admin/assignments",
        "/api/v1/admin/audit-logs",
        "/api/v1/attendance/me/history",
        "/api/v1/reports/daily-summaries",
        "/api/v1/reports/trends",
        "/api/v1/reports/late-arrivals",
        "/api/v1/reports/absences",
        "/api/v1/reports/absenteeism",
    ],
)
def test_every_paginated_listing_returns_the_same_envelope(path) -> None:
    """A listing that takes limit/offset must report a total.

    /admin/users used to take both and return a bare list, leaving a caller
    unable to tell a full page from the last one -- and breaking any client
    that assumed the envelope every other listing uses.
    """
    schema = _openapi()
    operation = schema["paths"][path]["get"]

    names = {p["name"] for p in operation.get("parameters", [])}
    assert {"limit", "offset"} <= names, f"{path} does not paginate"

    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]
    model = ref.get("$ref", "").rsplit("/", 1)[-1]
    properties = schema["components"]["schemas"][model]["properties"]
    assert {"items", "total", "limit", "offset"} <= set(properties), (
        f"{path} returns {model}, which is not the standard page envelope"
    )

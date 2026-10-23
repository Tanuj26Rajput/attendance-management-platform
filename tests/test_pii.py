"""Redaction unit tests. No database, no network.

The negative cases matter as much as the positive ones: a redactor that eats
every number would make the logs useless and would quietly be turned off.
"""

from __future__ import annotations

import logging

import pytest

from app.core.pii import (
    REDACTED,
    RedactingFilter,
    is_sensitive_key,
    mask_email,
    mask_employee_code,
    mask_phone,
    redact,
    redact_text,
)


# ------------------------------------------------------------- masking
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("jane.doe@acme.com", "j*******@acme.com"),
        ("a@b.co", "a*@b.co"),
    ],
)
def test_mask_email_keeps_the_domain(value, expected) -> None:
    assert mask_email(value) == expected


def test_mask_email_never_returns_the_local_part() -> None:
    masked = mask_email("verydistinctivename@acme.com")
    assert "verydistinctivename" not in masked
    assert masked.endswith("@acme.com")


@pytest.mark.parametrize("value", [None, ""])
def test_mask_email_handles_missing_values(value) -> None:
    assert mask_email(value) == value


def test_mask_email_on_a_non_address_is_opaque() -> None:
    """Something that is not an address still must not pass through intact."""
    assert mask_email("not-an-address") == "*" * len("not-an-address")


def test_mask_phone_keeps_the_last_four() -> None:
    assert mask_phone("+1 (555) 123-4567").endswith("4567")
    assert "555" not in mask_phone("+1 (555) 123-4567")


def test_mask_employee_code_leaves_it_recognisable() -> None:
    masked = mask_employee_code("EMP-12345678")
    assert masked.startswith("EMP-")
    assert masked.endswith("5678")
    assert "1234" not in masked


def test_mask_employee_code_handles_short_values() -> None:
    assert mask_employee_code("AB") == "**"
    assert mask_employee_code(None) == ""


# ------------------------------------------------------------ redaction
def test_email_is_redacted() -> None:
    assert redact_text("contact jane.doe@acme.com now") == f"contact {REDACTED} now"


def test_jwt_is_redacted() -> None:
    token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    out = redact_text(f"Authorization failed for {token}")
    assert token not in out
    assert REDACTED in out


def test_bearer_header_is_redacted() -> None:
    out = redact_text("Authorization: Bearer abcdef123456789xyz")
    assert "abcdef123456789xyz" not in out


def test_openrouter_api_key_is_redacted() -> None:
    key = "sk-or-v1-0123456789abcdef0123456789abcdef"
    assert key not in redact_text(f"using {key}")


def test_phone_numbers_are_redacted() -> None:
    for number in ("+1 555 123 4567", "(555) 123-4567", "+919876543210"):
        assert number not in redact_text(f"call {number}")


# --- the negative cases: things that must survive ---
@pytest.mark.parametrize(
    "keep",
    [
        "2026-05-04",
        "work_date=2026-05-04 status=late",
        "late_minutes=65",
        "duration_ms=1234",
        "rounds=3 tools=absences",
        "attendance_rate=0.9231",
    ],
)
def test_ordinary_log_content_survives(keep) -> None:
    """Dates and metrics are the whole point of the log line."""
    assert redact_text(keep) == keep


def test_a_uuid_survives() -> None:
    actor = "64624dba-202a-4c51-89fc-f29737711e18"
    assert redact_text(f"actor={actor}") == f"actor={actor}"


# -------------------------------------------------------- structures
def test_sensitive_keys_are_detected() -> None:
    assert is_sensitive_key("password")
    assert is_sensitive_key("hashed_password")
    assert is_sensitive_key("X-API-Key")
    assert is_sensitive_key("apiKey")
    assert is_sensitive_key("Authorization")
    assert not is_sensitive_key("work_date")


def test_redact_removes_sensitive_values_by_key() -> None:
    out = redact({"email": "a@b.com", "hashed_password": "$argon2id$v=19$..."})
    assert out["hashed_password"] == REDACTED
    assert out["email"] == REDACTED


def test_redact_recurses_into_nested_structures() -> None:
    out = redact(
        {
            "user": {"contact": "jane@acme.com", "token": "abc"},
            "items": [{"phone": "+1 555 123 4567"}],
        }
    )
    assert out["user"]["contact"] == REDACTED
    assert out["user"]["token"] == REDACTED
    assert REDACTED in str(out["items"])


def test_redact_leaves_non_string_scalars_alone() -> None:
    out = redact({"count": 42, "rate": 0.5, "ok": True, "nothing": None})
    assert out == {"count": 42, "rate": 0.5, "ok": True, "nothing": None}


def test_redact_is_depth_bounded() -> None:
    """A deeply nested structure must not spin or recurse without limit."""
    deep: dict = {}
    node = deep
    for _ in range(50):
        node["next"] = {}
        node = node["next"]
    assert redact(deep) is not None


# ------------------------------------------------------------- filter
def test_logging_filter_scrubs_the_message() -> None:
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="login for jane.doe@acme.com",
        args=(),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert "jane.doe@acme.com" not in record.getMessage()


def test_logging_filter_scrubs_interpolated_arguments() -> None:
    """The careless logger.info("user %s", user.email) is the real risk."""
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="user %s logged in",
        args=("jane.doe@acme.com",),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert "jane.doe@acme.com" not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_logging_filter_keeps_useful_arguments() -> None:
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="work_date=%s late=%d",
        args=("2026-05-04", 65),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert record.getMessage() == "work_date=2026-05-04 late=65"


# ------------------------------------------------- uvicorn access logging
def test_the_access_log_is_redacted_too() -> None:
    """uvicorn's access logger does not propagate to the root one, so it has
    to be filtered directly. It logs the full request target, which is where
    a search term -- and therefore anything someone pasted into a box --
    ends up."""
    import io

    from app.core.logging import configure_logging

    configure_logging("INFO")

    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    access = logging.getLogger("uvicorn.access")
    access.addHandler(handler)
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:5000",
            "GET",
            "/api/v1/admin/departments?search=jane.doe@acme.com",
            "1.1",
            200,
        )
    finally:
        access.removeHandler(handler)

    written = captured.getvalue()
    assert "jane.doe@acme.com" not in written
    assert REDACTED in written
    # The useful part of the line survives, or the log is worth nothing.
    assert "/api/v1/admin/departments" in written
    assert "200" in written

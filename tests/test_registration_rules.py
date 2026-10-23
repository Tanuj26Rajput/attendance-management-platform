"""Registration must never be a route to admin. No database required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.auth import UserRegister


def test_schema_has_no_role_field() -> None:
    assert "role" not in UserRegister.model_fields


def test_role_in_payload_is_rejected_outright() -> None:
    """extra='forbid' makes a privilege-escalation attempt fail loudly."""
    with pytest.raises(ValidationError) as exc:
        UserRegister(
            email="a@example.com",
            password="a-long-enough-password",
            full_name="A",
            role="admin",
        )
    assert "extra_forbidden" in str(exc.value)


@pytest.mark.parametrize("field", ["is_active", "employment_status", "employee_code"])
def test_other_privileged_fields_are_also_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        UserRegister(
            email="a@example.com",
            password="a-long-enough-password",
            full_name="A",
            **{field: "x"},
        )


def test_email_is_normalised_to_lowercase() -> None:
    """users.email has a CHECK (email = lower(email)) constraint."""
    u = UserRegister(
        email="  MiXeD@Example.COM  ",
        password="a-long-enough-password",
        full_name="A",
    )
    assert u.email == "mixed@example.com"


def test_short_passwords_are_rejected() -> None:
    with pytest.raises(ValidationError):
        UserRegister(email="a@example.com", password="short", full_name="A")

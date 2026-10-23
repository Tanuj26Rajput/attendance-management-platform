"""Unit tests for hashing and tokens. No database required."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.security import (
    ExpiredTokenError,
    TokenError,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_is_argon2id_and_salted() -> None:
    pw = "correct horse battery"
    a, b = hash_password(pw), hash_password(pw)
    assert a.startswith("$argon2id$")
    assert a != b, "identical passwords must not produce identical hashes"


def test_verify_accepts_correct_and_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery")
    assert verify_password(hashed, "correct horse battery") is True
    assert verify_password(hashed, "wrong horse battery") is False


def test_verify_with_no_stored_hash_is_false() -> None:
    """Unknown-user path: still returns False without raising."""
    assert verify_password(None, "anything at all") is False


def test_verify_tolerates_a_corrupt_hash() -> None:
    assert verify_password("not-a-real-hash", "anything at all") is False


def test_roundtrip_token_carries_subject_and_role(settings) -> None:
    uid = uuid.uuid4()
    token, jti, expires_at = create_access_token(uid, "employee", settings=settings)
    payload = decode_token(token, settings=settings)
    assert payload.subject == uid
    assert payload.role == "employee"
    assert payload.jti == jti
    # exp is an integer unix timestamp, so sub-second precision is lost.
    assert payload.expires_at == expires_at.replace(microsecond=0)


def test_expired_token_is_rejected(settings) -> None:
    token, _, _ = create_access_token(
        uuid.uuid4(),
        "employee",
        settings=settings,
        expires_delta=timedelta(seconds=-30),
    )
    with pytest.raises(ExpiredTokenError):
        decode_token(token, settings=settings)


def test_tampered_signature_is_rejected(settings) -> None:
    token, _, _ = create_access_token(uuid.uuid4(), "employee", settings=settings)
    head, payload, _sig = token.split(".")
    with pytest.raises(TokenError):
        decode_token(f"{head}.{payload}.deadbeef", settings=settings)


def test_token_signed_with_another_key_is_rejected(settings) -> None:
    token, _, _ = create_access_token(uuid.uuid4(), "admin", settings=settings)
    other = settings.model_copy(update={"JWT_SECRET_KEY": "a-completely-different-key"})
    with pytest.raises(TokenError):
        decode_token(token, settings=other)


def test_garbage_is_rejected(settings) -> None:
    with pytest.raises(TokenError):
        decode_token("not.a.jwt", settings=settings)


def test_role_claim_is_not_trusted_for_authorisation(settings) -> None:
    """A self-signed role claim decodes, but routes re-read the role from the DB.

    This documents why get_current_user loads the user rather than trusting the
    token: the claim itself is only as good as the signature.
    """
    token, _, _ = create_access_token(uuid.uuid4(), "admin", settings=settings)
    assert decode_token(token, settings=settings).role == "admin"

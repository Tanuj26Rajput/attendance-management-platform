"""Redaction of personal and secret data.

Two different jobs live here, and they want opposite defaults:

* **Masking** (``mask_email``, ``mask_phone``) keeps a value recognisable to
  someone who already knows it -- enough to confirm "yes, that is my address"
  without printing it. Used where a human needs to identify a record.
* **Redaction** (``redact_text``, ``redact``) removes the value entirely. Used
  for logs and anything heading towards a language model, where nobody needs to
  recognise anything and the only safe amount is none.

The patterns deliberately over-match rather than under-match. A redacted
timestamp in a log line is a small annoyance; a leaked phone number is not, and
logs are copied into tickets, chat and bug reports where they outlive any
decision about who should see them.
"""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "[REDACTED]"

# Key names whose *value* is always removed, whatever it looks like. Matched
# case-insensitively against a substring, so "hashed_password" and
# "X-API-Key" are both caught.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "hashed_password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "jwt",
        "credential",
        "credentials",
        "session",
        "cookie",
        "private_key",
    }
)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A JWT is three base64url segments; the leading "eyJ" is the encoded "{".
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")

# Provider keys: OpenRouter and OpenAI both use the sk- prefix.
_API_KEY = re.compile(r"\bsk-(?:or-v\d-)?[A-Za-z0-9_-]{16,}\b")

_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

# Phone numbers, in three shapes. Each is anchored so that a date (2026-05-04)
# or an ordinary integer cannot match -- an over-eager phone pattern that ate
# every date would make the logs useless.
_PHONE_PATTERNS = (
    # +1 555 123 4567 / +91-98765-43210
    re.compile(r"(?<![\w.])\+\d{1,3}[\s.-]?\d{2,5}[\s.-]?\d{3,5}[\s.-]?\d{0,5}(?!\w)"),
    # (555) 123-4567 / 555.123.4567
    re.compile(r"(?<![\d.])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d-])"),
    # A bare run of 10+ digits.
    re.compile(r"(?<![\d.-])\d{10,15}(?![\d-])"),
)


def mask_email(value: str | None) -> str | None:
    """``jane.doe@acme.com`` -> ``j*******@acme.com``.

    The domain survives because it is rarely the sensitive part and often the
    useful one ("an external address, not a staff one"), while the local part
    is what identifies the person.
    """
    if not value or "@" not in value:
        return _mask_opaque(value)

    local, _, domain = value.partition("@")
    if not local:
        return _mask_opaque(value)
    return f"{local[0]}{'*' * max(len(local) - 1, 1)}@{domain}"


def mask_phone(value: str | None) -> str | None:
    """Keep the last four digits, which is what people use to recognise a number."""
    if not value:
        return value
    digits = [c for c in value if c.isdigit()]
    if len(digits) < 4:
        return _mask_opaque(value)
    return f"{'*' * (len(digits) - 4)}{''.join(digits[-4:])}"


def mask_employee_code(value: str | None) -> str:
    """``EMP-12345678`` -> ``EMP-****5678``.

    Employee codes are internal identifiers people genuinely use to refer to
    each other, so this is the gentlest mask here: enough to spot your own code
    in a list, not enough to enumerate the workforce from a log file.
    """
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:4]}{'*' * max(len(value) - 8, 0)}{value[-4:]}"


def _mask_opaque(value: str | None) -> str | None:
    if value is None:
        return None
    if not value:
        return value
    return "*" * len(value)


def redact_text(text: str) -> str:
    """Strip anything that looks like a secret or a contact detail."""
    if not text:
        return text

    # Secrets first: a bearer token can contain a JWT, and redacting the inner
    # value first would leave the "Bearer" wrapper holding a partial match.
    redacted = _BEARER.sub(REDACTED, text)
    redacted = _JWT.sub(REDACTED, redacted)
    redacted = _API_KEY.sub(REDACTED, redacted)
    redacted = _EMAIL.sub(REDACTED, redacted)
    for pattern in _PHONE_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def is_sensitive_key(key: str) -> bool:
    """Match regardless of separator style.

    Header and field names arrive as ``X-API-Key``, ``api_key`` and ``apiKey``
    depending on where they came from; normalising the separators means the
    list above does not need an entry for each spelling.
    """
    normalised = key.lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalised for marker in SENSITIVE_KEYS)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a structure before logging or sending it onward.

    Keys named like secrets lose their value outright; everything else is
    scanned for patterns. Depth is bounded so a self-referential structure
    cannot spin here.
    """
    if _depth > 6:
        return REDACTED

    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and is_sensitive_key(key)
            else redact(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, _depth=_depth + 1) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


class RedactingFilter(logging.Filter):
    """Scrubs every log record on its way to a handler.

    A filter rather than a formatter, so it applies no matter how a handler is
    configured, and catches the careless ``logger.info("user %s", user.email)``
    that no code review will reliably spot forever.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            else:
                record.args = tuple(redact(arg) for arg in record.args)
        return True

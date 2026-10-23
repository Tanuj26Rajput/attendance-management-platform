"""OpenRouter client wiring.

OpenRouter exposes an OpenAI-compatible API, so the official ``openai`` SDK
talks to it unchanged -- only ``base_url`` and the key differ. This module is
infrastructure only: it builds a configured client and the default request
kwargs. Prompting and domain logic belong in ``app/services/``.
"""

from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from app.core.config import Settings, get_settings


class MissingAPIKeyError(RuntimeError):
    """Raised when an AI call is attempted without OPENROUTER_API_KEY set."""


def build_client(settings: Settings | None = None) -> AsyncOpenAI:
    settings = settings or get_settings()

    if not settings.OPENROUTER_API_KEY:
        raise MissingAPIKeyError(
            "OPENROUTER_API_KEY is not set. Add it to .env -- see .env.example."
        )

    # Optional; OpenRouter uses these only for public leaderboard attribution.
    default_headers: dict[str, str] = {}
    if settings.OPENROUTER_SITE_URL:
        default_headers["HTTP-Referer"] = settings.OPENROUTER_SITE_URL
    if settings.OPENROUTER_APP_NAME:
        default_headers["X-Title"] = settings.OPENROUTER_APP_NAME

    return AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_BASE_URL,
        timeout=settings.OPENROUTER_TIMEOUT_SECONDS,
        max_retries=settings.OPENROUTER_MAX_RETRIES,
        default_headers=default_headers or None,
    )


@lru_cache
def get_client() -> AsyncOpenAI:
    """Cached client; safe to share across requests."""
    return build_client()


def default_completion_kwargs(settings: Settings | None = None) -> dict[str, Any]:
    """Model and reasoning options every chat completion should carry.

    Step 3.7 Flash reasons on every request -- it cannot be disabled, only
    dialled between low/medium/high. Reasoning tokens bill at the completion
    rate, so `low` is the cheap setting for simple extraction work.
    """
    settings = settings or get_settings()
    return {
        "model": settings.OPENROUTER_MODEL,
        "max_tokens": settings.OPENROUTER_MAX_TOKENS,
        "extra_body": {
            "reasoning": {"effort": settings.OPENROUTER_REASONING_EFFORT},
        },
    }

"""AI layer: anomaly detection, summarisation, and natural-language queries
over attendance data.

Only the provider wiring exists so far; see ``app/ai/client.py``.
"""

from app.ai.client import (
    MissingAPIKeyError,
    build_client,
    default_completion_kwargs,
    get_client,
)

__all__ = [
    "MissingAPIKeyError",
    "build_client",
    "default_completion_kwargs",
    "get_client",
]

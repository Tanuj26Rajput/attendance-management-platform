"""Logging setup: human-readable lines locally, JSON in a deployment.

Every handler carries a ``RedactingFilter``: logs get copied into tickets,
chat threads and bug reports, outliving any decision about who should see
what, so emails, phone numbers, tokens and API keys are scrubbed on the way
out rather than trusted not to be passed in. The filter runs before either
formatter, so both are equally scrubbed.

``LOG_FORMAT=json`` emits one JSON object per line, which is what a log
shipper needs to index a field rather than regex a message. The default stays
``text`` so running locally is unchanged.
"""

import datetime as dt
import json
import logging
import sys

from app.core.pii import RedactingFilter

_CONFIGURED = False

# Attributes LogRecord always carries; anything else on a record was put
# there by the caller as structured context and is worth emitting.
_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, stdlib only -- no new dependency."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": dt.datetime.fromtimestamp(
                record.created, tz=dt.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # getMessage() applies the args the filter already redacted.
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS:
                payload[key] = value

        # default=str so a UUID or a datetime in the extras cannot turn a log
        # call into a TypeError at the worst possible moment.
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter()
        if fmt == "json"
        else logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers on these loggers, and the access
    # logger does not propagate to the root one, so the filter above never
    # sees its records. An access line carries the full request target --
    # "GET /api/v1/admin/departments?search=jane.doe@acme.com" -- so without
    # this the one log everybody tails is the one that is not scrubbed.
    # The filter goes on the logger rather than its handlers because uvicorn
    # may replace the handlers after this runs; a logger-level filter applies
    # to anything logged through it either way.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactingFilter())

    logging.getLogger("uvicorn.access").propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

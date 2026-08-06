from __future__ import annotations

import contextvars
import json
import logging
import sys
from typing import Any

from .config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] request_id=%(request_id)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Correlation ID propagated from the HTTP middleware into every log record
# emitted during that request. Background tasks created with asyncio.create_task
# inherit the caller context, so the request_id survives run-finalize paths too.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id",
    default="-",
)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True

RESET = "\033[0m"
COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[35m",
}


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.request_id = getattr(record, "request_id", None) or request_id_var.get()
        message = super().format(record)
        color = COLORS.get(record.levelno)
        if not color:
            return message
        return f"{color}{message}{RESET}"


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter_cls = ColorFormatter if settings.log_color else logging.Formatter
    handler.setFormatter(formatter_cls(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    logging.getLogger("httpx").setLevel(logging.DEBUG if settings.debug else logging.WARNING)
    logging.getLogger("freebuff2api").debug(
        "logging configured debug=%s level=%s body_chars=%s color=%s",
        settings.debug,
        settings.log_level,
        settings.log_body_chars,
        settings.log_color,
    )


def render_debug(value: Any, limit: int) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)

    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "cookie", "set-cookie"}:
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted

"""Structured JSON logging to stdout, with secret redaction.

Usage: log = get_logger(__name__); log.info("event_name", extra={"post_id": ...})
Anything passed via `extra` lands as a top-level JSON key.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_SECRETS: set[str] = set()
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def register_secret(value: str | None) -> None:
    """Redact this value from every future log line (e.g. a LinkedIn token loaded from the DB)."""
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    for s in secrets or []:
        register_secret(s)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO, and Telegram URLs embed the bot token.
    for noisy in ("httpx", "httpcore", "httpx2", "telegram", "hpack", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

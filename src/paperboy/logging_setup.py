"""Redacted logging: JSON file handler + optional rich console handler.

Credentials (session string, `api_hash`, phone, login code) must never reach
a log file (spec §2/§3). Rather than trust every call site to avoid logging
them, sensitive values are registered once via `register_secret` and a
`RedactionFilter` scrubs every record before it reaches any handler.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

_registered_secrets: set[str] = set()

_MASK = "***"


def register_secret(value: str) -> None:
    """Register a sensitive string so every log record has it masked out.

    Safe to call repeatedly (e.g. once a session/api_hash is loaded); empty
    strings are ignored so an unset secret doesn't (harmlessly but
    pointlessly) match everything.
    """
    if value:
        _registered_secrets.add(value)


class RedactionFilter(logging.Filter):
    """Masks every registered secret out of a record before it is formatted.

    Renders `record.getMessage()` once (applying any %-args), substitutes
    registered secrets with `***`, then rewrites `record.msg` with the
    redacted text and clears `record.args` so no handler ever re-renders the
    original, unredacted arguments.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        # Longest-first so one secret that is a substring of another (e.g. a
        # short test token contained in a longer one) doesn't leave a partial
        # match unmasked.
        for secret in sorted(_registered_secrets, key=len, reverse=True):
            if secret in message:
                message = message.replace(secret, _MASK)
        record.msg = message
        record.args = ()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(path: Path, console: bool = True) -> None:
    """Configure the `paperboy` logger: JSON file handler + optional rich console.

    Idempotent: clears any handlers/filters from a prior call so tests (and a
    CLI re-invocation within one process) can call it more than once safely.
    """
    logger = logging.getLogger("paperboy")
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for f in list(logger.filters):
        logger.removeFilter(f)

    # A filter attached to the `paperboy` logger itself is only consulted
    # by `Logger.filter()` for records logged *through that logger* — the
    # app logs exclusively through children (`paperboy.cli`, the `log`
    # threaded into recipes/collectors), and `Logger.callHandlers` never
    # re-checks an ancestor logger's own filters for a record bubbling up
    # from a child. Attaching one shared (stateless) `RedactionFilter`
    # instance to each handler instead works for every record regardless of
    # which logger emitted it, since `callHandlers` always applies each
    # handler's own filters.
    rf = RedactionFilter()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(_JsonFormatter())
    file_handler.addFilter(rf)
    logger.addHandler(file_handler)

    if console:
        from rich.logging import RichHandler

        rich_handler = RichHandler(show_path=False)
        rich_handler.addFilter(rf)
        logger.addHandler(rich_handler)

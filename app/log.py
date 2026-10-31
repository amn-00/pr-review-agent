"""JSON logging with per-request / per-job context.

The webhook and the job call set_log_context() with delivery_id, repo, pr and sha, so every
log line they emit carries those fields and one PR can be traced end to end with a single
filter. contextvars are per asyncio task, so concurrent requests and jobs don't mix contexts.
"""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

_context: ContextVar[dict[str, object] | None] = ContextVar("log_context", default=None)


def _current() -> dict[str, object]:
    return _context.get() or {}


def set_log_context(**fields: object) -> None:
    """Replace the context. Call at the start of a request or job."""
    _context.set(fields)


def bind_log_context(**fields: object) -> None:
    """Add fields to the current context (e.g. run_id once it is known)."""
    _context.set({**_current(), **fields})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_current(),
        }
        # Extra one-off fields: logger.info("...", extra={"fields": {"status": "done"}})
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(level: str) -> None:
    """Send every log line, including uvicorn's and arq's, through one JSON handler.

    Called after uvicorn/arq have configured their own loggers (in the API lifespan and the
    worker's on_startup), so we can take over their handlers.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

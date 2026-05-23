"""Structured logging setup.

We use structlog so logs become structured key/value events that journald
can render either as plain text (terminal/dev) or as JSON (server/production).

The format is chosen by the ``ORCLAW_LOG_FORMAT`` env var: ``text`` (default
when stderr is a TTY) or ``json`` (default on the server).

Optionally, structlog events can be tapped into a SQLite table (see
:mod:`orclaw.event_store`) by setting ``ORCLAW_LOG_EVENTS_DB`` to a
DB path. The tap is best-effort — never breaks the calling code if the
DB is unavailable.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import structlog

from orclaw.event_store import insert_event


def _sqlite_event_sink(db_path: Path) -> structlog.types.Processor:
    """structlog processor that mirrors each event into SQLite.

    The processor must return ``event_dict`` unchanged so the next
    processor (the renderer) still sees it. We strip standard keys
    (level/timestamp/event) before persisting — the rest is the
    structured payload.
    """

    def _processor(_logger: object, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event = event_dict.get("event", "")
        level = event_dict.get("level", "info")
        module = event_dict.get("logger")
        attrs = {
            k: v
            for k, v in event_dict.items()
            if k not in {"event", "level", "timestamp", "logger"}
        }
        insert_event(
            db_path,
            level=str(level),
            event=str(event),
            module=str(module) if module else None,
            attrs=attrs,
        )
        return event_dict

    return _processor


def configure_logging(level: str = "INFO", fmt: str | None = None) -> None:
    """Configure structlog and stdlib logging.

    Idempotent: safe to call multiple times (the second call re-applies the
    configuration with the new arguments).
    """
    fmt = fmt or os.environ.get("ORCLAW_LOG_FORMAT") or ("text" if sys.stderr.isatty() else "json")

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        timestamper,
    ]

    # Optional SQLite tap — inserted BEFORE the renderer so it sees the
    # structured event dict, not a string. Opt-in via env var; off in
    # tests and dev unless explicitly set.
    sqlite_db = os.environ.get("ORCLAW_LOG_EVENTS_DB")
    if sqlite_db:
        shared_processors.append(_sqlite_event_sink(Path(sqlite_db)))

    renderer: structlog.types.Processor
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quiet noisy third-party libs at WARNING by default.
    logging.basicConfig(level=logging.WARNING)


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Return a logger pre-bound with the given context.

    Example:
        log = get_logger("orchestrator", run_id="abc123")
        log.info("batch_started", layer=2, size=3)
    """
    log = structlog.get_logger(name)
    if initial_context:
        log = log.bind(**initial_context)
    return log

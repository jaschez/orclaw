"""SQLite-backed sink for structlog events — a queryable internal log.

Why this exists
---------------
journalctl is great for live debugging but awful for incident analysis
("what happened around 14:30 last Tuesday that led to PR #42 hard-blocking?").
We need a queryable, indexed store of structured events that survives
log-rotation and is accessible from the specialist agent without sudo.

Design
------
- One table (``events``) defined in ``schema.sql``: ``ts``, ``level``,
  ``event``, ``module``, ``attrs`` (JSON).
- :func:`insert_event` is a low-overhead INSERT (~0.1ms on WAL).
- :func:`query_events` returns dict rows for the specialist tool.
- :func:`purge_older_than` for retention (default 30 days).

The structlog wiring lives in :mod:`orclaw.logging` — this module
stays a pure data layer so it's trivially testable.

Failure mode is *opt-in + best-effort*: if the configured DB path can't
be opened, every INSERT silently drops. Logging must never break the
caller.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


#: Levels the schema CHECK accepts. Any structlog level not in here is
#: coerced to "info" to avoid breaking the constraint on a typo.
_ALLOWED_LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error", "critical"})


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection.

    We don't share connections across threads — each call opens, writes,
    closes. WAL mode keeps this cheap (≤1ms typical). The structlog
    processor wraps this in a try/except so a missing DB never bubbles
    up to the caller.
    """
    conn = sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL was set at init_db time; just confirm it's still on.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def insert_event(
    db_path: Path,
    *,
    level: str,
    event: str,
    attrs: dict[str, object] | None = None,
    module: str | None = None,
) -> None:
    """Append one event row. Silent no-op on any I/O failure."""
    safe_level = level.lower() if level.lower() in _ALLOWED_LEVELS else "info"
    payload = json.dumps(attrs or {}, default=str, ensure_ascii=False)
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO events (level, event, module, attrs) VALUES (?, ?, ?, ?)",
                (safe_level, event, module, payload),
            )
    except (sqlite3.Error, OSError):
        # Never propagate logging errors — caller is doing important work.
        return


def query_events(
    db_path: Path,
    *,
    since: datetime | None = None,
    level: str | None = None,
    event_pattern: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return events matching the filters, most-recent first.

    Filters compose conjunctively. ``event_pattern`` uses SQL ``LIKE``
    semantics — pass ``%dispatch%`` to match any event whose name
    contains "dispatch".
    """
    where: list[str] = []
    params: list[object] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(since.isoformat().replace("+00:00", "Z"))
    if level is not None:
        where.append("level = ?")
        params.append(level.lower())
    if event_pattern is not None:
        where.append("event LIKE ?")
        params.append(event_pattern)

    sql = "SELECT id, ts, level, event, module, attrs FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))

    try:
        with _connect(db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []

    return [
        {
            "id": int(r["id"]),
            "ts": r["ts"],
            "level": r["level"],
            "event": r["event"],
            "module": r["module"],
            "attrs": _decode_attrs(r["attrs"]),
        }
        for r in rows
    ]


def _decode_attrs(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}
    return loaded if isinstance(loaded, dict) else {"_value": loaded}


def purge_older_than(db_path: Path, *, days: int) -> int:
    """Delete events older than ``days`` days. Returns rows deleted."""
    if days < 1:
        raise ValueError("days must be >= 1")
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    try:
        with _connect(db_path) as conn:
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            return int(cur.rowcount or 0)
    except sqlite3.Error:
        return 0


def count_events(db_path: Path) -> int:
    """Total rows in the events table — useful for retention dashboards."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.Error:
        return 0

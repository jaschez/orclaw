"""Populate the local SQLite with synthetic data for the dashboard demo.

Called from ``orclaw demo-seed``. Idempotent — re-running clobbers
whatever was in the DB and re-seeds from scratch so the demo always
looks fresh.

Layout produced
---------------

- 3 layers of batches:
    * Layer 0: 4 batches — 1 merged, 1 in_progress, 2 pending
    * Layer 1: 4 batches — all pending (active layer once L0 finishes)
    * Layer 2: 4 batches — pending (waiting on L1)
- ~25 runs spread across the layers + a few failed ones for realism
- ~40 events from the last hour: dispatches, pollback merges, one
  saturation alert, dashboard writes
- engine_state seeded with: paused=false, max_in_flight_override unset

The point is "every screen has something to render", not statistical
accuracy. Tune by editing the constants below.
"""

from __future__ import annotations

import contextlib
import json
import random
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orclaw.db import connect, init_db
from orclaw.logging import get_logger

log = get_logger(__name__)

# Deterministic-ish for reproducible screenshots.
_RNG = random.Random(42)


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def _iso(dt: datetime) -> str:
    """SQLite-friendly ISO timestamp (no TZ, space separator)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _wipe(conn: sqlite3.Connection) -> None:
    for table in ("events", "runs", "batches", "engine_state"):
        # Table may not exist on first run — that's fine, init_db()
        # will create it before we reach the seeders.
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(f"DELETE FROM {table}")


def _seed_batches(conn: sqlite3.Connection) -> list[tuple[int, int, str]]:
    """Return (issue_number, layer, status) for everything we created."""
    now = datetime.now(UTC)
    rows: list[tuple[int, int, str, str | None, int | None, str, str]] = []

    # Layer 0 — half done, half live
    rows += [
        (0, 41, "merged", _short_id(), 142, _iso(now - timedelta(hours=4)), _iso(now - timedelta(hours=2))),
        (0, 42, "in_progress", _short_id(), 145, _iso(now - timedelta(hours=2)), _iso(now - timedelta(minutes=30))),
        (0, 43, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
        (0, 44, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
    ]
    # Layer 1 — all pending, becomes active when L0 finishes
    rows += [
        (1, 51, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
        (1, 52, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
        (1, 53, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
        (1, 54, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
    ]
    # Layer 2 — pending, blocked
    rows += [
        (2, 61, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
        (2, 62, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
        (2, 63, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
        (2, 64, "pending", None, None, _iso(now - timedelta(hours=2)), _iso(now - timedelta(hours=2))),
    ]

    conn.executemany(
        "INSERT INTO batches "
        "(layer, issue_number, status, implementer_run_id, pr_number, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return [(r[1], r[0], r[2]) for r in rows]


def _seed_runs(conn: sqlite3.Connection) -> None:
    """A mix of implementer + reviewer runs across recent time."""
    now = datetime.now(UTC)

    runs = [
        # The completed implementer for #41 + its reviewer
        ("impl-aaaa1111aaaa", "implementer", "claude-sonnet", 41, 142, "feat/health-endpoint", "success",
         now - timedelta(hours=4), now - timedelta(hours=3, minutes=45), 900, None),
        ("rev-bbbb2222bbbb", "reviewer", "claude-sonnet", None, 142, "feat/health-endpoint", "success",
         now - timedelta(hours=3, minutes=40), now - timedelta(hours=3, minutes=35), 300, None),
        # In-flight implementer for #42
        ("impl-cccc3333cccc", "implementer", "claude-sonnet", 42, 145, "feat/structured-logging", "running",
         now - timedelta(minutes=18), None, None, None),
        # An earlier implementer that failed + got retried
        ("impl-dddd4444dddd", "implementer", "claude-sonnet", 42, None, "feat/structured-logging", "failed",
         now - timedelta(hours=2, minutes=10), now - timedelta(hours=2), 600, "exceeded retry budget"),
        # Batch planner runs (oneshot)
        ("planner-eeee5555ee", "batch_planner", "n/a", None, None, None, "success",
         now - timedelta(minutes=42), now - timedelta(minutes=42), 3, None),
        ("planner-ffff6666ff", "batch_planner", "n/a", None, None, None, "success",
         now - timedelta(minutes=12), now - timedelta(minutes=12), 2, None),
        # Specialist queries (from claude.ai mobile)
        ("spec-gggg7777ggg", "specialist", "claude-sonnet", None, None, None, "success",
         now - timedelta(minutes=5), now - timedelta(minutes=5), 1, None),
    ]

    # Pad with a handful of older successes for the runs-table density
    for _ in range(15):
        ts = now - timedelta(hours=_RNG.randint(6, 30))
        agent = _RNG.choice(["implementer", "reviewer"])
        status = _RNG.choice(["success", "success", "success", "failed"])
        runs.append((
            f"{agent[:4]}-{_short_id()}", agent, "claude-sonnet",
            _RNG.randint(20, 40), _RNG.randint(100, 140), None,
            status, ts, ts + timedelta(minutes=_RNG.randint(2, 15)),
            _RNG.randint(120, 900), None,
        ))

    conn.executemany(
        "INSERT INTO runs "
        "(id, agent, model, issue_number, pr_number, branch, status, "
        " started_at, finished_at, duration_seconds, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                _iso(r[7]) if r[7] else None,
                _iso(r[8]) if r[8] else None,
                r[9], r[10],
            )
            for r in runs
        ],
    )


def _seed_events(conn: sqlite3.Connection) -> None:
    """Plausible event log for the last hour."""
    now = datetime.now(UTC)
    events: list[tuple[str, str, str, str]] = []

    sample = [
        ("info", "orchestrator_tick_started", {"orchestrator_run_id": "abc123"}),
        ("info", "pollback_started", {}),
        ("info", "pollback_complete", {"merged": 0, "reviewed": 1}),
        ("info", "implementer_dispatch_started", {"issue": 42, "run_id": "impl-cccc3333cccc"}),
        ("info", "implementer_dispatch_complete", {"issue": 42, "comment_id": 9001}),
        ("warning", "dispatch_skipped", {"issue": 43, "reason": "already in_progress"}),
        ("info", "reviewer_dispatch_started", {"pr": 142}),
        ("info", "reviewer_dispatch_complete", {"pr": 142, "comment_id": 9050}),
        ("info", "batch_planner_run_complete", {"layers": 3, "added": 0}),
        ("warning", "saturation", {"active": 2, "cap": 2, "pending": 6}),
        ("info", "overlay_prompt_set", {"name": "implementer-comment.md", "bytes": 412}),
        ("info", "concurrency_override_set", {"value": 3}),
        ("info", "telegram_bot_command", {"text": "/status"}),
        ("info", "auto_deploy_success", {"sha": "ec6f13e"}),
    ]

    for _ in range(40):
        level, event, attrs = _RNG.choice(sample)
        ts = now - timedelta(minutes=_RNG.randint(0, 60))
        events.append((_iso(ts), level, event, json.dumps(attrs)))

    conn.executemany(
        "INSERT INTO events (ts, level, event, attrs) VALUES (?, ?, ?, ?)",
        events,
    )


def _seed_engine_state(conn: sqlite3.Connection) -> None:
    """Just enough engine_state for the tuning page to show real values."""
    conn.execute(
        "INSERT INTO engine_state (key, value) VALUES (?, ?)",
        ("orchestrator_paused", "false"),
    )


def run(db_path: Path | str) -> None:
    """Wipe + reseed the local DB. Returns nothing."""
    db_path = Path(db_path)
    init_db(db_path)
    with connect(db_path) as conn:
        _wipe(conn)
        rows = _seed_batches(conn)
        _seed_runs(conn)
        _seed_events(conn)
        _seed_engine_state(conn)
    log.info("demo_seed_complete", db=str(db_path), batches=len(rows))

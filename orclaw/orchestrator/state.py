"""Read/write accessors over the SQLite state.

Thin functional wrappers — no ORM. Each function takes a sqlite3.Connection
so callers can compose multiple reads in the same transaction.

The orchestrator loop and the CLI both consume these.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from orclaw.logging import get_logger
from orclaw.models import BatchStatus

if TYPE_CHECKING:
    import sqlite3

log = get_logger(__name__)


# --- Composed views --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchSnapshot:
    """Aggregate view of the current ``batches`` table used by the orchestrator.

    Materialised once per tick so downstream decisions operate on a
    consistent snapshot, even if the DB changes mid-iteration.
    """

    layers: list[list[int]]
    """``layers[k]`` = issue numbers at layer k, ordered by (priority, number)."""
    status_by_issue: dict[int, BatchStatus]
    """Latest status for each known issue."""
    issues_pending: frozenset[int]
    issues_in_progress: frozenset[int]
    issues_merged: frozenset[int]
    issues_failed: frozenset[int]
    issues_skipped: frozenset[int]

    @property
    def total_issues(self) -> int:
        return len(self.status_by_issue)


@dataclass(frozen=True, slots=True)
class OrchestratorState:
    """The full snapshot the orchestrator reads at the top of each tick."""

    paused: bool
    last_planner_run: datetime | None
    batches: BatchSnapshot
    active_run_count: int
    effective_max_in_flight: int | None = None
    """Effective cap (override or settings default). ``None`` only in
    legacy paths where load_state() was called without a settings ref —
    callers should fall back to settings.concurrency.max_in_flight."""


# --- Engine state key/value ------------------------------------------------


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO engine_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')",
        (key, value),
    )


def is_paused(conn: sqlite3.Connection) -> bool:
    """True iff the orchestrator should refrain from dispatching new work."""
    return _get_state(conn, "orchestrator_paused") == "true"


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    """Toggle the orchestrator pause flag.

    Note: paused=true does NOT abort in-flight work — it only prevents
    new dispatches. Use ``orclaw pause --hard`` (sprint 3+) for
    that.
    """
    _set_state(conn, "orchestrator_paused", "true" if paused else "false")
    log.info("orchestrator_pause_changed", paused=paused)


_CONCURRENCY_OVERRIDE_KEY = "max_in_flight_override"


def get_concurrency_override(conn: sqlite3.Connection) -> int | None:
    """Optional runtime override of ``settings.concurrency.max_in_flight``.

    Editable from the dashboard so we can dial concurrency up/down without
    a redeploy. ``None`` means "no override — use the config value".
    """
    raw = _get_state(conn, _CONCURRENCY_OVERRIDE_KEY)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def set_concurrency_override(conn: sqlite3.Connection, value: int | None) -> None:
    """Persist (or clear) the runtime concurrency override.

    Pass ``None`` to remove the override and fall back to config. Negative
    values are rejected — pass 0 to fully pause dispatching instead (the
    orchestrator already has a dedicated paused flag for that, but 0
    works as a soft equivalent).

    Note: the override can only *lower* the effective cap below the config
    ceiling (``settings.concurrency.max_in_flight``); values above the
    ceiling are clamped down by :func:`effective_max_in_flight`. The raw
    value is still stored verbatim so the dashboard can show what was
    requested vs. what is in effect.
    """
    if value is None:
        conn.execute("DELETE FROM engine_state WHERE key = ?", (_CONCURRENCY_OVERRIDE_KEY,))
        log.info("concurrency_override_cleared")
        return
    if value < 0:
        raise ValueError(f"concurrency override must be ≥ 0, got {value}")
    _set_state(conn, _CONCURRENCY_OVERRIDE_KEY, str(value))
    log.info("concurrency_override_set", value=value)


def effective_max_in_flight(
    conn: sqlite3.Connection,
    *,
    settings_default: int,
) -> int:
    """Single source of truth for the active concurrency cap.

    Resolution:
      1. Start from ``settings.concurrency.max_in_flight`` (the config
         ceiling — defaults to 1, a single Claude task at a time).
      2. If a runtime override (engine_state.max_in_flight_override) is
         set, it may only **lower** the cap below that ceiling — never
         raise it above. So ``effective = min(override, settings_default)``.

    Clamping to the ceiling (rather than letting the override win
    outright) keeps the single-flight guarantee intact: no dashboard edit
    can accidentally push the engine past the configured parallelism. To
    run more than one dispatch in parallel, raise ``max_in_flight`` in
    config — not via the override.

    The orchestrator loop AND the dashboard both call this so the number
    shown in the UI matches what the loop will actually use next tick.
    """
    override = get_concurrency_override(conn)
    if override is None:
        return settings_default
    return min(override, settings_default)


def last_planner_run(conn: sqlite3.Connection) -> datetime | None:
    """Timestamp of the last successful planner run, if any."""
    value = _get_state(conn, "last_planner_run")
    if not value or value == "1970-01-01T00:00:00Z":
        return None
    try:
        # SQLite stores the result of datetime('now') as 'YYYY-MM-DD HH:MM:SS'.
        return datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None


# --- Batches snapshot ------------------------------------------------------


#: Statuses that should still participate in layered scheduling. Anything
#: outside this set (merged/failed/skipped) is a terminal state — its
#: row stays in the DB for audit, but it must NOT appear in ``layers``
#: or :func:`next_executable_batch` would happily re-dispatch it.
_LIVE_BATCH_STATUSES: frozenset[str] = frozenset(
    {BatchStatus.PENDING.value, BatchStatus.IN_PROGRESS.value}
)


def load_batch_snapshot(conn: sqlite3.Connection) -> BatchSnapshot:
    """Read the current ``batches`` table into a frozen snapshot.

    Layer construction is **restricted to live statuses** (pending +
    in_progress). Terminal rows (merged/failed/skipped) are still
    reflected in the corresponding ``issues_*`` frozensets — callers
    use those to decide e.g. whether a PR has already merged — but they
    no longer pollute the layers themselves, which would cause
    :func:`next_executable_batch` to re-pick a skipped issue.
    """
    rows = conn.execute(
        "SELECT layer, issue_number, status FROM batches ORDER BY layer, issue_number"
    ).fetchall()

    layers: list[list[int]] = []
    status_by_issue: dict[int, BatchStatus] = {}
    by_status: dict[str, set[int]] = {
        "pending": set(),
        "in_progress": set(),
        "merged": set(),
        "failed": set(),
        "skipped": set(),
    }

    for row in rows:
        layer_idx = int(row["layer"])
        status = BatchStatus(row["status"])
        status_by_issue[int(row["issue_number"])] = status
        by_status[status.value].add(int(row["issue_number"]))

        if status.value in _LIVE_BATCH_STATUSES:
            while len(layers) <= layer_idx:
                layers.append([])
            layers[layer_idx].append(int(row["issue_number"]))

    return BatchSnapshot(
        layers=layers,
        status_by_issue=status_by_issue,
        issues_pending=frozenset(by_status["pending"]),
        issues_in_progress=frozenset(by_status["in_progress"]),
        issues_merged=frozenset(by_status["merged"]),
        issues_failed=frozenset(by_status["failed"]),
        issues_skipped=frozenset(by_status["skipped"]),
    )


def issues_in_progress(conn: sqlite3.Connection) -> frozenset[int]:
    """Issue numbers currently flagged ``in_progress`` in ``batches``."""
    rows = conn.execute(
        "SELECT issue_number FROM batches WHERE status = ?",
        (BatchStatus.IN_PROGRESS.value,),
    ).fetchall()
    return frozenset(int(row["issue_number"]) for row in rows)


#: Queued runs older than this are treated as zombies (external agent /
#: GitHub Action never picked them up) and excluded from the in-flight
#: count so they can't block the dispatcher indefinitely. The row stays
#: in the DB with ``status='queued'`` for forensics — no destructive write.
STALE_QUEUED_MINUTES = 60


def active_run_count(conn: sqlite3.Connection) -> int:
    """Number of ``runs`` rows currently in flight for concurrency capping.

    Counts ``running`` rows plus *fresh* ``queued`` rows (started within the
    last :data:`STALE_QUEUED_MINUTES`). See the constant docstring for why
    stale ``queued`` rows are excluded.

    This is the **single source of truth** for the in-flight count — all
    consumers (orchestrator dispatch cap, dashboard, CLI, MCP get_status,
    Telegram bot) MUST call this helper rather than duplicate the SQL,
    so the cap and what every UI displays never diverge.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs "
        "WHERE status = 'running' "
        f"   OR (status = 'queued' AND started_at >= datetime('now', '-{STALE_QUEUED_MINUTES} minutes'))"
    ).fetchone()
    return int(row["n"]) if row else 0


def load_state(
    conn: sqlite3.Connection,
    *,
    settings_max_in_flight: int | None = None,
) -> OrchestratorState:
    """Build the full per-tick snapshot in one go.

    ``settings_max_in_flight`` is the default cap from config. When passed,
    the returned state carries the *effective* cap (override or default)
    via :attr:`OrchestratorState.effective_max_in_flight`, so callers
    don't need a second DB read.
    """
    effective = (
        effective_max_in_flight(conn, settings_default=settings_max_in_flight)
        if settings_max_in_flight is not None
        else None
    )
    return OrchestratorState(
        paused=is_paused(conn),
        last_planner_run=last_planner_run(conn),
        batches=load_batch_snapshot(conn),
        active_run_count=active_run_count(conn),
        effective_max_in_flight=effective,
    )


# --- Batch transitions -----------------------------------------------------


def create_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    agent: str,
    model: str,
    issue_number: int | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
    status: str = "queued",
    notes: str | None = None,
) -> None:
    """Insert a row into ``runs`` (typically status='queued' at dispatch time)."""
    conn.execute(
        "INSERT INTO runs (id, agent, model, issue_number, pr_number, branch, status, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, agent, model, issue_number, pr_number, branch, status, notes),
    )


def mark_batch_in_progress(
    conn: sqlite3.Connection,
    *,
    issue_number: int,
    implementer_run_id: str,
) -> None:
    """Promote a pending batch row to ``in_progress``."""
    conn.execute(
        "UPDATE batches SET status = ?, implementer_run_id = ?, "
        "updated_at = datetime('now') "
        "WHERE issue_number = ? AND status = ?",
        (
            BatchStatus.IN_PROGRESS.value,
            implementer_run_id,
            issue_number,
            BatchStatus.PENDING.value,
        ),
    )


def mark_batch_merged(
    conn: sqlite3.Connection,
    *,
    issue_number: int,
    pr_number: int,
) -> None:
    """Mark a batch row as ``merged`` once its PR lands."""
    conn.execute(
        "UPDATE batches SET status = ?, pr_number = ?, "
        "updated_at = datetime('now') "
        "WHERE issue_number = ? AND status IN (?, ?)",
        (
            BatchStatus.MERGED.value,
            pr_number,
            issue_number,
            BatchStatus.PENDING.value,
            BatchStatus.IN_PROGRESS.value,
        ),
    )


def mark_batch_failed(conn: sqlite3.Connection, *, issue_number: int) -> None:
    """Mark a batch as ``failed`` (e.g., implementer gave up after retries)."""
    conn.execute(
        "UPDATE batches SET status = ?, updated_at = datetime('now') "
        "WHERE issue_number = ? AND status NOT IN (?, ?)",
        (
            BatchStatus.FAILED.value,
            issue_number,
            BatchStatus.MERGED.value,
            BatchStatus.SKIPPED.value,
        ),
    )

"""Planner: fetch issues, run algorithm, persist layers.

This is the I/O shell around :mod:`algorithm`. It:

1. Fetches all open non-OPS issues from GitHub
2. Builds the dependency graph and computes layers
3. Writes/updates rows in the ``batches`` table

It does NOT spawn implementers, post @claude comments, or move cards on
the project board — those are the orchestrator's job.

Idempotent: running twice with no changes in GitHub yields the same DB
state. The planner is designed to run on a systemd timer every 10 minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from orclaw.batch_planner.algorithm import compute_layers
from orclaw.db import connect
from orclaw.github_client import GitHubAPIConfig, GitHubClient
from orclaw.logging import get_logger
from orclaw.models import BatchStatus, Issue

if TYPE_CHECKING:
    import sqlite3

    from orclaw.config import Settings

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """Summary of one planner run, returned to the caller and logged."""

    layers_count: int
    issues_scanned: int
    issues_planned: int
    issues_pending_added: int
    issues_already_tracked: int
    layers: list[list[int]]


# --- Persistence helpers ---------------------------------------------------


def _existing_active_batches(conn: sqlite3.Connection) -> dict[int, dict[str, object]]:
    """Map of ``issue_number -> row`` for batches in non-terminal states.

    The schema enforces uniqueness of issue_number for active rows
    (``status != 'failed' AND status != 'skipped'``), so this is well-defined.
    """
    rows = conn.execute(
        "SELECT id, layer, issue_number, status, pr_number "
        "FROM batches "
        "WHERE status NOT IN ('failed', 'skipped')"
    ).fetchall()
    return {row["issue_number"]: row for row in rows}


def _upsert_batch(
    conn: sqlite3.Connection,
    *,
    issue_number: int,
    layer: int,
    existing: dict[str, object] | None,
) -> str:
    """Insert a pending batch row for this issue, or refresh its layer.

    Returns one of ``"inserted"``, ``"layer_updated"``, ``"unchanged"``.

    We never **demote** a row from ``in_progress``/``merged`` back to
    ``pending`` — once Claude is on it, planner reruns don't disturb it.
    """
    if existing is None:
        conn.execute(
            "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
            (layer, issue_number, BatchStatus.PENDING.value),
        )
        return "inserted"

    if existing["status"] != BatchStatus.PENDING.value:
        # Don't touch in_progress / merged rows. They live their lifecycle.
        return "unchanged"

    if existing["layer"] == layer:
        return "unchanged"

    conn.execute(
        "UPDATE batches SET layer = ? WHERE id = ?",
        (layer, existing["id"]),
    )
    return "layer_updated"


# --- Public entrypoint -----------------------------------------------------


async def run_planner(settings: Settings) -> PlannerResult:
    """Run one planner cycle. Returns a summary.

    The caller is expected to pass a freshly-loaded :class:`Settings`.
    Logging context is bound automatically.
    """
    log.info("planner_run_started")

    api_config = GitHubAPIConfig(
        repo=settings.github.repo,
        token=settings.github.token,
    )

    async with GitHubClient(api_config) as gh:
        # state=open already excludes closed issues; non-OPS filtering
        # happens in the algorithm via EXCLUDED_LABELS.
        issues: list[Issue] = await gh.list_issues(state="open")

    layers = compute_layers(issues)
    log.info(
        "planner_layers_computed",
        issues_scanned=len(issues),
        layers=len(layers),
        layer_sizes=[len(layer) for layer in layers],
    )

    added = 0
    layer_updated = 0
    unchanged = 0

    with connect(settings.paths.db_path) as conn:
        existing = _existing_active_batches(conn)

        # Within a single transaction so partial planner failures don't
        # leave half-applied layer numbers.
        conn.execute("BEGIN")
        try:
            for layer_index, issue_numbers in enumerate(layers):
                for issue_number in issue_numbers:
                    outcome = _upsert_batch(
                        conn,
                        issue_number=issue_number,
                        layer=layer_index,
                        existing=existing.get(issue_number),
                    )
                    if outcome == "inserted":
                        added += 1
                    elif outcome == "layer_updated":
                        layer_updated += 1
                    else:
                        unchanged += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # Update the engine_state cursor so the dashboard knows the planner
        # ran recently. The schema seeds this key on init_db.
        conn.execute(
            "UPDATE engine_state SET value = datetime('now'), updated_at = datetime('now') "
            "WHERE key = 'last_planner_run'"
        )

    result = PlannerResult(
        layers_count=len(layers),
        issues_scanned=len(issues),
        issues_planned=sum(len(layer) for layer in layers),
        issues_pending_added=added,
        issues_already_tracked=unchanged + layer_updated,
        layers=layers,
    )
    log.info(
        "planner_run_finished",
        layers_count=result.layers_count,
        issues_planned=result.issues_planned,
        added=added,
        layer_updated=layer_updated,
        unchanged=unchanged,
    )
    return result

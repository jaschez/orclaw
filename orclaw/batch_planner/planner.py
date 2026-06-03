"""Planner: fetch issues, run algorithm, persist layers.

This is the I/O shell around :mod:`algorithm`. It:

1. Fetches all open non-OPS issues from GitHub
2. Builds the dependency graph and computes layers
3. Writes/updates rows in the ``batches`` table
4. Audits issue bodies for malformed ``Blocked by`` lines and emits a
   structured event per offender so the operator can fix the body
   before the planner silently swallows the dep again.

It does NOT spawn implementers, post @claude comments, or move cards on
the project board — those are the orchestrator's job.

Idempotent: running twice with no changes in GitHub yields the same DB
state. The planner is designed to run on a systemd timer every 10 minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from orclaw.batch_planner.algorithm import compute_layers, filter_candidates
from orclaw.db import connect
from orclaw.dependency_parser import (
    find_suspicious_blocked_lines,
    parse_blocked_by,
)
from orclaw.event_store import insert_event
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
    # New: issues whose ``Blocked by`` lines couldn't be parsed (e.g.,
    # placeholder codenames the specialist forgot to substitute). Keys are
    # issue numbers; values are the suspicious raw lines for logging.
    issues_with_suspicious_deps: dict[int, list[str]] = field(default_factory=dict)
    # New: issues whose declared deps reference issue numbers that are
    # outside the candidate set (closed, excluded, or non-existent).
    # Distinguishes between "blocker is already done" (fine) and "blocker
    # number doesn't correspond to any open issue" (suspicious, but the
    # planner has no way to tell those two apart with only an open-issues
    # fetch — surfaced for the operator to verify).
    issues_with_unresolved_dep_refs: dict[int, list[int]] = field(default_factory=dict)


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
    repo: str = "",
) -> str:
    """Insert a pending batch row for this issue, or refresh its layer.

    Returns one of ``"inserted"``, ``"layer_updated"``, ``"unchanged"``.

    We never **demote** a row from ``in_progress``/``merged`` back to
    ``pending`` — once Claude is on it, planner reruns don't disturb it.

    ``repo`` (``owner/name``) tags new rows for multi-repo scheduling.
    """
    if existing is None:
        conn.execute(
            "INSERT INTO batches (repo, layer, issue_number, status) VALUES (?, ?, ?, ?)",
            (repo, layer, issue_number, BatchStatus.PENDING.value),
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


# --- Body audits -----------------------------------------------------------


def _audit_issue_bodies(
    issues: list[Issue],
    db_path: str,
) -> tuple[dict[int, list[str]], dict[int, list[int]]]:
    """Walk all issue bodies and flag suspicious dep declarations.

    Two failure modes, both silent until this audit landed:

    1. **Mal-formed ``Blocked by`` lines** — the line LOOKS like a dep but
       the regex didn't extract a number. Most common cause: a placeholder
       codename (``#N1``, ``#P3``) the specialist forgot to replace with a
       real issue number. Other causes: typos (``Blocked by issue 88``),
       mismatched markdown that hides the ``#``.

    2. **Refs to issue numbers outside the candidate set** — the dep
       resolved to a number, but that number isn't any of the open issues
       the planner is considering. Could be: (a) the dep is already closed
       (totally fine, planner correctly ignores), (b) the dep number
       points at a non-existent issue (specialist typo'd a digit, planner
       silently treats as resolved). We can't tell (a) from (b) without a
       second GitHub fetch; surfacing the ref count lets the operator
       eyeball it when something seems off.

    Emits one ``planner_suspicious_deps`` event per offending issue.
    Returns ``(suspicious_lines_by_issue, unresolved_refs_by_issue)`` so
    the caller can include them in the result.
    """
    candidates = filter_candidates(issues)
    candidate_numbers = frozenset(candidates)
    open_numbers = frozenset(i.number for i in issues if i.is_open)

    suspicious: dict[int, list[str]] = {}
    unresolved: dict[int, list[int]] = {}

    for issue in issues:
        if not issue.is_open:
            continue
        lines = find_suspicious_blocked_lines(issue.body)
        declared = parse_blocked_by(issue.body)
        # An "unresolved" ref is a parsed dep that isn't in the current
        # candidate set. If it IS in open_numbers but not candidates, the
        # dep is open-but-excluded (e.g., labelled ``do-not-implement``)
        # which is its own kind of weird; surface it. If it's NOT in
        # open_numbers, it's either closed (fine) or non-existent
        # (suspicious) — we can't tell, but we surface the list for the
        # operator to skim.
        out_of_candidate = sorted(d for d in declared if d not in candidate_numbers)
        # Filter out the harmless "blocker is closed" case: if the ref
        # isn't in open_numbers at all, it's most likely a closed issue
        # that's already done. Only surface ones that look genuinely
        # off — i.e., NEITHER candidate NOR closed-and-resolved. For now
        # we treat "not in candidate_numbers but not in open_numbers" as
        # "closed-and-fine" and don't surface. The operator gets refs
        # that are open-but-excluded (rare, but a real misconfiguration).
        truly_unresolved = [d for d in out_of_candidate if d in open_numbers]

        if lines:
            suspicious[issue.number] = lines
        if truly_unresolved:
            unresolved[issue.number] = truly_unresolved

        if lines or truly_unresolved:
            log.warning(
                "planner_suspicious_deps",
                issue=issue.number,
                suspicious_lines=lines,
                unresolved_open_refs=truly_unresolved,
            )
            insert_event(
                db_path,
                level="warning",
                event="planner_suspicious_deps",
                attrs={
                    "issue_number": issue.number,
                    "suspicious_lines": lines,
                    "unresolved_open_refs": truly_unresolved,
                },
                module=__name__,
            )

    return suspicious, unresolved


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

    # Audit before persistence — events flow into the DB regardless of
    # whether the upsert below succeeds, so the operator sees malformed
    # bodies even if the planner trips later in the transaction.
    suspicious_by_issue, unresolved_by_issue = _audit_issue_bodies(
        issues, settings.paths.db_path
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
                        repo=settings.github.repo,
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
        issues_with_suspicious_deps=suspicious_by_issue,
        issues_with_unresolved_dep_refs=unresolved_by_issue,
    )
    log.info(
        "planner_run_finished",
        layers_count=result.layers_count,
        issues_planned=result.issues_planned,
        added=added,
        layer_updated=layer_updated,
        unchanged=unchanged,
        issues_with_suspicious_deps=len(suspicious_by_issue),
        issues_with_unresolved_dep_refs=len(unresolved_by_issue),
    )
    return result

"""Poll-back: reconcile GitHub state into the local SQLite.

The orchestrator dispatches work as ``@claude`` comments; the agents then
do whatever they do — open a PR, apply a label, etc. — *outside* the
engine's view. Pollback is the inverse: each tick reads GitHub's current
truth and updates the local DB so the next decision sees fresh data.

Two reconciliations happen here:

1. **Reviewer verdicts**: for each open PR with a ``review:*`` terminal
   label, find the matching ``runs`` row (agent='reviewer', queued/running,
   pr_number=N) and:
     - mark the run ``success`` (or ``failed`` on ``review:hard-block``)
     - insert a ``reviews`` row with the verdict
   Idempotent — skipped if a ``reviews`` row already exists for that PR.

2. **Merged PRs**: for each closed+merged PR against ``develop`` since
   the last cursor, mark the corresponding ``batches`` rows (via the PR's
   ``closing_issues``) as ``merged``.

The cursor ``last_merged_pr_check`` lives in ``engine_state`` and is
updated at the end of each successful poll.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from orclaw.github_client import GitHubAPIConfig, GitHubClient
from orclaw.logging import get_logger
from orclaw.models import ReviewVerdict, RunStatus
from orclaw.orchestrator.state import mark_batch_merged

if TYPE_CHECKING:
    import sqlite3

    from orclaw.config import Settings
    from orclaw.models import PullRequest

log = get_logger(__name__)


# --- Label → verdict mapping ----------------------------------------------

#: Map from reviewer-applied label to the verdict + final run status.
#: ``review:hard-block`` marks the run as ``failed`` because the PR is not
#: mergeable without human escalation.
LABEL_TO_VERDICT: dict[str, tuple[ReviewVerdict, RunStatus]] = {
    "review:approved": (ReviewVerdict.APPROVED, RunStatus.SUCCESS),
    "review:minor-fixes-applied": (ReviewVerdict.MINOR_FIXES_APPLIED, RunStatus.SUCCESS),
    "review:needs-changes": (ReviewVerdict.NEEDS_CHANGES, RunStatus.SUCCESS),
    "review:hard-block": (ReviewVerdict.HARD_BLOCK, RunStatus.FAILED),
}


# --- Result DTO ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PollbackResult:
    """Summary of one pollback pass, returned to the caller (logged + CLI)."""

    reviews_completed: list[tuple[int, str]] = field(default_factory=list)
    """``(pr_number, verdict)`` for each reviewer run completed this pass."""
    reviews_skipped: list[tuple[int, str]] = field(default_factory=list)
    """``(pr_number, reason)`` — usually because no matching run row exists."""
    batches_merged: list[tuple[int, int]] = field(default_factory=list)
    """``(issue_number, pr_number)`` for each batch transitioned to merged."""

    @property
    def is_noop(self) -> bool:
        return not (self.reviews_completed or self.batches_merged)


# --- Reviewer verdict reconciliation --------------------------------------


def _pick_verdict_label(pr_labels: frozenset[str]) -> str | None:
    """Return the first ``review:*`` terminal label found on a PR (or None).

    A PR should normally carry exactly one terminal label, but if multiple
    are set (e.g., the reviewer flipped its mind across runs) we prefer
    the most-definitive: hard-block > needs-changes > minor-fixes-applied
    > approved. That ordering avoids "approved" wins over a later
    "needs-changes" sneaking in.
    """
    priority = [
        "review:hard-block",
        "review:needs-changes",
        "review:minor-fixes-applied",
        "review:approved",
    ]
    for label in priority:
        if label in pr_labels:
            return label
    return None


def complete_reviewer_runs(
    conn: sqlite3.Connection,
    prs: list[PullRequest],
) -> PollbackResult:
    """Reconcile reviewer verdicts for the given open PRs into the DB.

    Pure-DB function — does NOT call GitHub. The caller passes in the PRs
    already fetched (so we can share fetches across tick passes).
    """
    completed: list[tuple[int, str]] = []
    skipped: list[tuple[int, str]] = []

    for pr in prs:
        label = _pick_verdict_label(pr.labels)
        if label is None:
            continue

        verdict, new_run_status = LABEL_TO_VERDICT[label]

        # Skip if a reviews row already exists for this PR — idempotent.
        existing = conn.execute(
            "SELECT id FROM reviews WHERE pr_number = ?",
            (pr.number,),
        ).fetchone()
        if existing is not None:
            continue

        # Find the matching reviewer run. There should be exactly one
        # active per PR; if there are none, the PR was reviewed before we
        # tracked it — record a "synthetic" reviews row anyway so the next
        # tick doesn't keep looking.
        run_row = conn.execute(
            "SELECT id, status FROM runs "
            "WHERE agent = 'reviewer' AND pr_number = ? "
            "AND status IN ('queued', 'running') "
            "ORDER BY started_at DESC LIMIT 1",
            (pr.number,),
        ).fetchone()

        conn.execute("BEGIN")
        try:
            if run_row is not None:
                conn.execute(
                    "UPDATE runs SET status = ?, finished_at = datetime('now') WHERE id = ?",
                    (new_run_status.value, run_row["id"]),
                )
                conn.execute(
                    "INSERT INTO reviews (pr_number, run_id, verdict) VALUES (?, ?, ?)",
                    (pr.number, run_row["id"], verdict.value),
                )
            else:
                # Untracked review (PR reviewed before we knew about it).
                conn.execute(
                    "INSERT INTO reviews (pr_number, verdict) VALUES (?, ?)",
                    (pr.number, verdict.value),
                )
                skipped.append((pr.number, "no matching run; recorded synthetic review"))
            conn.execute("COMMIT")
            completed.append((pr.number, verdict.value))
        except Exception as e:
            conn.execute("ROLLBACK")
            log.error("pollback_review_db_failed", pr=pr.number, error=str(e))
            skipped.append((pr.number, f"db error: {e}"))

    return PollbackResult(
        reviews_completed=completed,
        reviews_skipped=skipped,
    )


# --- Merged-PR reconciliation ---------------------------------------------


def mark_merged_batches(
    conn: sqlite3.Connection,
    closed_prs: list[PullRequest],
) -> list[tuple[int, int]]:
    """Transition batches → 'merged' for each closing_issues of merged PRs.

    Pure-DB function. The caller fetches the closed+merged PR list. Skips
    PRs that closed without merging (e.g., manual close, conflict abandons).
    Returns the ``(issue_number, pr_number)`` pairs that were transitioned.
    """
    transitioned: list[tuple[int, int]] = []
    for pr in closed_prs:
        if not pr.merged:
            continue
        for issue_number in pr.closing_issues:
            mark_batch_merged(conn, issue_number=issue_number, pr_number=pr.number)
            transitioned.append((issue_number, pr.number))
    return transitioned


# --- Cursor helpers --------------------------------------------------------

_CURSOR_KEY = "last_merged_pr_check"
#: How far back to look the first time we run (no cursor yet).
_INITIAL_LOOKBACK = timedelta(days=2)


def _get_cursor(conn: sqlite3.Connection) -> datetime:
    """Return the timestamp of the last successful merged-PR poll.

    On a fresh DB, returns ``now - INITIAL_LOOKBACK`` so the first poll
    catches any merges that happened just before the engine came up.
    """
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (_CURSOR_KEY,)).fetchone()
    if row is None or not row["value"]:
        return datetime.now(UTC) - _INITIAL_LOOKBACK
    try:
        return datetime.fromisoformat(row["value"])
    except ValueError:
        # Defensive: malformed cursor → fall back to lookback so we don't
        # accidentally re-process everything.
        return datetime.now(UTC) - _INITIAL_LOOKBACK


def _set_cursor(conn: sqlite3.Connection, ts: datetime) -> None:
    conn.execute(
        "INSERT INTO engine_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')",
        (_CURSOR_KEY, ts.isoformat()),
    )


# --- Public entrypoint -----------------------------------------------------


async def run_pollback(settings: Settings) -> PollbackResult:
    """One pollback cycle: reconcile reviewer verdicts + merged batches.

    Designed to be called at the top of every orchestrator tick, before
    any decision logic. If GitHub is unreachable we log + return an empty
    result rather than failing the whole tick.
    """
    from orclaw.db import connect

    log.info("pollback_started")

    api_config = GitHubAPIConfig(
        repo=settings.github.repo,
        token=settings.github.token,
    )

    try:
        async with GitHubClient(api_config) as gh:
            open_prs = await gh.list_prs(state="open", base="develop")

            # Use the cursor to limit closed-PR fetch.
            with connect(settings.paths.db_path) as conn:
                cursor = _get_cursor(conn)
            closed_prs = await gh.list_prs(state="closed", base="develop", since=cursor)
    except Exception as e:
        log.warning("pollback_fetch_failed", error=str(e))
        return PollbackResult()

    # Reviewer verdicts can land on either an open PR (reviewer ran but
    # the PR hasn't been auto-merged yet) or on a closed PR (the verdict
    # → auto-merge → close happened between two ticks). We feed BOTH
    # lists into complete_reviewer_runs — the de-dup logic on reviews row
    # existence keeps it idempotent. Found in prod on first deploy: an
    # approved PR auto-merged in <30s and we missed the verdict because
    # we only checked open PRs.
    all_prs_with_labels = open_prs + closed_prs

    # All the SQL stays in one connection so the two reconciliations + the
    # cursor bump are atomic at the tick level.
    with connect(settings.paths.db_path) as conn:
        review_result = complete_reviewer_runs(conn, all_prs_with_labels)
        merged_pairs = mark_merged_batches(conn, closed_prs)
        _set_cursor(conn, datetime.now(UTC))

    log.info(
        "pollback_completed",
        reviews_completed=len(review_result.reviews_completed),
        batches_merged=len(merged_pairs),
    )
    return PollbackResult(
        reviews_completed=review_result.reviews_completed,
        reviews_skipped=review_result.reviews_skipped,
        batches_merged=merged_pairs,
    )

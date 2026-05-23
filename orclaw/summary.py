"""Daily / weekly summary builder.

Reads the SQLite tables (``runs``, ``batches``, ``reviews``) and emits a
human-readable digest suitable for Telegram or stdout. Intended to be
called from a systemd timer once a day.

Pure: takes a connection + a time window, returns a string. The CLI
binding and the Telegram post happen in the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True, slots=True)
class SummaryWindow:
    """The numbers we report on for a given period."""

    since: datetime
    until: datetime

    runs_total: int
    runs_success: int
    runs_failed: int
    runs_by_agent: dict[str, int]

    batches_merged: int
    batches_failed: int
    batches_in_progress: int
    batches_pending: int

    reviews_approved: int
    reviews_needs_changes: int
    reviews_hard_block: int

    @property
    def runs_success_rate(self) -> float:
        if self.runs_total == 0:
            return 0.0
        return self.runs_success / self.runs_total


def build_summary(
    conn: sqlite3.Connection,
    *,
    window: timedelta = timedelta(days=1),
) -> SummaryWindow:
    """Read the relevant rows and aggregate into a :class:`SummaryWindow`.

    ``window`` controls how far back we look from ``now``. ``timedelta(days=1)``
    by default for the daily digest; ``timedelta(days=7)`` for the weekly
    retro.
    """
    until = datetime.now(UTC)
    since = until - window
    since_iso = since.isoformat()

    # --- Runs ---
    runs_total_row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE started_at >= ?", (since_iso,)
    ).fetchone()
    runs_total = int(runs_total_row["n"]) if runs_total_row else 0

    runs_success = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE started_at >= ? AND status = 'success'",
            (since_iso,),
        ).fetchone()["n"]
    )
    runs_failed = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE started_at >= ? "
            "AND status IN ('failed', 'timeout', 'aborted')",
            (since_iso,),
        ).fetchone()["n"]
    )
    by_agent_rows = conn.execute(
        "SELECT agent, COUNT(*) AS n FROM runs WHERE started_at >= ? GROUP BY agent",
        (since_iso,),
    ).fetchall()
    runs_by_agent = {row["agent"]: int(row["n"]) for row in by_agent_rows}

    # --- Batches (snapshot, not windowed — these are totals) ---
    batch_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM batches GROUP BY status"
    ).fetchall()
    batch_counts = {row["status"]: int(row["n"]) for row in batch_rows}

    # --- Reviews (windowed) ---
    review_rows = conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM reviews WHERE created_at >= ? GROUP BY verdict",
        (since_iso,),
    ).fetchall()
    review_counts = {row["verdict"]: int(row["n"]) for row in review_rows}

    return SummaryWindow(
        since=since,
        until=until,
        runs_total=runs_total,
        runs_success=runs_success,
        runs_failed=runs_failed,
        runs_by_agent=runs_by_agent,
        batches_merged=batch_counts.get("merged", 0),
        batches_failed=batch_counts.get("failed", 0),
        batches_in_progress=batch_counts.get("in_progress", 0),
        batches_pending=batch_counts.get("pending", 0),
        reviews_approved=review_counts.get("approved", 0)
        + review_counts.get("minor_fixes_applied", 0),
        reviews_needs_changes=review_counts.get("needs_changes", 0),
        reviews_hard_block=review_counts.get("hard_block", 0),
    )


def format_summary_telegram(summary: SummaryWindow) -> str:
    """Render a :class:`SummaryWindow` as a Telegram MarkdownV1 message."""
    hours = int((summary.until - summary.since).total_seconds() / 3600)
    period_label = "24h" if hours == 24 else f"{hours}h"

    by_agent_lines = (
        "\n".join(f"  - {agent}: `{n}`" for agent, n in sorted(summary.runs_by_agent.items()))
        or "  - (none)"
    )

    return (
        f"*📊 orclaw — last {period_label}*\n\n"
        f"*Runs:* `{summary.runs_total}` total "
        f"(`{summary.runs_success}` ✓ / `{summary.runs_failed}` ✗ "
        f"— `{summary.runs_success_rate:.0%}` success)\n"
        f"By agent:\n{by_agent_lines}\n\n"
        f"*Batches (current snapshot):*\n"
        f"  - merged: `{summary.batches_merged}`\n"
        f"  - in_progress: `{summary.batches_in_progress}`\n"
        f"  - pending: `{summary.batches_pending}`\n"
        f"  - failed: `{summary.batches_failed}`\n\n"
        f"*Reviews ({period_label}):*\n"
        f"  - approved/minor: `{summary.reviews_approved}`\n"
        f"  - needs-changes: `{summary.reviews_needs_changes}`\n"
        f"  - hard-block: `{summary.reviews_hard_block}`"
    )

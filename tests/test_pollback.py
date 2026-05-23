"""Tests for the pollback pass (pure DB functions).

These exercise the SQL side of pollback without hitting GitHub. The
caller is responsible for fetching the PRs; we just verify that the
reconciliation logic moves rows the way we expect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from orclaw.db import connect, init_db
from orclaw.models import BatchStatus, PullRequest, RunStatus
from orclaw.orchestrator.pollback import (
    _pick_verdict_label,
    complete_reviewer_runs,
    mark_merged_batches,
)
from orclaw.orchestrator.state import create_run

if TYPE_CHECKING:
    from pathlib import Path


# --- Helpers ---------------------------------------------------------------


def _pr(
    *,
    number: int,
    closing: tuple[int, ...] = (),
    labels: tuple[str, ...] = (),
    merged: bool = False,
    state: str = "open",
) -> PullRequest:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    return PullRequest(
        number=number,
        title=f"pr-{number}",
        body="",
        head_ref=f"feat/{number}",
        base_ref="develop",
        state=state,
        merged=merged,
        labels=frozenset(labels),
        closing_issues=frozenset(closing),
        created_at=now,
        updated_at=now,
        merged_at=now if merged else None,
    )


# --- _pick_verdict_label ---------------------------------------------------


class TestPickVerdictLabel:
    def test_returns_none_when_no_review_label(self) -> None:
        assert _pick_verdict_label(frozenset({"area:schema"})) is None

    def test_picks_approved(self) -> None:
        assert _pick_verdict_label(frozenset({"review:approved"})) == "review:approved"

    def test_hard_block_wins_over_approved(self) -> None:
        # If both are set (reviewer flipped its mind), the stricter verdict wins.
        labels = frozenset({"review:approved", "review:hard-block"})
        assert _pick_verdict_label(labels) == "review:hard-block"

    def test_needs_changes_wins_over_approved(self) -> None:
        labels = frozenset({"review:approved", "review:needs-changes"})
        assert _pick_verdict_label(labels) == "review:needs-changes"


# --- complete_reviewer_runs -----------------------------------------------


class TestCompleteReviewerRuns:
    def test_marks_run_success_and_inserts_review_on_approved(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(
                conn,
                run_id="rev-1",
                agent="reviewer",
                model="sonnet",
                pr_number=200,
                issue_number=88,
            )

        pr = _pr(number=200, labels=("review:approved", "review:pending"))
        with connect(db_path) as conn:
            result = complete_reviewer_runs(conn, [pr])

        assert result.reviews_completed == [(200, "approved")]
        with connect(db_path, read_only=True) as conn:
            run_row = conn.execute(
                "SELECT status, finished_at FROM runs WHERE id = ?", ("rev-1",)
            ).fetchone()
            assert run_row["status"] == RunStatus.SUCCESS.value
            assert run_row["finished_at"] is not None

            review = conn.execute(
                "SELECT verdict, run_id FROM reviews WHERE pr_number = 200"
            ).fetchone()
            assert review["verdict"] == "approved"
            assert review["run_id"] == "rev-1"

    def test_marks_run_failed_on_hard_block(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(
                conn,
                run_id="rev-2",
                agent="reviewer",
                model="sonnet",
                pr_number=201,
            )

        pr = _pr(number=201, labels=("review:hard-block",))
        with connect(db_path) as conn:
            result = complete_reviewer_runs(conn, [pr])

        assert result.reviews_completed == [(201, "hard_block")]
        with connect(db_path, read_only=True) as conn:
            run_row = conn.execute("SELECT status FROM runs WHERE id = ?", ("rev-2",)).fetchone()
            assert run_row["status"] == RunStatus.FAILED.value

    def test_idempotent_when_reviews_row_exists(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(conn, run_id="rev-3", agent="reviewer", model="sonnet", pr_number=202)
            conn.execute(
                "INSERT INTO reviews (pr_number, run_id, verdict) VALUES (?, ?, ?)",
                (202, "rev-3", "approved"),
            )

        pr = _pr(number=202, labels=("review:approved",))
        with connect(db_path) as conn:
            result = complete_reviewer_runs(conn, [pr])

        assert result.reviews_completed == []  # nothing new

    def test_records_synthetic_review_when_no_matching_run(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)

        pr = _pr(number=300, labels=("review:approved",))
        with connect(db_path) as conn:
            result = complete_reviewer_runs(conn, [pr])

        assert result.reviews_completed == [(300, "approved")]
        assert any("synthetic" in reason for _, reason in result.reviews_skipped)
        with connect(db_path, read_only=True) as conn:
            review = conn.execute(
                "SELECT verdict, run_id FROM reviews WHERE pr_number = 300"
            ).fetchone()
            assert review["verdict"] == "approved"
            assert review["run_id"] is None  # no associated run

    def test_skips_prs_without_terminal_label(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)

        pr = _pr(number=400, labels=("review:pending",))  # only pending → skip
        with connect(db_path) as conn:
            result = complete_reviewer_runs(conn, [pr])

        assert result.reviews_completed == []
        with connect(db_path, read_only=True) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()
            assert row["n"] == 0


# --- mark_merged_batches --------------------------------------------------


class TestMarkMergedBatches:
    def test_transitions_in_progress_to_merged(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, BatchStatus.IN_PROGRESS.value),
            )

        pr = _pr(number=200, closing=(88,), merged=True, state="closed")
        with connect(db_path) as conn:
            result = mark_merged_batches(conn, [pr])

        assert result == [(88, 200)]
        with connect(db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT status, pr_number FROM batches WHERE issue_number = 88"
            ).fetchone()
            assert row["status"] == BatchStatus.MERGED.value
            assert row["pr_number"] == 200

    def test_ignores_closed_unmerged_prs(self, tmp_data_dir: Path) -> None:
        # Closed-but-not-merged (manual close, abandoned, etc.) → batch stays.
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, BatchStatus.IN_PROGRESS.value),
            )

        pr = _pr(number=200, closing=(88,), merged=False, state="closed")
        with connect(db_path) as conn:
            result = mark_merged_batches(conn, [pr])

        assert result == []
        with connect(db_path, read_only=True) as conn:
            row = conn.execute("SELECT status FROM batches WHERE issue_number = 88").fetchone()
            assert row["status"] == BatchStatus.IN_PROGRESS.value

    def test_handles_multi_closer_pr(self, tmp_data_dir: Path) -> None:
        # PR closes two issues. Both should transition.
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, BatchStatus.IN_PROGRESS.value),
            )
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 91, BatchStatus.IN_PROGRESS.value),
            )

        pr = _pr(number=200, closing=(88, 91), merged=True, state="closed")
        with connect(db_path) as conn:
            result = mark_merged_batches(conn, [pr])

        assert sorted(result) == [(88, 200), (91, 200)]

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
    complete_implementer_runs,
    complete_reviewer_runs,
    mark_merged_batches,
    reap_stale_queued_runs,
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

    def test_requires_human_review_recognised_as_terminal(self) -> None:
        # The reviewer agent sometimes only applies `requires-human-review`
        # without the matching `review:hard-block`. Pollback must still
        # treat that as a terminal verdict so the run doesn't sit queued
        # for 2h until the reaper kills it.
        assert _pick_verdict_label(frozenset({"requires-human-review"})) == "requires-human-review"

    def test_hard_block_wins_over_requires_human_review(self) -> None:
        # If both signals are present, the explicit review:* label wins
        # (it's the one the prompt is supposed to apply).
        labels = frozenset({"requires-human-review", "review:hard-block"})
        assert _pick_verdict_label(labels) == "review:hard-block"


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

    def test_marks_run_failed_on_requires_human_review(self, tmp_data_dir: Path) -> None:
        # The reviewer agent escalates without a `review:*` prefix sometimes
        # label. Pollback should still close the run as a hard-block — same effect as `review:hard-block` — so the cap
        # gets freed promptly.
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(
                conn,
                run_id="rev-rhr",
                agent="reviewer",
                model="sonnet",
                pr_number=210,
            )

        pr = _pr(number=210, labels=("requires-human-review",))
        with connect(db_path) as conn:
            result = complete_reviewer_runs(conn, [pr])

        assert result.reviews_completed == [(210, "hard_block")]
        with connect(db_path, read_only=True) as conn:
            run_row = conn.execute(
                "SELECT status FROM runs WHERE id = ?", ("rev-rhr",)
            ).fetchone()
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


# --- complete_implementer_runs --------------------------------------------


class TestCompleteImplementerRuns:
    def test_marks_implementer_success_when_pr_closes_issue(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(conn, run_id="impl-1", agent="implementer", model="sonnet", issue_number=42)

        pr = _pr(number=500, closing=(42,))
        with connect(db_path) as conn:
            completed = complete_implementer_runs(conn, [pr])

        assert completed == [("impl-1", 42, 500)]
        with connect(db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT status, finished_at, pr_number FROM runs WHERE id = 'impl-1'"
            ).fetchone()
            assert row["status"] == RunStatus.SUCCESS.value
            assert row["finished_at"] is not None
            assert row["pr_number"] == 500  # back-filled from PR

    def test_back_fills_batch_pr_number(self, tmp_data_dir: Path) -> None:
        # When the implementer finishes by opening a PR, pollback must
        # propagate the PR number to the batch row so the dashboard can
        # link batch → PR before the merge happens. Without this the
        # dashboard shows in_progress batches with no PR for hours.
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 77, BatchStatus.IN_PROGRESS.value),
            )
            create_run(conn, run_id="impl-77", agent="implementer", model="sonnet", issue_number=77)

        pr = _pr(number=420, closing=(77,))
        with connect(db_path) as conn:
            complete_implementer_runs(conn, [pr])

        with connect(db_path, read_only=True) as conn:
            batch = conn.execute(
                "SELECT pr_number, status FROM batches WHERE issue_number = 77"
            ).fetchone()
            assert batch["pr_number"] == 420
            # Status is still in_progress — only mark_merged_batches moves
            # batches to 'merged'. pr_number alone is a denormalised hint
            # for the dashboard, not a state transition.
            assert batch["status"] == BatchStatus.IN_PROGRESS.value

    def test_does_not_overwrite_existing_batch_pr_number(self, tmp_data_dir: Path) -> None:
        # If a batch already has a pr_number (e.g., from an earlier pollback
        # pass), a second implementer run for the same issue must NOT
        # clobber it. Conservative — we only back-fill when NULL.
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status, pr_number) "
                "VALUES (?, ?, ?, ?)",
                (0, 78, BatchStatus.IN_PROGRESS.value, 999),
            )
            create_run(conn, run_id="impl-78", agent="implementer", model="sonnet", issue_number=78)

        pr = _pr(number=421, closing=(78,))
        with connect(db_path) as conn:
            complete_implementer_runs(conn, [pr])

        with connect(db_path, read_only=True) as conn:
            batch = conn.execute(
                "SELECT pr_number FROM batches WHERE issue_number = 78"
            ).fetchone()
            assert batch["pr_number"] == 999  # untouched

    def test_ignores_pr_without_closing_issues(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(conn, run_id="impl-2", agent="implementer", model="sonnet", issue_number=99)

        pr = _pr(number=501, closing=())  # PR opens for the issue but doesn't close it
        with connect(db_path) as conn:
            completed = complete_implementer_runs(conn, [pr])

        assert completed == []
        with connect(db_path, read_only=True) as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = 'impl-2'").fetchone()
            assert row["status"] == RunStatus.QUEUED.value  # untouched

    def test_idempotent_when_run_already_terminal(self, tmp_data_dir: Path) -> None:
        # If an implementer run is already success/failed, leave it alone.
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(conn, run_id="impl-3", agent="implementer", model="sonnet", issue_number=7)
            conn.execute("UPDATE runs SET status = 'success' WHERE id = 'impl-3'")

        pr = _pr(number=502, closing=(7,))
        with connect(db_path) as conn:
            completed = complete_implementer_runs(conn, [pr])

        assert completed == []

    def test_multi_closer_pr_closes_multiple_implementers(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(conn, run_id="impl-a", agent="implementer", model="sonnet", issue_number=10)
            create_run(conn, run_id="impl-b", agent="implementer", model="sonnet", issue_number=11)

        pr = _pr(number=600, closing=(10, 11))
        with connect(db_path) as conn:
            completed = complete_implementer_runs(conn, [pr])

        assert sorted(completed) == [("impl-a", 10, 600), ("impl-b", 11, 600)]


# --- reap_stale_queued_runs ------------------------------------------------


class TestReapStaleQueuedRuns:
    def test_reaps_only_stale_queued_rows(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO runs (id, agent, model, status, started_at, issue_number) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    # 3h-old queued → reaped
                    ("stale-impl", "implementer", "sonnet", "queued", "3h", 110),
                    ("stale-rev", "reviewer", "sonnet", "queued", "3h", None),
                    # fresh queued → untouched
                    ("fresh", "implementer", "sonnet", "queued", "now", 200),
                    # already terminal → untouched
                    ("done", "implementer", "sonnet", "success", "3h", 50),
                    # running of any age → untouched
                    ("hot", "reviewer", "sonnet", "running", "3h", None),
                ],
            )
            conn.execute("UPDATE runs SET started_at = datetime('now') WHERE started_at = 'now'")
            conn.execute(
                "UPDATE runs SET started_at = datetime('now', '-3 hours') WHERE started_at = '3h'"
            )

            reaped = reap_stale_queued_runs(conn, max_age_hours=2)

        assert sorted(r[0] for r in reaped) == ["stale-impl", "stale-rev"]

        with connect(db_path, read_only=True) as conn:
            statuses = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM runs").fetchall()
            }
        assert statuses["stale-impl"] == RunStatus.TIMEOUT.value
        assert statuses["stale-rev"] == RunStatus.TIMEOUT.value
        assert statuses["fresh"] == RunStatus.QUEUED.value
        assert statuses["done"] == RunStatus.SUCCESS.value
        assert statuses["hot"] == RunStatus.RUNNING.value

    def test_noop_when_no_stale_rows(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            create_run(conn, run_id="fresh", agent="implementer", model="sonnet", issue_number=1)
            assert reap_stale_queued_runs(conn) == []

"""Tests for the orchestrator SQLite state accessors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orclaw.db import connect, init_db
from orclaw.orchestrator.state import (
    active_run_count,
    is_paused,
    issues_in_progress,
    load_batch_snapshot,
    load_state,
    mark_batch_failed,
    mark_batch_in_progress,
    mark_batch_merged,
    set_paused,
)

if TYPE_CHECKING:
    from pathlib import Path


def _seed(db_path: Path) -> None:
    init_db(db_path)


class TestPauseFlag:
    def test_default_not_paused(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path, read_only=True) as conn:
            assert is_paused(conn) is False

    def test_can_pause_and_resume(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            set_paused(conn, True)
            assert is_paused(conn) is True
            set_paused(conn, False)
            assert is_paused(conn) is False


class TestBatchSnapshot:
    def test_empty_snapshot(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path, read_only=True) as conn:
            snap = load_batch_snapshot(conn)
        assert snap.layers == []
        assert snap.total_issues == 0
        assert snap.issues_pending == frozenset()

    def test_groups_by_layer(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            for layer, num in [(0, 88), (0, 91), (1, 92), (2, 97)]:
                conn.execute(
                    "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                    (layer, num, "pending"),
                )
            snap = load_batch_snapshot(conn)
        assert snap.layers == [[88, 91], [92], [97]]
        assert snap.issues_pending == frozenset({88, 91, 92, 97})
        assert snap.total_issues == 4

    def test_status_indexing(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 1, "pending"),
            )
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 2, "in_progress"),
            )
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (1, 3, "merged"),
            )
            snap = load_batch_snapshot(conn)
        assert snap.issues_pending == frozenset({1})
        assert snap.issues_in_progress == frozenset({2})
        assert snap.issues_merged == frozenset({3})

    def test_layers_exclude_terminal_statuses(self, tmp_data_dir: Path) -> None:
        """Regression: a 'skipped' (or merged/failed) batch must NOT stay in
        ``layers`` — otherwise ``next_executable_batch`` will happily
        re-dispatch it. Caught in prod on the very first deploy when an
        operator marked an issue skipped via SQL and the next tick fired
        the implementer again.
        """
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                [
                    (0, 10, "pending"),
                    (0, 11, "in_progress"),
                    (0, 12, "skipped"),  # the dangerous one
                    (0, 13, "merged"),
                    (0, 14, "failed"),
                ],
            )
            snap = load_batch_snapshot(conn)

        # Only live (pending + in_progress) rows go into layers.
        assert snap.layers == [[10, 11]]
        # But the index sets still reflect every status — so the
        # orchestrator can still query "what's been merged?" etc.
        assert snap.issues_pending == frozenset({10})
        assert snap.issues_in_progress == frozenset({11})
        assert snap.issues_skipped == frozenset({12})
        assert snap.issues_merged == frozenset({13})
        assert snap.issues_failed == frozenset({14})


class TestBatchTransitions:
    def test_mark_in_progress_promotes_pending(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, "pending"),
            )
            mark_batch_in_progress(conn, issue_number=88, implementer_run_id="run_abc")
            row = conn.execute(
                "SELECT status, implementer_run_id FROM batches WHERE issue_number = 88"
            ).fetchone()
        assert row["status"] == "in_progress"
        assert row["implementer_run_id"] == "run_abc"

    def test_mark_merged(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, "in_progress"),
            )
            mark_batch_merged(conn, issue_number=88, pr_number=142)
            row = conn.execute(
                "SELECT status, pr_number FROM batches WHERE issue_number = 88"
            ).fetchone()
        assert row["status"] == "merged"
        assert row["pr_number"] == 142

    def test_mark_failed(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, "pending"),
            )
            mark_batch_failed(conn, issue_number=88)
            row = conn.execute("SELECT status FROM batches WHERE issue_number = 88").fetchone()
        assert row["status"] == "failed"

    def test_mark_failed_does_not_disturb_merged(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, "merged"),
            )
            mark_batch_failed(conn, issue_number=88)
            row = conn.execute("SELECT status FROM batches WHERE issue_number = 88").fetchone()
        assert row["status"] == "merged"  # unchanged


class TestActiveRunCount:
    def test_zero_when_no_runs(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path, read_only=True) as conn:
            assert active_run_count(conn) == 0

    def test_counts_queued_and_running(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO runs (id, agent, model, status) VALUES (?, ?, ?, ?)",
                [
                    ("r1", "implementer", "sonnet", "queued"),
                    ("r2", "implementer", "sonnet", "running"),
                    ("r3", "reviewer", "sonnet", "success"),
                    ("r4", "implementer", "sonnet", "failed"),
                ],
            )
            assert active_run_count(conn) == 2


class TestIssuesInProgress:
    def test_returns_in_progress_set(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                [(0, 1, "pending"), (0, 2, "in_progress"), (0, 3, "merged")],
            )
            assert issues_in_progress(conn) == frozenset({2})


class TestLoadState:
    def test_assembles_full_view(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        _seed(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, "pending"),
            )
            state = load_state(conn)
        assert state.paused is False
        assert state.batches.total_issues == 1
        assert state.batches.issues_pending == frozenset({88})
        assert state.active_run_count == 0
        assert state.last_planner_run is None  # seeded to epoch sentinel

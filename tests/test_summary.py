"""Tests for the daily/weekly summary builder."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from orclaw.db import connect, init_db
from orclaw.orchestrator.state import create_run
from orclaw.summary import build_summary, format_summary_telegram

if TYPE_CHECKING:
    from pathlib import Path


def _init_db(tmp_data_dir: Path) -> Path:
    db_path = tmp_data_dir / "engine.db"
    init_db(db_path)
    return db_path


class TestBuildSummary:
    def test_empty_db_yields_zero_counts(self, tmp_data_dir: Path) -> None:
        db_path = _init_db(tmp_data_dir)
        with connect(db_path, read_only=True) as conn:
            snap = build_summary(conn, window=timedelta(days=1))
        assert snap.runs_total == 0
        assert snap.runs_success == 0
        assert snap.runs_by_agent == {}
        assert snap.batches_merged == 0
        assert snap.reviews_approved == 0
        assert snap.runs_success_rate == 0.0

    def test_aggregates_runs_by_agent(self, tmp_data_dir: Path) -> None:
        db_path = _init_db(tmp_data_dir)
        with connect(db_path) as conn:
            create_run(conn, run_id="r1", agent="implementer", model="x", status="success")
            create_run(conn, run_id="r2", agent="implementer", model="x", status="failed")
            create_run(conn, run_id="r3", agent="reviewer", model="x", status="success")

        with connect(db_path, read_only=True) as conn:
            snap = build_summary(conn, window=timedelta(days=1))
        assert snap.runs_total == 3
        assert snap.runs_success == 2
        assert snap.runs_failed == 1
        assert snap.runs_by_agent == {"implementer": 2, "reviewer": 1}
        assert snap.runs_success_rate == 2 / 3

    def test_aggregates_batch_snapshot(self, tmp_data_dir: Path) -> None:
        db_path = _init_db(tmp_data_dir)
        with connect(db_path) as conn:
            for status, count in [("pending", 3), ("in_progress", 1), ("merged", 5)]:
                for i in range(count):
                    conn.execute(
                        "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                        (0, hash((status, i)) % 10000, status),
                    )

        with connect(db_path, read_only=True) as conn:
            snap = build_summary(conn, window=timedelta(days=1))
        assert snap.batches_pending == 3
        assert snap.batches_in_progress == 1
        assert snap.batches_merged == 5

    def test_aggregates_reviews_with_minor_fixes_in_approved_bucket(
        self, tmp_data_dir: Path
    ) -> None:
        db_path = _init_db(tmp_data_dir)
        with connect(db_path) as conn:
            for verdict, count in [
                ("approved", 4),
                ("minor_fixes_applied", 2),
                ("needs_changes", 1),
                ("hard_block", 1),
            ]:
                for i in range(count):
                    conn.execute(
                        "INSERT INTO reviews (pr_number, verdict) VALUES (?, ?)",
                        (1000 + i + hash(verdict) % 100, verdict),
                    )

        with connect(db_path, read_only=True) as conn:
            snap = build_summary(conn, window=timedelta(days=1))
        assert snap.reviews_approved == 6  # approved + minor_fixes_applied
        assert snap.reviews_needs_changes == 1
        assert snap.reviews_hard_block == 1


class TestFormatSummary:
    def test_includes_period_label_24h(self, tmp_data_dir: Path) -> None:
        db_path = _init_db(tmp_data_dir)
        with connect(db_path, read_only=True) as conn:
            snap = build_summary(conn, window=timedelta(days=1))
        msg = format_summary_telegram(snap)
        assert "last 24h" in msg
        assert "*Runs:*" in msg

    def test_handles_zero_runs_without_division_by_zero(self, tmp_data_dir: Path) -> None:
        db_path = _init_db(tmp_data_dir)
        with connect(db_path, read_only=True) as conn:
            snap = build_summary(conn, window=timedelta(days=1))
        msg = format_summary_telegram(snap)
        assert "0%" in msg  # success rate of 0 / 0

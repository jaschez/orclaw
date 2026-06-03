"""Tests for the SQLite layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from orclaw.db import apply_migrations, backfill_repo, connect, init_db
from orclaw.exceptions import DatabaseError

if TYPE_CHECKING:
    from pathlib import Path


class TestInitDb:
    def test_creates_db_file(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        assert not db_path.exists()
        init_db(db_path)
        assert db_path.is_file()

    def test_creates_expected_tables(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)

        with connect(db_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            names = {row["name"] for row in rows}

        # Tables defined in schema.sql
        expected = {
            "batches",
            "runs",
            "token_ledger",
            "reviews",
            "specialist_sessions",
            "engine_state",
            "monthly_summary",
        }
        assert expected.issubset(names), f"missing tables: {expected - names}"

    def test_is_idempotent(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        # Should not raise on second invocation.
        init_db(db_path)

    def test_seeds_engine_state_defaults(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)

        with connect(db_path, read_only=True) as conn:
            rows = conn.execute("SELECT key, value FROM engine_state ORDER BY key").fetchall()

        state = {row["key"]: row["value"] for row in rows}
        assert state["orchestrator_paused"] == "false"
        assert state["budget_paused"] == "false"

    def test_raises_when_schema_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "engine.db"
        with pytest.raises(DatabaseError, match="Schema file not found"):
            init_db(db_path, schema_path=tmp_path / "missing.sql")


def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


class TestRepoMigration:
    """Phase 1 multi-repo: the additive ``repo`` column + backfill."""

    def test_fresh_db_has_repo_columns(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path, read_only=True) as conn:
            for table in ("batches", "runs", "reviews"):
                assert "repo" in _columns(conn, table), f"{table} missing repo"

    def test_composite_unique_index_present(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path, read_only=True) as conn:
            idx = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        assert "idx_batches_repo_issue_active" in idx
        assert "idx_batches_issue_active" not in idx  # legacy index removed

    def test_two_repos_can_share_issue_number(self, tmp_data_dir: Path) -> None:
        # The composite unique index keys on (repo, issue_number), so the
        # same issue number in two repos must coexist.
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (repo, layer, issue_number, status) VALUES (?, ?, ?, ?)",
                ("owner/a", 0, 1, "pending"),
            )
            conn.execute(
                "INSERT INTO batches (repo, layer, issue_number, status) VALUES (?, ?, ?, ?)",
                ("owner/b", 0, 1, "pending"),
            )
            n = conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
        assert n == 2

    def test_migrates_legacy_db_without_repo(self, tmp_data_dir: Path) -> None:
        # Simulate a pre-Phase-1 database: tables without the repo column
        # and the old single-repo unique index.
        db_path = tmp_data_dir / "engine.db"
        with connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE batches (id INTEGER PRIMARY KEY, layer INTEGER, "
                "issue_number INTEGER, status TEXT NOT NULL DEFAULT 'pending')"
            )
            conn.execute(
                "CREATE UNIQUE INDEX idx_batches_issue_active "
                "ON batches(issue_number) WHERE status != 'failed'"
            )
            conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, agent TEXT, model TEXT)")
            conn.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, pr_number INTEGER, verdict TEXT)")
            conn.execute("INSERT INTO batches (layer, issue_number, status) VALUES (0, 5, 'pending')")

            apply_migrations(conn)

            for table in ("batches", "runs", "reviews"):
                assert "repo" in _columns(conn, table)
            # Legacy row now carries the default empty repo.
            row = conn.execute("SELECT repo FROM batches WHERE issue_number = 5").fetchone()
            assert row["repo"] == ""

    def test_apply_migrations_idempotent(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            apply_migrations(conn)
            apply_migrations(conn)  # second run must not raise

    def test_backfill_stamps_legacy_rows(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (0, 7, 'pending')"
            )
            conn.execute(
                "INSERT INTO runs (id, agent, model, status) VALUES ('r1', 'implementer', 'm', 'queued')"
            )
            updated = backfill_repo(conn, "owner/target")
            assert updated == 2
            # Already-stamped rows are left alone on a second backfill.
            assert backfill_repo(conn, "owner/target") == 0
            row = conn.execute("SELECT repo FROM batches WHERE issue_number = 7").fetchone()
            assert row["repo"] == "owner/target"

    def test_backfill_noop_for_empty_repo(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (0, 9, 'pending')"
            )
            assert backfill_repo(conn, "") == 0


class TestConnect:
    def test_connect_returns_dict_rows(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                (0, 88, "pending"),
            )
            row = conn.execute("SELECT * FROM batches").fetchone()

        assert isinstance(row, dict)
        assert row["layer"] == 0
        assert row["issue_number"] == 88
        assert row["status"] == "pending"

    def test_connect_raises_when_data_dir_missing(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "nope" / "engine.db"
        with pytest.raises(DatabaseError, match="does not exist"), connect(nonexistent):
            pass

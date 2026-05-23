"""Tests for the SQLite layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from orclaw.db import connect, init_db
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

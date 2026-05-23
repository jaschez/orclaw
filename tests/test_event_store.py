"""Tests for the SQLite-backed event log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from orclaw.db import init_db
from orclaw.event_store import (
    count_events,
    insert_event,
    purge_older_than,
    query_events,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def event_db(tmp_data_dir: Path) -> Path:
    db = tmp_data_dir / "engine.db"
    init_db(db)
    return db


class TestInsert:
    def test_basic_roundtrip(self, event_db: Path) -> None:
        insert_event(
            event_db,
            level="info",
            event="implementer_dispatch_started",
            module="orclaw.orchestrator.loop",
            attrs={"issue": 88, "run_id": "abc"},
        )
        rows = query_events(event_db, limit=1)
        assert len(rows) == 1
        row = rows[0]
        assert row["event"] == "implementer_dispatch_started"
        assert row["level"] == "info"
        assert row["module"] == "orclaw.orchestrator.loop"
        assert row["attrs"] == {"issue": 88, "run_id": "abc"}

    def test_invalid_level_is_coerced_to_info(self, event_db: Path) -> None:
        insert_event(event_db, level="banana", event="x")
        row = query_events(event_db, limit=1)[0]
        assert row["level"] == "info"

    def test_attrs_default_to_empty_dict(self, event_db: Path) -> None:
        insert_event(event_db, level="info", event="x")
        row = query_events(event_db, limit=1)[0]
        assert row["attrs"] == {}

    def test_silent_on_missing_db(self, tmp_data_dir: Path) -> None:
        """Inserting against a path that points to a missing DB file is
        silent — must never break the calling code."""
        missing = tmp_data_dir / "nope.db"
        # Should not raise.
        insert_event(missing, level="info", event="x")

    def test_non_dict_attrs_round_trip_via_value_key(self, event_db: Path) -> None:
        # If something exotic ends up in attrs (e.g. a JSON list), we
        # still want a queryable row, not an exception.
        import json
        import sqlite3

        # Bypass the helper to force a weird payload.
        conn = sqlite3.connect(str(event_db))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO events (level, event, attrs) VALUES (?, ?, ?)",
            ("info", "x", json.dumps(["a", "b"])),
        )
        conn.commit()
        conn.close()

        row = query_events(event_db, limit=1)[0]
        assert row["attrs"] == {"_value": ["a", "b"]}


class TestQuery:
    def test_filters_by_level(self, event_db: Path) -> None:
        insert_event(event_db, level="info", event="a")
        insert_event(event_db, level="warning", event="b")
        insert_event(event_db, level="error", event="c")

        warns = query_events(event_db, level="warning")
        assert [r["event"] for r in warns] == ["b"]

    def test_filters_by_event_pattern_with_like(self, event_db: Path) -> None:
        insert_event(event_db, level="info", event="implementer_dispatch_started")
        insert_event(event_db, level="info", event="implementer_dispatch_completed")
        insert_event(event_db, level="info", event="reviewer_dispatch_started")
        insert_event(event_db, level="info", event="pollback_started")

        impl = query_events(event_db, event_pattern="implementer%")
        assert {r["event"] for r in impl} == {
            "implementer_dispatch_started",
            "implementer_dispatch_completed",
        }
        dispatch = query_events(event_db, event_pattern="%dispatch%")
        assert {r["event"] for r in dispatch} == {
            "implementer_dispatch_started",
            "implementer_dispatch_completed",
            "reviewer_dispatch_started",
        }

    def test_orders_most_recent_first(self, event_db: Path) -> None:
        for i in range(5):
            insert_event(event_db, level="info", event=f"e{i}")
        rows = query_events(event_db, limit=10)
        assert [r["event"] for r in rows] == ["e4", "e3", "e2", "e1", "e0"]

    def test_clamps_limit_to_max_1000(self, event_db: Path) -> None:
        for i in range(20):
            insert_event(event_db, level="info", event=f"e{i}")
        rows = query_events(event_db, limit=99999)
        assert len(rows) <= 1000

    def test_returns_empty_on_missing_db(self, tmp_data_dir: Path) -> None:
        assert query_events(tmp_data_dir / "nope.db") == []


class TestPurge:
    def test_purge_removes_old_rows_keeps_new(self, event_db: Path) -> None:
        # Insert 3 events directly with explicit timestamps so we control them.
        import sqlite3

        conn = sqlite3.connect(str(event_db))
        old = (datetime.now(UTC) - timedelta(days=40)).isoformat().replace("+00:00", "Z")
        recent = (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        conn.execute("INSERT INTO events (ts, level, event) VALUES (?, ?, ?)", (old, "info", "old"))
        conn.execute(
            "INSERT INTO events (ts, level, event) VALUES (?, ?, ?)",
            (recent, "info", "recent"),
        )
        conn.commit()
        conn.close()

        n = purge_older_than(event_db, days=30)
        assert n == 1

        remaining = query_events(event_db, limit=100)
        assert [r["event"] for r in remaining] == ["recent"]

    def test_purge_rejects_zero_days(self, event_db: Path) -> None:
        with pytest.raises(ValueError):
            purge_older_than(event_db, days=0)

    def test_count_returns_zero_on_missing_db(self, tmp_data_dir: Path) -> None:
        assert count_events(tmp_data_dir / "nope.db") == 0

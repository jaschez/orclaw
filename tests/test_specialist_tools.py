"""Tests for the specialist tool catalogue.

We test the tools' Python implementations directly — no MCP transport
involved. The MCP layer is a thin decorator over these functions, so
exercising them is enough for the wiring.

Tools that hit GitHub are tested by monkey-patching ``GitHubClient`` so
no real API calls happen. Tools that touch the DB use the existing
``tmp_data_dir`` fixture from ``conftest.py`` and the temporary config
created by ``specialist_settings``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from orclaw.db import connect, init_db
from orclaw.specialist import tools as t

if TYPE_CHECKING:
    from pathlib import Path


# --- helpers --------------------------------------------------------------


@pytest.fixture()
def specialist_db(
    tmp_data_dir: Path,
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Init the schema and patch ``load_settings`` to return tmp-dir settings.

    The tools call ``load_settings()`` on each invocation (intentional —
    secrets may rotate). We patch the symbol where the tools imported it,
    so each call returns the fixture's Settings instance pointing at
    the tmp dir.
    """
    db_path = tmp_data_dir / "engine.db"
    init_db(db_path)

    def _fake_load(*args: object, **kwargs: object) -> object:
        return settings

    monkeypatch.setattr("orclaw.specialist.tools.load_settings", _fake_load)
    return db_path


# --- read-only tools ------------------------------------------------------


class TestGetStatus:
    def test_empty_db_yields_clean_snapshot(self, specialist_db: Path) -> None:
        out = t.get_status()
        assert "Paused" in out
        assert "no ▶" in out
        assert "In flight" in out

    def test_pause_state_reflected(self, specialist_db: Path) -> None:
        with connect(specialist_db) as conn:
            conn.execute(
                "INSERT INTO engine_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("orchestrator_paused", "true"),
            )
        out = t.get_status()
        assert "yes ⏸" in out

    def test_batch_counts_shown(self, specialist_db: Path) -> None:
        with connect(specialist_db) as conn:
            for n, status in enumerate(["pending", "pending", "merged"]):
                conn.execute(
                    "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                    (0, n + 1, status),
                )
        out = t.get_status()
        assert "pending: 2" in out
        assert "merged: 1" in out


class TestQueryRuns:
    def test_returns_no_runs_message_when_empty(self, specialist_db: Path) -> None:
        assert "no runs match" in t.query_runs()

    def test_rejects_invalid_limit(self, specialist_db: Path) -> None:
        assert "between 1 and 200" in t.query_runs(limit=0)
        assert "between 1 and 200" in t.query_runs(limit=999)

    def test_filters_by_agent(self, specialist_db: Path) -> None:
        with connect(specialist_db) as conn:
            from orclaw.orchestrator.state import create_run

            create_run(conn, run_id="r1", agent="implementer", model="x", issue_number=1)
            create_run(conn, run_id="r2", agent="reviewer", model="x", pr_number=200)

        out = t.query_runs(agent="implementer")
        assert "r1" in out
        assert "r2" not in out


class TestQueryBatches:
    def test_filters_by_status(self, specialist_db: Path) -> None:
        with connect(specialist_db) as conn:
            for layer, num, status in [(0, 1, "pending"), (0, 2, "merged")]:
                conn.execute(
                    "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                    (layer, num, status),
                )

        out = t.query_batches(status="pending")
        assert "#1" in out
        assert "#2" not in out

    def test_rejects_invalid_limit(self, specialist_db: Path) -> None:
        assert "between 1 and 500" in t.query_batches(limit=0)


class TestGetRecentLogs:
    def test_rejects_invalid_unit(self, specialist_db: Path) -> None:
        out = t.get_recent_logs(unit="something-else")
        assert "Invalid unit" in out

    def test_rejects_invalid_lines(self, specialist_db: Path) -> None:
        assert "between 1 and 1000" in t.get_recent_logs(lines=0)
        assert "between 1 and 1000" in t.get_recent_logs(lines=99999)


class TestGetSummary:
    def test_works_on_empty_db(self, specialist_db: Path) -> None:
        out = t.get_summary()
        assert "Runs" in out
        assert "0%" in out  # success rate

    def test_rejects_invalid_window(self, specialist_db: Path) -> None:
        assert "between 1 and 30" in t.get_summary(window_days=0)
        assert "between 1 and 30" in t.get_summary(window_days=999)


# --- action tools (DB only) -----------------------------------------------


class TestPauseResume:
    def test_pause_sets_flag(self, specialist_db: Path) -> None:
        out = t.pause_orchestrator()
        assert "paused" in out.lower()
        with connect(specialist_db, read_only=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'orchestrator_paused'"
            ).fetchone()
            assert row["value"] == "true"

    def test_resume_clears_flag(self, specialist_db: Path) -> None:
        t.pause_orchestrator()
        out = t.resume_orchestrator()
        assert "resumed" in out.lower()
        with connect(specialist_db, read_only=True) as conn:
            row = conn.execute(
                "SELECT value FROM engine_state WHERE key = 'orchestrator_paused'"
            ).fetchone()
            assert row["value"] == "false"


# --- send_daily_summary ---------------------------------------------------


class TestSendDailySummary:
    @pytest.mark.asyncio
    async def test_rejects_invalid_window(self, specialist_db: Path) -> None:
        assert "between 1 and 30" in await t.send_daily_summary(window_days=0)
        assert "between 1 and 30" in await t.send_daily_summary(window_days=999)

    @pytest.mark.asyncio
    async def test_does_not_post_when_telegram_disabled(
        self, specialist_db: Path
    ) -> None:
        # Default fixture settings have telegram empty → disabled.
        out = await t.send_daily_summary()
        assert "not configured" in out
        assert "*📊 orclaw" in out  # rendered message still returned

    @pytest.mark.asyncio
    async def test_reports_success_when_post_returns_true(
        self,
        tmp_data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Build a settings with telegram enabled, patch load_settings + the
        # outbound HTTP call so no real Telegram request is made.
        from orclaw.config import (
            BackoffSettings,
            ConcurrencySettings,
            GitHubSettings,
            NotificationsSettings,
            PathsSettings,
            Settings,
        )

        init_db(tmp_data_dir / "engine.db")
        cfg = Settings(
            github=GitHubSettings(token="ghp_test"),
            concurrency=ConcurrencySettings(),
            backoff=BackoffSettings(),
            notifications=NotificationsSettings(
                telegram_bot_token="bot_x", telegram_chat_id="chat_y"
            ),
            paths=PathsSettings(data_dir=tmp_data_dir),
        )
        monkeypatch.setattr(
            "orclaw.specialist.tools.load_settings", lambda *a, **kw: cfg
        )

        async def _fake_post(*_args: object, **_kwargs: object) -> bool:
            return True

        monkeypatch.setattr("orclaw.notifications.post_telegram", _fake_post)
        out = await t.send_daily_summary()
        assert "✓ Posted to Telegram" in out

    @pytest.mark.asyncio
    async def test_reports_failure_when_post_returns_false(
        self,
        tmp_data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from orclaw.config import (
            BackoffSettings,
            ConcurrencySettings,
            GitHubSettings,
            NotificationsSettings,
            PathsSettings,
            Settings,
        )

        init_db(tmp_data_dir / "engine.db")
        cfg = Settings(
            github=GitHubSettings(token="ghp_test"),
            concurrency=ConcurrencySettings(),
            backoff=BackoffSettings(),
            notifications=NotificationsSettings(
                telegram_bot_token="bot_x", telegram_chat_id="chat_y"
            ),
            paths=PathsSettings(data_dir=tmp_data_dir),
        )
        monkeypatch.setattr(
            "orclaw.specialist.tools.load_settings", lambda *a, **kw: cfg
        )

        async def _fake_post(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr("orclaw.notifications.post_telegram", _fake_post)
        out = await t.send_daily_summary()
        assert "❌ Telegram post FAILED" in out


# --- input validation -----------------------------------------------------


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_skip_issue_rejects_zero(self) -> None:
        out = await t.skip_issue(issue_number=0)
        assert "Invalid issue number" in out

    @pytest.mark.asyncio
    async def test_require_human_review_rejects_zero(self) -> None:
        out = await t.require_human_review(pr_number=0)
        assert "Invalid PR number" in out

    @pytest.mark.asyncio
    async def test_force_review_rejects_zero(self) -> None:
        out = await t.force_review(pr_number=0)
        assert "Invalid PR number" in out


# --- catalog --------------------------------------------------------------


class TestAllTools:
    def test_catalog_includes_all_expected_tools(self) -> None:
        names = set(t.all_tools().keys())
        expected = {
            "get_status",
            "get_decision_preview",
            "get_recent_logs",
            "query_runs",
            "query_batches",
            "get_summary",
            "doctor",
            "query_events",  # sprint 10
            "view_engine_pr",  # sprint 12
            "pause_orchestrator",
            "resume_orchestrator",
            "force_tick",
            "run_planner",
            "skip_issue",
            "require_human_review",
            "force_review",
            "propose_engine_change",  # sprint 12
            "merge_engine_pr",  # sprint 12
            "send_daily_summary",
        }
        assert expected == names

    def test_all_callables(self) -> None:
        for name, fn in t.all_tools().items():
            assert callable(fn), f"{name} not callable"


class TestServerBuild:
    def test_server_registers_all_tools(self) -> None:
        from orclaw.specialist.server import build_server

        srv = build_server()
        # FastMCP keeps tools in _tool_manager — peek at it.
        tools = srv._tool_manager.list_tools()
        names = {tool.name for tool in tools}
        assert names == set(t.all_tools().keys())

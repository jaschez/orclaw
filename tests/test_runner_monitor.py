"""Tests for the runner_monitor module — pure functions only.

The HTTP + systemd surface is tested by reading mocked outputs through
function-level seams (we don't drive a real httpx.AsyncClient here; the
small integration with GitHubClient is covered by the dispatcher tests).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from orclaw.db import connect, init_db
from orclaw.runner_monitor import (
    LONG_RUN_THRESHOLD,
    TRUSTED_ACTORS,
    _classify_run,
    _get_state,
    _set_state,
)

if TYPE_CHECKING:
    from pathlib import Path


def _run(
    *,
    actor: str = "github-actions[bot]",
    status: str = "completed",
    conclusion: str | None = "success",
    created_at: datetime | None = None,
) -> dict[str, object]:
    if created_at is None:
        created_at = datetime.now(UTC) - timedelta(minutes=1)
    return {
        "id": 1,
        "name": "ci",
        "head_branch": "main",
        "html_url": "https://github.com/${TARGET_REPO}/actions/runs/1",
        "actor": {"login": actor},
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": created_at.isoformat().replace("+00:00", "Z"),
    }


class TestClassifyRun:
    def test_completed_success_from_trusted_actor_is_fine(self) -> None:
        assert _classify_run(_run()) is None

    def test_failed_run_is_suspicious(self) -> None:
        reason = _classify_run(_run(conclusion="failure"))
        assert reason is not None
        assert "failed" in reason

    def test_actor_outside_trusted_set_is_suspicious(self) -> None:
        reason = _classify_run(_run(actor="someone-else"))
        assert reason is not None
        assert "someone-else" in reason

    def test_long_running_in_progress_is_suspicious(self) -> None:
        long_ago = datetime.now(UTC) - LONG_RUN_THRESHOLD - timedelta(minutes=1)
        run = _run(status="in_progress", conclusion=None, created_at=long_ago)
        reason = _classify_run(run)
        assert reason is not None
        assert ">" in reason

    def test_short_in_progress_is_fine(self) -> None:
        recent = datetime.now(UTC) - timedelta(seconds=30)
        run = _run(status="in_progress", conclusion=None, created_at=recent)
        assert _classify_run(run) is None

    def test_completed_failure_takes_priority_over_long_threshold(self) -> None:
        long_ago = datetime.now(UTC) - LONG_RUN_THRESHOLD - timedelta(minutes=1)
        # completed failures don't enter the long-running branch — should
        # still flag for the failure reason.
        run = _run(status="completed", conclusion="failure", created_at=long_ago)
        reason = _classify_run(run)
        assert reason is not None
        assert "failed" in reason

    def test_unknown_actor_field_doesnt_crash(self) -> None:
        run = {
            "id": 1,
            "name": "x",
            "head_branch": "main",
            "html_url": "x",
            "actor": None,  # GitHub returns None for some bot-triggered runs
            "status": "completed",
            "conclusion": "success",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        # None actor → falls through trusted check (since `actor and ...`
        # is False) → returns None.
        assert _classify_run(run) is None


class TestTrustedActors:
    def test_github_actions_bot_is_trusted(self) -> None:
        assert "github-actions[bot]" in TRUSTED_ACTORS

    def test_vercel_bot_is_trusted(self) -> None:
        # Vercel posts deploy-status checks that trigger workflows;
        # would be flagged as suspicious otherwise.
        assert "vercel[bot]" in TRUSTED_ACTORS

    def test_random_bot_is_not_trusted(self) -> None:
        # Negative: an unknown bot does flag.
        assert "evil-actor[bot]" not in TRUSTED_ACTORS


class TestEngineStateHelpers:
    """Make sure the de-dup cursor round-trips correctly."""

    def test_set_then_get(self, tmp_data_dir: Path) -> None:
        db_path = tmp_data_dir / "engine.db"
        init_db(db_path)
        with connect(db_path) as conn:
            assert _get_state(conn, "k1") is None
            _set_state(conn, "k1", "v1")
            assert _get_state(conn, "k1") == "v1"
            _set_state(conn, "k1", "v2")
            assert _get_state(conn, "k1") == "v2"


@pytest.mark.asyncio
async def test_check_runner_health_flips_var_on_transition(
    tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transition recorded in DB; repo var update attempted.

    We monkey-patch the three I/O seams (systemd probe, repo-var
    read/write) so the test is hermetic.
    """
    from orclaw import runner_monitor as rm
    from orclaw.config import Settings

    db_path = tmp_data_dir / "engine.db"
    init_db(db_path)

    monkeypatch.setattr(rm, "_is_runner_active", lambda unit=...: True)
    monkeypatch.setattr(rm, "_get_repo_variable", _async_returning(None))
    set_calls: list[tuple[str, str]] = []

    async def fake_set(settings, name, value):
        set_calls.append((name, value))
        return True

    monkeypatch.setattr(rm, "_set_repo_variable", fake_set)
    monkeypatch.setattr(rm, "post_telegram", _async_noop)

    settings = Settings(  # minimal
        github=__import__("orclaw.config", fromlist=["GitHubSettings"]).GitHubSettings(
            token="x"
        ),
        concurrency=__import__(
            "orclaw.config", fromlist=["ConcurrencySettings"]
        ).ConcurrencySettings(),
        backoff=__import__("orclaw.config", fromlist=["BackoffSettings"]).BackoffSettings(),
        notifications=__import__(
            "orclaw.config", fromlist=["NotificationsSettings"]
        ).NotificationsSettings(),
        paths=__import__("orclaw.config", fromlist=["PathsSettings"]).PathsSettings(
            data_dir=tmp_data_dir
        ),
    )

    with connect(db_path) as conn:
        result = await rm.check_runner_health(settings, conn)

    assert result.systemd_active is True
    assert result.transitioned is True  # nothing recorded before
    assert set_calls == [("ORCLAW_RUNNER", "self-hosted")]


# --- async test helpers ----------------------------------------------------


def _async_returning(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn


async def _async_noop(*args, **kwargs) -> None:
    return None

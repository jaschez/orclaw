"""Tests for the dashboard FastAPI app.

We use FastAPI's TestClient (sync wrapper over ASGI) so we can hit
endpoints without an actual HTTP server. Only the read endpoints are
exercised end-to-end here — write endpoints touch GitHub and are
already covered by the github_repo_ops + specialist.tools tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from orclaw.db import connect, init_db

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def dashboard_client(tmp_data_dir: Path, settings, monkeypatch: pytest.MonkeyPatch):
    """A TestClient backed by an isolated SQLite + patched settings.

    Also points the overlay layer at a tmp dir so write tests for
    prompts and timers don't bleed into the real
    ``/var/lib/orclaw/overrides`` (which doesn't exist on dev
    machines anyway). The overlay's ``apply_to_systemd`` flag is False
    so writes don't shell out to ``sudo``.
    """
    db_path = tmp_data_dir / "engine.db"
    init_db(db_path)

    # The app uses module-level load_settings. Patch both the FastAPI
    # helper AND the specialist.tools helper (called by write endpoints).
    def _fake_load(*args, **kwargs):
        return settings

    monkeypatch.setattr("orclaw.dashboard.app.load_settings", _fake_load)
    monkeypatch.setattr("orclaw.specialist.tools.load_settings", _fake_load)

    # Sandbox the overlay under tmp_data_dir.
    from orclaw import overlay as overlay_mod

    overlay_root = tmp_data_dir / "overrides"
    overlay_root.mkdir()
    test_paths = overlay_mod.OverlayPaths(
        root=overlay_root,
        # Defaults still come from the real shipped tree so tests can
        # assert on real prompt/timer names (implementer-comment.md,
        # orclaw-orchestrator.timer, ...). That mirrors prod and avoids
        # having to invent a parallel fixture tree.
        apply_to_systemd=False,
    )
    monkeypatch.setattr("orclaw.dashboard.app._overlay_paths", lambda: test_paths)

    from orclaw.dashboard.app import create_app

    return TestClient(create_app())


class TestIndex:
    def test_root_serves_html(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/")
        assert r.status_code == 200
        assert "orclaw" in r.text.lower()
        assert "<html" in r.text.lower()


class TestStatusEndpoint:
    def test_returns_basic_fields(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is False  # default in fresh DB
        assert "max_in_flight" in body
        assert "batches" in body
        assert "runs" in body


class TestBatchesEndpoint:
    def test_empty_db(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/batches")
        assert r.status_code == 200
        assert r.json() == []

    def test_filter_by_status(self, dashboard_client: TestClient, tmp_data_dir: Path) -> None:
        db = tmp_data_dir / "engine.db"
        with connect(db) as conn:
            conn.executemany(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                [
                    (0, 88, "pending"),
                    (0, 91, "in_progress"),
                    (1, 99, "merged"),
                ],
            )
        r = dashboard_client.get("/api/batches?status=pending")
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["issue_number"] == 88
        assert rows[0]["status"] == "pending"


class TestRunsEndpoint:
    def test_filters_compose(self, dashboard_client: TestClient, tmp_data_dir: Path) -> None:
        db = tmp_data_dir / "engine.db"
        from orclaw.orchestrator.state import create_run

        with connect(db) as conn:
            create_run(conn, run_id="a", agent="implementer", model="x", status="success")
            create_run(conn, run_id="b", agent="implementer", model="x", status="failed")
            create_run(conn, run_id="c", agent="reviewer", model="x", status="success")

        # agent filter alone
        r = dashboard_client.get("/api/runs?agent=reviewer")
        assert [row["id"] for row in r.json()] == ["c"]

        # composed
        r = dashboard_client.get("/api/runs?agent=implementer&status=failed")
        assert [row["id"] for row in r.json()] == ["b"]


class TestEventsEndpoint:
    def test_passes_filters_through(self, dashboard_client: TestClient, tmp_data_dir: Path) -> None:
        from orclaw.event_store import insert_event

        db = tmp_data_dir / "engine.db"
        insert_event(db, level="info", event="pollback_started")
        insert_event(db, level="info", event="implementer_dispatch_started")
        insert_event(db, level="warning", event="dispatch_skipped")

        r = dashboard_client.get("/api/events?level=warning")
        rows = r.json()
        assert all(row["level"] == "warning" for row in rows)
        assert any("dispatch_skipped" in row["event"] for row in rows)


class TestSummaryEndpoint:
    def test_returns_zero_counts_on_empty(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/summary?days=7")
        body = r.json()
        assert body["runs_total"] == 0
        assert body["runs_success_rate"] == 0.0
        assert "since" in body
        assert "until" in body


class TestConfigEndpoint:
    def test_no_secrets_in_response(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/config")
        body = r.json()
        assert "github" in body
        # Token presence is reported as a boolean, never the value.
        assert body["github"]["token_set"] in (True, False)
        # Sanity check — body shouldn't contain raw token text.
        assert "ghp_" not in r.text


class TestPromptsEndpoint:
    def test_list_returns_defaults_not_overridden(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/prompts")
        assert r.status_code == 200
        rows = r.json()
        names = {row["name"] for row in rows}
        assert "implementer-comment.md" in names
        assert "reviewer-comment.md" in names
        # Fresh overlay → nothing overridden yet.
        assert all(row["is_overridden"] is False for row in rows)

    def test_get_individual_prompt(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/prompts/implementer-comment.md")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "implementer-comment.md"
        assert "@claude implement" in body["content"]
        assert body["is_overridden"] is False

    def test_put_creates_override(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.put(
            "/api/prompts/implementer-comment.md",
            json={"content": "@claude implement issue #{{ISSUE_NUMBER}} please"},
        )
        assert r.status_code == 200
        assert r.json()["is_overridden"] is True
        # GET now returns the override.
        body = dashboard_client.get("/api/prompts/implementer-comment.md").json()
        assert body["content"].endswith("please")
        assert body["is_overridden"] is True
        # List flags it.
        rows = dashboard_client.get("/api/prompts").json()
        by_name = {row["name"]: row for row in rows}
        assert by_name["implementer-comment.md"]["is_overridden"] is True
        assert by_name["reviewer-comment.md"]["is_overridden"] is False

    def test_delete_reverts_to_default(self, dashboard_client: TestClient) -> None:
        dashboard_client.put(
            "/api/prompts/implementer-comment.md",
            json={"content": "OVERRIDDEN"},
        )
        r = dashboard_client.delete("/api/prompts/implementer-comment.md")
        assert r.status_code == 200
        assert r.json()["action"] == "reverted"
        # GET returns the shipped default again.
        body = dashboard_client.get("/api/prompts/implementer-comment.md").json()
        assert body["is_overridden"] is False
        assert "@claude implement" in body["content"]

    def test_delete_404_when_no_override(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.delete("/api/prompts/implementer-comment.md")
        assert r.status_code == 404

    def test_put_rejects_empty_content(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.put("/api/prompts/implementer-comment.md", json={"content": "   "})
        assert r.status_code == 400

    def test_rejects_path_traversal(self, dashboard_client: TestClient) -> None:
        for evil in (
            "../../etc/passwd",
            "..%2Fpasswd",
            "evil/path.md",
            "no-extension",
            "not_md.txt",
        ):
            r = dashboard_client.get(f"/api/prompts/{evil}")
            assert r.status_code in (400, 404), f"{evil} returned {r.status_code}"

    def test_404_on_unknown(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/prompts/does-not-exist.md")
        assert r.status_code == 404


class TestPlanEndpoint:
    def test_empty_plan_when_no_batches(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/plan")
        assert r.status_code == 200
        body = r.json()
        assert body["layers"] == []
        assert body["active_layer_index"] == -1
        assert body["next_dispatchable_issues"] == []
        assert "concurrency_cap" in body
        assert "remaining_slots" in body

    def test_layers_grouped_correctly_with_stats(
        self, dashboard_client: TestClient, tmp_data_dir: Path
    ) -> None:
        db = tmp_data_dir / "engine.db"
        with connect(db) as conn:
            # Layer 0: 1 in_progress + 1 pending (live)
            # Layer 1: 1 merged (terminal) — should be marked complete
            # Layer 2: 2 pending (live)
            conn.executemany(
                "INSERT INTO batches (layer, issue_number, status) VALUES (?, ?, ?)",
                [
                    (0, 88, "in_progress"),
                    (0, 91, "pending"),
                    (1, 100, "merged"),
                    (2, 110, "pending"),
                    (2, 111, "pending"),
                ],
            )

        body = dashboard_client.get("/api/plan").json()
        assert len(body["layers"]) == 3

        # Layer 0 is active (first non-complete with live issues)
        l0 = body["layers"][0]
        assert l0["index"] == 0
        assert l0["is_active_layer"] is True
        assert l0["complete"] is False
        assert l0["live_issues"] == 2
        assert l0["counts"]["pending"] == 1
        assert l0["counts"]["in_progress"] == 1

        # Layer 1 is complete (only merged)
        l1 = body["layers"][1]
        assert l1["complete"] is True
        assert l1["is_active_layer"] is False

        # Layer 2 is not the active layer (layer 0 has live work first)
        l2 = body["layers"][2]
        assert l2["complete"] is False
        assert l2["is_active_layer"] is False

        # Next-dispatchable: only pending issues IN the active layer
        assert body["next_dispatchable_issues"] == [91]
        assert body["active_layer_index"] == 0

    def test_issue_chip_includes_pr_and_run_info(
        self, dashboard_client: TestClient, tmp_data_dir: Path
    ) -> None:
        db = tmp_data_dir / "engine.db"
        with connect(db) as conn:
            conn.execute(
                "INSERT INTO batches (layer, issue_number, status, "
                "implementer_run_id, pr_number) VALUES (?, ?, ?, ?, ?)",
                (0, 88, "in_progress", "impl-abc12345", 142),
            )
        body = dashboard_client.get("/api/plan").json()
        issue = body["layers"][0]["issues"][0]
        assert issue["number"] == 88
        assert issue["status"] == "in_progress"
        assert issue["implementer_run_id"] == "impl-abc12345"
        assert issue["pr_number"] == 142
        assert issue["is_live"] is True
        assert issue["is_terminal"] is False


class TestConcurrencyCapEndpoint:
    def test_status_exposes_default_and_override(self, dashboard_client: TestClient) -> None:
        body = dashboard_client.get("/api/status").json()
        # Fresh DB → no override.
        assert body["max_in_flight_override"] is None
        assert body["max_in_flight_default"] == body["max_in_flight"]

    def test_set_override_then_status_reflects_it(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post("/api/concurrency_cap", json={"value": 7})
        assert r.status_code == 200
        body = r.json()
        assert body["override"] == 7
        assert body["effective"] == 7

        st = dashboard_client.get("/api/status").json()
        assert st["max_in_flight_override"] == 7
        assert st["max_in_flight"] == 7  # effective wins

    def test_clear_override_via_null(self, dashboard_client: TestClient) -> None:
        dashboard_client.post("/api/concurrency_cap", json={"value": 3})
        r = dashboard_client.post("/api/concurrency_cap", json={"value": None})
        assert r.status_code == 200
        body = r.json()
        assert body["override"] is None
        # Effective falls back to the config default.
        st = dashboard_client.get("/api/status").json()
        assert st["max_in_flight_override"] is None
        assert st["max_in_flight"] == st["max_in_flight_default"]

    def test_rejects_negative(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post("/api/concurrency_cap", json={"value": -1})
        assert r.status_code == 400

    def test_rejects_huge_value(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post("/api/concurrency_cap", json={"value": 9999})
        assert r.status_code == 400

    def test_rejects_missing_value_key(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post("/api/concurrency_cap", json={})
        assert r.status_code == 400

    def test_rejects_non_int_value(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post("/api/concurrency_cap", json={"value": "five"})
        assert r.status_code == 400


class TestTimersEndpoints:
    def test_list_returns_timer_and_service_files(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/timers")
        assert r.status_code == 200
        rows = r.json()
        names = {row["name"] for row in rows}
        # We expect at least the orchestrator + batch-planner timers to be there.
        assert "orclaw-orchestrator.timer" in names
        assert "orclaw-batch-planner.timer" in names
        # Services exposed too — same dir.
        assert any(row["kind"] == "service" for row in rows)
        # Fresh overlay → nothing overridden + nothing user-created.
        assert all(row["is_overridden"] is False for row in rows)
        assert all(row["is_user_created"] is False for row in rows)

    def test_get_individual_timer(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/timers/orclaw-orchestrator.timer")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "orclaw-orchestrator.timer"
        assert body["kind"] == "timer"
        assert "[Timer]" in body["content"]
        assert body["is_overridden"] is False

    def test_put_creates_override_for_default(self, dashboard_client: TestClient) -> None:
        new_body = "[Timer]\nOnCalendar=daily\nUnit=orclaw-orchestrator.service\n"
        r = dashboard_client.put("/api/timers/orclaw-orchestrator.timer", json={"content": new_body})
        assert r.status_code == 200
        assert r.json()["is_overridden"] is True
        assert r.json()["is_user_created"] is False
        # GET returns the override.
        body = dashboard_client.get("/api/timers/orclaw-orchestrator.timer").json()
        assert "OnCalendar=daily" in body["content"]
        assert body["is_overridden"] is True

    def test_post_creates_new_unit(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post(
            "/api/timers",
            json={
                "name": "my-custom-thing.timer",
                "content": "[Timer]\nOnCalendar=hourly\nUnit=fake.service\n",
            },
        )
        assert r.status_code == 200
        assert r.json()["is_user_created"] is True
        rows = dashboard_client.get("/api/timers").json()
        custom = next(row for row in rows if row["name"] == "my-custom-thing.timer")
        assert custom["is_user_created"] is True

    def test_post_rejects_collision(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post(
            "/api/timers",
            json={
                "name": "orclaw-orchestrator.timer",
                "content": "[Timer]\nOnCalendar=hourly\n",
            },
        )
        assert r.status_code == 409

    def test_delete_overridden_default_reverts(self, dashboard_client: TestClient) -> None:
        dashboard_client.put(
            "/api/timers/orclaw-orchestrator.timer",
            json={"content": "[Timer]\nOnCalendar=daily\n"},
        )
        r = dashboard_client.delete("/api/timers/orclaw-orchestrator.timer")
        assert r.status_code == 200
        assert r.json()["action"] == "reverted"
        body = dashboard_client.get("/api/timers/orclaw-orchestrator.timer").json()
        assert body["is_overridden"] is False
        assert "OnCalendar=daily" not in body["content"]

    def test_delete_user_created_removes(self, dashboard_client: TestClient) -> None:
        dashboard_client.post(
            "/api/timers",
            json={
                "name": "ephemeral.timer",
                "content": "[Timer]\nOnCalendar=hourly\n",
            },
        )
        r = dashboard_client.delete("/api/timers/ephemeral.timer")
        assert r.status_code == 200
        assert r.json()["action"] == "removed"
        assert dashboard_client.get("/api/timers/ephemeral.timer").status_code == 404

    def test_delete_unknown_returns_404(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.delete("/api/timers/does-not-exist.timer")
        assert r.status_code == 404

    def test_put_rejects_empty_content(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.put("/api/timers/orclaw-orchestrator.timer", json={"content": ""})
        assert r.status_code == 400

    def test_rejects_path_traversal(self, dashboard_client: TestClient) -> None:
        for evil in (
            "../../etc/passwd",
            "..%2Fpasswd",
            "evil/path.timer",
            "no-extension",
            "not_a_unit.txt",
        ):
            r = dashboard_client.get(f"/api/timers/{evil}")
            assert r.status_code in (400, 404), f"{evil} returned {r.status_code}"

    def test_404_on_unknown(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/timers/does-not-exist.timer")
        assert r.status_code == 404


class TestQuotaEstimateEndpoint:
    def test_empty_db_returns_zero(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.get("/api/quota_estimate")
        assert r.status_code == 200
        body = r.json()
        assert body["dispatches_counted"] == 0
        assert body["pct_used"] == 0.0
        assert body["budget_per_window"] > 0
        assert body["window_hours"] == 5
        assert body["is_lower_bound"] is True

    def test_counts_recent_implementer_and_reviewer_runs(
        self, dashboard_client: TestClient, tmp_data_dir: Path
    ) -> None:
        from orclaw.orchestrator.state import create_run

        db = tmp_data_dir / "engine.db"
        with connect(db) as conn:
            create_run(conn, run_id="r1", agent="implementer", model="x", status="running")
            create_run(conn, run_id="r2", agent="reviewer", model="x", status="success")
            # batch_planner runs DO NOT count toward dispatch budget.
            create_run(conn, run_id="r3", agent="batch_planner", model="x", status="success")
            # Pretend r1 + r2 + r3 just started — default is datetime('now').
        body = dashboard_client.get("/api/quota_estimate").json()
        assert body["dispatches_counted"] == 2  # implementer + reviewer only


class TestPauseResumeEndpoints:
    def test_pause_then_resume_round_trip(self, dashboard_client: TestClient) -> None:
        r = dashboard_client.post("/api/pause")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"

        st = dashboard_client.get("/api/status").json()
        assert st["paused"] is True

        r = dashboard_client.post("/api/resume")
        assert r.status_code == 200
        assert r.json()["status"] == "resumed"

        st = dashboard_client.get("/api/status").json()
        assert st["paused"] is False

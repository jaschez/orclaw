"""FastAPI dashboard.

Endpoints
---------
Read (no side effects):
  GET  /                      → HTML SPA
  GET  /api/status            → paused, in-flight, cap, doctor summary
  GET  /api/batches           → query params: status, limit
  GET  /api/runs              → query params: agent, status, limit
  GET  /api/events            → query params: minutes, level, pattern, limit
  GET  /api/summary           → query params: days
  GET  /api/decision_preview  → dry-run next tick
  GET  /api/prompts           → list prompt files in prompts/
  GET  /api/prompts/{name}    → full content of a prompt file
  GET  /api/config            → read-only config view (no secrets)

Webhook (HMAC-authenticated, exclude from Cloudflare Access):
  POST /webhook/github        → verify signature → coalesced apply tick

Write (each fires Telegram + logs to events):
  POST /api/pause
  POST /api/resume
  POST /api/force_tick
  POST /api/run_planner
  POST /api/skip_issue/{number}
  POST /api/require_human_review/{pr_number}
  POST /api/force_review/{pr_number}
  POST /api/concurrency_cap          → set runtime override (body: {value: int|null})
  PUT  /api/prompts/{name}           → propose PR (uses propose_engine_change)
  PUT  /api/timers/{name}            → propose PR for a systemd timer file

Tuning (read-only):
  GET  /api/timers                   → list .timer + .service files
  GET  /api/timers/{name}            → full content of a timer/service file
  GET  /api/quota_estimate           → Pro-plan 5h budget estimate

Auth model: Cloudflare Access cookie auth in front. The app trusts every
request that reaches it (Access already validated the user identity at
the edge). No user-facing auth code here.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from orclaw import webhooks
from orclaw.config import load_settings
from orclaw.db import connect
from orclaw.event_store import query_events as _query_events
from orclaw.exceptions import OrclawError
from orclaw.logging import get_logger
from orclaw.notifications import post_telegram
from orclaw.orchestrator import (
    active_run_count,
    describe_next_action,
    effective_max_in_flight,
    get_concurrency_override,
    is_paused,
    orchestrator_tick,
    set_concurrency_override,
    set_paused,
)
from orclaw.summary import build_summary

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
# Prompts + systemd unit paths are owned by the overlay layer now
# (orclaw.overlay). The dashboard delegates all read/write through
# there so dashboard edits land in /var/lib/orclaw/overrides/.

# Pro-plan ballpark: ~45 outbound Claude messages per 5h rolling window.
# This is a *lower-bound estimate* for the dashboard quota gauge — we
# count dispatches from the runs table, which doesn't see e.g. specialist
# chat from claude.ai. Treat the gauge as "minimum used".
_QUOTA_BUDGET_PER_5H = 45
_QUOTA_WINDOW_HOURS = 5


def _asset_version() -> str:
    """Cache-buster derived from the static files' mtimes.

    Recomputed on every request — cheap (two stat calls) and guarantees
    the browser refetches CSS+JS whenever they change on disk (every
    auto-deploy bumps mtimes via ``git checkout``). No build step needed.
    """
    try:
        css_mt = (_STATIC_DIR / "dashboard.css").stat().st_mtime
        js_mt = (_STATIC_DIR / "dashboard.js").stat().st_mtime
        return str(int(max(css_mt, js_mt)))
    except OSError:
        # Fallback if files are missing — better than crashing index.
        return "dev"


def create_app() -> FastAPI:
    """Build the FastAPI app. Factory pattern so tests can instantiate it
    in isolation without side effects at import time.
    """
    app = FastAPI(title="orclaw dashboard", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    # =====================================================================
    # SPA shell
    # =====================================================================

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        # The template renders the static layout; data comes from /api/*
        # via fetch in JS so each section can refresh on its own cadence.
        # Newer Starlette signature (positional Request first) sidesteps a
        # weakref hashing issue in jinja2's LRU cache.
        return templates.TemplateResponse(
            request, "index.html", {"asset_version": _asset_version()}
        )

    # =====================================================================
    # READ endpoints
    # =====================================================================

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        settings = _settings_or_500()
        with connect(settings.paths.db_path, read_only=True) as conn:
            paused = is_paused(conn)
            override = get_concurrency_override(conn)
            effective_cap = effective_max_in_flight(
                conn, settings_default=settings.concurrency.max_in_flight
            )
            in_flight = active_run_count(conn)
            batch_counts_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM batches GROUP BY status"
            ).fetchall()
            run_counts_rows = conn.execute(
                "SELECT agent, status, COUNT(*) AS n FROM runs GROUP BY agent, status"
            ).fetchall()

        batch_counts = {row["status"]: int(row["n"]) for row in batch_counts_rows}
        run_counts: dict[str, dict[str, int]] = {}
        for row in run_counts_rows:
            run_counts.setdefault(row["agent"], {})[row["status"]] = int(row["n"])

        return {
            "paused": paused,
            "in_flight": in_flight,
            # ``max_in_flight`` is the **effective** cap (override or default).
            # Kept under this name for backwards-compat with existing widgets.
            "max_in_flight": effective_cap,
            "max_in_flight_default": settings.concurrency.max_in_flight,
            "max_in_flight_override": override,
            "batches": batch_counts,
            "runs": run_counts,
            "repo": settings.github.repo,
            "ts": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/batches")
    async def api_batches(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        settings = _settings_or_500()
        limit = max(1, min(limit, 500))
        query = (
            "SELECT id, layer, issue_number, status, implementer_run_id, "
            "pr_number, created_at, updated_at FROM batches"
        )
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY layer, issue_number LIMIT ?"
        params.append(limit)

        with connect(settings.paths.db_path, read_only=True) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    @app.get("/api/runs")
    async def api_runs(
        agent: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        settings = _settings_or_500()
        limit = max(1, min(limit, 500))
        query = (
            "SELECT id, agent, status, issue_number, pr_number, branch, model, "
            "started_at, finished_at, duration_seconds, notes FROM runs"
        )
        where: list[str] = []
        params: list[Any] = []
        if agent:
            where.append("agent = ?")
            params.append(agent)
        if status:
            where.append("status = ?")
            params.append(status)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        with connect(settings.paths.db_path, read_only=True) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    @app.get("/api/events")
    async def api_events(
        minutes: int = 60,
        level: str | None = None,
        pattern: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        settings = _settings_or_500()
        minutes = max(1, min(minutes, 10080))
        limit = max(1, min(limit, 500))
        rows = _query_events(
            settings.paths.db_path,
            since=datetime.now(UTC) - timedelta(minutes=minutes),
            level=level,
            event_pattern=pattern,
            limit=limit,
        )
        return rows

    @app.get("/api/summary")
    async def api_summary(days: int = 1) -> dict[str, Any]:
        settings = _settings_or_500()
        days = max(1, min(days, 30))
        with connect(settings.paths.db_path, read_only=True) as conn:
            snap = build_summary(conn, window=timedelta(days=days))
        return {
            "since": snap.since.isoformat(),
            "until": snap.until.isoformat(),
            "runs_total": snap.runs_total,
            "runs_success": snap.runs_success,
            "runs_failed": snap.runs_failed,
            "runs_by_agent": snap.runs_by_agent,
            "runs_success_rate": snap.runs_success_rate,
            "batches_merged": snap.batches_merged,
            "batches_failed": snap.batches_failed,
            "batches_in_progress": snap.batches_in_progress,
            "batches_pending": snap.batches_pending,
            "reviews_approved": snap.reviews_approved,
            "reviews_needs_changes": snap.reviews_needs_changes,
            "reviews_hard_block": snap.reviews_hard_block,
        }

    @app.get("/api/plan")
    async def api_plan() -> dict[str, Any]:
        """Full orchestration plan: layered batches + concurrency state.

        Returns the same layered structure the orchestrator uses to decide
        what to dispatch. Each layer is a list of issues; issues within a
        layer are independent (no blocker between them, can run in parallel
        if concurrency allows). Lower-index layers must finish before
        higher-index layers can start.

        Within a layer, order matches what was persisted by the planner
        (priority then issue number — P0 before P1 before un-prioritised,
        ascending issue number to break ties).
        """
        settings = _settings_or_500()
        with connect(settings.paths.db_path, read_only=True) as conn:
            rows = conn.execute(
                "SELECT layer, issue_number, status, implementer_run_id, "
                "pr_number, created_at, updated_at "
                "FROM batches "
                "ORDER BY layer, id"  # id reflects insertion order = priority order
            ).fetchall()
            in_flight = active_run_count(conn)
            paused = is_paused(conn)
            cap = effective_max_in_flight(conn, settings_default=settings.concurrency.max_in_flight)

        # Build layer structure. Skipped + merged + failed stay in their
        # original layer so the UI can show full history, but flag them.
        layers_dict: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            layer = int(row["layer"])
            layers_dict.setdefault(layer, []).append(
                {
                    "number": int(row["issue_number"]),
                    "status": str(row["status"]),
                    "pr_number": int(row["pr_number"]) if row["pr_number"] else None,
                    "implementer_run_id": row["implementer_run_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "is_terminal": str(row["status"]) in {"merged", "failed", "skipped"},
                    "is_live": str(row["status"]) in {"pending", "in_progress"},
                }
            )

        # Layer stats — useful for visualising bottlenecks.
        layers_list = []
        for idx in sorted(layers_dict.keys()):
            issues = layers_dict[idx]
            counts = {"pending": 0, "in_progress": 0, "merged": 0, "failed": 0, "skipped": 0}
            for iss in issues:
                counts[iss["status"]] = counts.get(iss["status"], 0) + 1
            live = counts["pending"] + counts["in_progress"]
            total = len(issues)
            layers_list.append(
                {
                    "index": idx,
                    "issues": issues,
                    "total_issues": total,
                    "live_issues": live,
                    "counts": counts,
                    "complete": live == 0,
                    # Layer is "active" if it has any live issues AND all prior
                    # layers are complete (no live issues). The orchestrator
                    # only dispatches from the first non-complete layer.
                    "is_active_layer": False,  # set in loop below
                }
            )

        # Find the first non-complete layer → that's the active one.
        active_layer_idx = -1
        for layer in layers_list:
            if not layer["complete"]:
                active_layer_idx = int(layer["index"])
                layer["is_active_layer"] = True
                break

        # Next-dispatchable: live issues in the active layer that aren't
        # in_progress (so the orchestrator could pick them up).
        next_dispatchable: list[int] = []
        if active_layer_idx >= 0:
            for iss in layers_list[active_layer_idx]["issues"]:
                if iss["status"] == "pending":
                    next_dispatchable.append(int(iss["number"]))

        return {
            "paused": paused,
            "concurrency_cap": cap,
            "in_flight": in_flight,
            "remaining_slots": max(0, cap - in_flight),
            "active_layer_index": active_layer_idx,
            "next_dispatchable_issues": next_dispatchable,
            "layers": layers_list,
            "repo": settings.github.repo,
            "ts": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/decision_preview")
    async def api_decision_preview() -> dict[str, Any]:
        settings = _settings_or_500()
        try:
            decision = await orchestrator_tick(settings, apply=False, notify=False)
        except Exception as e:
            raise HTTPException(500, f"tick preview failed: {e}") from e
        return {
            "is_idle": decision.is_idle,
            "layer_index": decision.layer_index,
            "issues_to_dispatch": decision.issues_to_dispatch,
            "prs_to_review": decision.prs_to_review,
            "reason": decision.reason,
            "next_action": describe_next_action(decision, settings),
            "active_run_count": decision.state.active_run_count,
            "max_in_flight": settings.concurrency.max_in_flight,
        }

    @app.get("/api/prompts")
    async def api_prompts() -> list[dict[str, Any]]:
        from orclaw import overlay

        infos = overlay.list_prompts(_overlay_paths())
        return [
            {
                "name": p.name,
                "size_bytes": p.size_bytes,
                "is_overridden": p.is_overridden,
            }
            for p in infos
        ]

    @app.get("/api/prompts/{name}")
    async def api_prompt_get(name: str) -> dict[str, Any]:
        from orclaw import overlay

        _validate_prompt_name(name)
        try:
            content, is_overridden = overlay.read_prompt(_overlay_paths(), name)
        except FileNotFoundError as e:
            raise HTTPException(404, f"prompt not found: {name}") from e
        return {"name": name, "content": content, "is_overridden": is_overridden}

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        settings = _settings_or_500()
        with connect(settings.paths.db_path, read_only=True) as conn:
            override = get_concurrency_override(conn)
            effective = effective_max_in_flight(
                conn, settings_default=settings.concurrency.max_in_flight
            )
        return {
            "github": {
                "repo": settings.github.repo,
                "poll_interval_seconds": settings.github.poll_interval_seconds,
                "token_set": bool(settings.github.token),
            },
            "concurrency": {
                "max_in_flight": settings.concurrency.max_in_flight,
                "max_in_flight_override": override,
                "max_in_flight_effective": effective,
                "default_in_flight": settings.concurrency.default_in_flight,
            },
            "backoff": {
                "saturation_threshold_failures": settings.backoff.saturation_threshold_failures,
                "saturation_window_minutes": settings.backoff.saturation_window_minutes,
                "saturation_cooldown_minutes": settings.backoff.saturation_cooldown_minutes,
            },
            "paths": {
                "data_dir": str(settings.paths.data_dir),
                "db_path": str(settings.paths.db_path),
            },
            "notifications": {
                "telegram_enabled": settings.notifications.telegram_enabled,
                "healthchecks_orchestrator_set": bool(
                    settings.notifications.healthchecks_orchestrator_url
                ),
            },
            "log_level": settings.log_level,
        }

    # =====================================================================
    # TUNING — concurrency cap, timers, quota estimate
    # =====================================================================

    @app.get("/api/timers")
    async def api_timers() -> list[dict[str, Any]]:
        """List every .timer + .service unit known to the engine.

        Union of the git-tracked defaults and the user-managed overlay.
        Each entry carries flags so the UI can badge user-edited units
        (``is_overridden``) and user-created ones (``is_user_created``).
        """
        from orclaw import overlay

        return [
            {
                "name": t.name,
                "kind": t.kind,
                "size_bytes": t.size_bytes,
                "is_overridden": t.is_overridden,
                "is_user_created": t.is_user_created,
            }
            for t in overlay.list_timers(_overlay_paths())
        ]

    @app.get("/api/timers/{name}")
    async def api_timer_get(name: str) -> dict[str, Any]:
        from orclaw import overlay

        _validate_unit_name(name)
        try:
            content, info = overlay.read_timer(_overlay_paths(), name)
        except FileNotFoundError as e:
            raise HTTPException(404, f"unit not found: {name}") from e
        return {
            "name": info.name,
            "kind": info.kind,
            "content": content,
            "is_overridden": info.is_overridden,
            "is_user_created": info.is_user_created,
        }

    @app.get("/api/quota_estimate")
    async def api_quota_estimate() -> dict[str, Any]:
        """Pro-plan 5h-budget estimate.

        We count distinct outbound *dispatches* from the runs table in the
        last 5h — each dispatch posts a single ``@claude`` comment, which
        consumes one Pro-plan message. This is a **lower bound**: it
        doesn't see specialist chat from claude.ai. Treat the percentage
        as "minimum used in this window" — actual usage may be higher.
        """
        settings = _settings_or_500()
        since = datetime.now(UTC) - timedelta(hours=_QUOTA_WINDOW_HOURS)
        # SQLite stores timestamps as 'YYYY-MM-DD HH:MM:SS' (UTC). Compare
        # using the same string format to avoid TZ surprises.
        since_str = since.strftime("%Y-%m-%d %H:%M:%S")
        with connect(settings.paths.db_path, read_only=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM runs "
                "WHERE started_at >= ? "
                "  AND agent IN ('implementer', 'reviewer')",
                (since_str,),
            ).fetchone()
            dispatches = int(row["n"]) if row else 0
            by_agent_rows = conn.execute(
                "SELECT agent, COUNT(*) AS n FROM runs WHERE started_at >= ? GROUP BY agent",
                (since_str,),
            ).fetchall()
        by_agent = {r["agent"]: int(r["n"]) for r in by_agent_rows}
        budget = _QUOTA_BUDGET_PER_5H
        pct_used = round(100.0 * dispatches / budget, 1) if budget > 0 else 0.0
        return {
            "window_hours": _QUOTA_WINDOW_HOURS,
            "since": since.isoformat(),
            "until": datetime.now(UTC).isoformat(),
            "dispatches_counted": dispatches,
            "by_agent": by_agent,
            "budget_per_window": budget,
            "remaining_estimate": max(0, budget - dispatches),
            "pct_used": pct_used,
            "is_lower_bound": True,
        }

    # =====================================================================
    # WRITE actions — each one logs to events + Telegram
    # =====================================================================

    @app.post("/api/concurrency_cap")
    async def api_concurrency_cap(body: dict[str, Any]) -> dict[str, Any]:
        """Set or clear the runtime concurrency override.

        Body: ``{"value": int | null}`` — pass ``null`` to clear and fall
        back to the value in config. Negative values are rejected; 0 is
        accepted (acts as soft pause). The override survives restarts
        because it lives in engine_state.
        """
        raw = body.get("value", "__missing__")
        if raw == "__missing__":
            raise HTTPException(400, "body must include 'value' (int or null)")
        if raw is not None and not isinstance(raw, int):
            raise HTTPException(400, f"'value' must be int or null, got {type(raw).__name__}")
        if isinstance(raw, int) and raw < 0:
            raise HTTPException(400, "'value' must be ≥ 0")
        if isinstance(raw, int) and raw > 50:
            raise HTTPException(400, "'value' must be ≤ 50 (sanity cap)")

        settings = _settings_or_500()
        with connect(settings.paths.db_path) as conn:
            set_concurrency_override(conn, raw)
            effective = effective_max_in_flight(
                conn, settings_default=settings.concurrency.max_in_flight
            )
        msg = (
            f"🔧 Concurrency cap override cleared (now using config = {effective})"
            if raw is None
            else f"🔧 Concurrency cap override set to {raw}"
        )
        await _alert(settings, msg)
        return {
            "override": raw,
            "effective": effective,
            "default": settings.concurrency.max_in_flight,
        }

    @app.post("/api/pause")
    async def api_pause() -> dict[str, str]:
        settings = _settings_or_500()
        with connect(settings.paths.db_path) as conn:
            set_paused(conn, True)
        await _alert(settings, "⏸ Pause requested from dashboard")
        return {"status": "paused"}

    @app.post("/api/resume")
    async def api_resume() -> dict[str, str]:
        settings = _settings_or_500()
        with connect(settings.paths.db_path) as conn:
            set_paused(conn, False)
        await _alert(settings, "▶ Resume requested from dashboard")
        return {"status": "resumed"}

    @app.post("/api/force_tick")
    async def api_force_tick() -> dict[str, Any]:
        settings = _settings_or_500()
        try:
            decision = await orchestrator_tick(settings, apply=True, notify=True)
        except Exception as e:
            raise HTTPException(500, f"tick failed: {e}") from e
        return {
            "reason": decision.reason,
            "next_action": describe_next_action(decision, settings),
            "dispatched": len(decision.dispatched),
            "reviewers_dispatched": len(decision.reviewers_dispatched),
        }

    # =====================================================================
    # GitHub webhook — push instead of poll
    # =====================================================================

    async def _run_apply_tick() -> None:
        """Drive one apply-mode tick. Loads settings fresh so a token /
        config change lands without restarting the dashboard."""
        s = load_settings(require_secrets=True)
        await orchestrator_tick(s, apply=True, notify=True)

    # One coalescing runner per app instance: a burst of deliveries
    # collapses into "run now + at most one trailing run".
    tick_runner = webhooks.CoalescingTickRunner(_run_apply_tick)
    # Hold strong refs to background tasks so they aren't GC'd mid-flight.
    bg_tasks: set[asyncio.Task[None]] = set()

    @app.post("/webhook/github")
    async def github_webhook(request: Request) -> JSONResponse:
        """Receive a GitHub webhook delivery and (maybe) trigger a tick.

        Auth is the HMAC signature, NOT Cloudflare Access — this path must
        be excluded from Access so GitHub can reach it. See
        ``docs/webhooks.md``.
        """
        settings = _settings_or_500()
        secret = settings.github.webhook_secret
        if not secret:
            raise HTTPException(503, "webhooks disabled — set GITHUB_WEBHOOK_SECRET")

        raw = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not webhooks.verify_signature(secret, raw, signature):
            raise HTTPException(401, "invalid or missing signature")

        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if event == "ping":
            return JSONResponse({"status": "pong"})

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"invalid JSON body: {e}") from e

        if not webhooks.event_should_trigger(event, payload):
            return JSONResponse({"status": "ignored", "event": event})

        # Fire-and-forget so GitHub gets a fast 2xx; the runner serialises
        # overlapping deliveries internally.
        async def _bg() -> None:
            try:
                await tick_runner.trigger()
            except Exception as e:  # pragma: no cover — defensive
                log.error("webhook_bg_tick_failed", event=event, error=str(e))

        task = asyncio.create_task(_bg())
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        log.info(
            "webhook_received",
            gh_event=event,
            delivery=delivery,
            action=payload.get("action"),
        )
        return JSONResponse(
            {"status": "scheduled", "event": event, "delivery": delivery},
            status_code=202,
        )

    @app.post("/api/run_planner")
    async def api_run_planner() -> dict[str, Any]:
        settings = _settings_or_500()
        from orclaw.batch_planner import run_planner

        try:
            result = await run_planner(settings)
        except Exception as e:
            raise HTTPException(500, f"planner failed: {e}") from e
        await _alert(settings, f"📋 Planner run from dashboard — {result.layers_count} layers")
        return {
            "layers_count": result.layers_count,
            "issues_scanned": result.issues_scanned,
            "issues_planned": result.issues_planned,
            "issues_pending_added": result.issues_pending_added,
        }

    @app.post("/api/skip_issue/{number}")
    async def api_skip_issue(number: int, reason: str = "dashboard request") -> dict[str, str]:
        from orclaw.specialist.tools import skip_issue

        out = await skip_issue(issue_number=number, reason=reason)
        return {"result": out}

    @app.post("/api/require_human_review/{pr_number}")
    async def api_require_human_review(
        pr_number: int, reason: str = "dashboard request"
    ) -> dict[str, str]:
        from orclaw.specialist.tools import require_human_review

        out = await require_human_review(pr_number=pr_number, reason=reason)
        return {"result": out}

    @app.post("/api/force_review/{pr_number}")
    async def api_force_review(pr_number: int) -> dict[str, str]:
        from orclaw.specialist.tools import force_review

        out = await force_review(pr_number=pr_number)
        return {"result": out}

    @app.put("/api/prompts/{name}")
    async def api_prompt_update(name: str, body: dict[str, str]) -> dict[str, Any]:
        """Persist a prompt override to the overlay layer (no PR, no git).

        The new content lands in
        ``/var/lib/orclaw/overrides/prompts/<name>`` and the
        orchestrator picks it up on the next tick (prompts are loaded
        fresh each call). The git-tracked default is left untouched, so
        clearing the override later restores it.
        """
        from orclaw import overlay

        new_content = body.get("content")
        if not isinstance(new_content, str) or not new_content.strip():
            raise HTTPException(400, "content is required")
        _validate_prompt_name(name)
        try:
            overlay.set_prompt(_overlay_paths(), name, new_content)
        except OSError as e:
            raise HTTPException(500, f"failed to write overlay: {e}") from e
        await _alert(_settings_or_500(), f"📝 Prompt <code>{name}</code> overridden via dashboard")
        return {"name": name, "is_overridden": True, "bytes": len(new_content)}

    @app.delete("/api/prompts/{name}")
    async def api_prompt_revert(name: str) -> dict[str, Any]:
        """Drop a prompt override — reverts to the shipped default.

        Returns 404 if there was no override to revert (the default is
        always there; we never let the user delete it).
        """
        from orclaw import overlay

        _validate_prompt_name(name)
        existed = overlay.delete_prompt_override(_overlay_paths(), name)
        if not existed:
            raise HTTPException(404, f"no override to revert for {name}")
        await _alert(_settings_or_500(), f"↩ Prompt <code>{name}</code> reverted to default")
        return {"name": name, "is_overridden": False, "action": "reverted"}

    @app.put("/api/timers/{name}")
    async def api_timer_update(name: str, body: dict[str, str]) -> dict[str, Any]:
        """Write a unit override + apply to systemd (no PR, no git).

        Writes the new content to
        ``/var/lib/orclaw/overrides/systemd/<name>``, publishes it
        to ``/etc/systemd/system/<name>``, runs ``daemon-reload``, and
        ``try-restart``s the unit so the change is live within a
        second or two. Brand-new ``.timer`` units are automatically
        enabled + started.
        """
        from orclaw import overlay

        new_content = body.get("content")
        if not isinstance(new_content, str) or not new_content.strip():
            raise HTTPException(400, "content is required")
        _validate_unit_name(name)
        try:
            info = overlay.set_timer(_overlay_paths(), name, new_content)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[:300]
            raise HTTPException(500, f"systemctl failed: {stderr}") from e
        except OSError as e:
            raise HTTPException(500, f"failed to write overlay: {e}") from e
        await _alert(
            _settings_or_500(),
            f"⏱ Unit <code>{name}</code> updated via dashboard — applied to systemd",
        )
        return {
            "name": info.name,
            "kind": info.kind,
            "is_overridden": info.is_overridden,
            "is_user_created": info.is_user_created,
            "bytes": len(new_content),
        }

    @app.post("/api/timers")
    async def api_timer_create(body: dict[str, str]) -> dict[str, Any]:
        """Create a brand-new unit from scratch.

        Body: ``{"name": "my-thing.timer", "content": "..."}``. The
        name must not collide with an existing default or override —
        use ``PUT /api/timers/<name>`` to edit those. Brand-new ``.timer``
        units are enabled + started automatically.
        """
        from orclaw import overlay

        name = body.get("name", "").strip()
        new_content = body.get("content", "")
        if not isinstance(new_content, str) or not new_content.strip():
            raise HTTPException(400, "content is required")
        _validate_unit_name(name)
        paths = _overlay_paths()
        if (paths.systemd / name).exists() or (paths.defaults_systemd / name).exists():
            raise HTTPException(409, f"{name} already exists — use PUT to edit")
        try:
            info = overlay.set_timer(paths, name, new_content, enable_if_new=True)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[:300]
            raise HTTPException(500, f"systemctl failed: {stderr}") from e
        await _alert(_settings_or_500(), f"+ New unit <code>{name}</code> created via dashboard")
        return {
            "name": info.name,
            "kind": info.kind,
            "is_user_created": info.is_user_created,
            "bytes": len(new_content),
        }

    @app.delete("/api/timers/{name}")
    async def api_timer_delete(name: str) -> dict[str, Any]:
        """Remove a unit.

        - If a git-tracked default exists → "reverted": overlay deleted,
          default content re-published to systemd, daemon-reload +
          try-restart.
        - If the unit was user-created → "removed": stop + disable +
          unlink from ``/etc/systemd/system/`` + delete overlay.
        """
        from orclaw import overlay

        _validate_unit_name(name)
        try:
            action = overlay.delete_timer(_overlay_paths(), name)
        except FileNotFoundError as e:
            raise HTTPException(404, f"unit not found: {name}") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")[:300]
            raise HTTPException(500, f"systemctl failed: {stderr}") from e
        await _alert(
            _settings_or_500(),
            f"🗑 Unit <code>{name}</code> {action} via dashboard",
        )
        return {"name": name, "action": action}

    return app


# --- helpers --------------------------------------------------------------


def _settings_or_500() -> Any:
    """Load settings or fail with 500 carrying the error message."""
    try:
        return load_settings(require_secrets=False)
    except OrclawError as e:
        raise HTTPException(500, f"config error: {e}") from e


def _row_to_dict(row: Any) -> dict[str, Any]:
    """SQLite Row → plain dict for JSON serialisation."""
    return dict(row)


def _overlay_paths() -> Any:
    """Build the production overlay paths (or a test override).

    Tests monkeypatch ``orclaw.dashboard.app._overlay_paths`` with
    a lambda returning an :class:`~orclaw.overlay.OverlayPaths` that
    points everything under ``tmp_path`` and sets
    ``apply_to_systemd=False`` so file ops can be verified in isolation.
    """
    from orclaw import overlay

    return overlay.default_paths()


def _validate_prompt_name(name: str) -> None:
    """Cheap name guard. Rejects path traversal + non-``.md`` names."""
    if not name or "/" in name or "\\" in name or ".." in name or not name.endswith(".md"):
        raise HTTPException(400, f"invalid prompt name: {name!r}")


def _validate_unit_name(name: str) -> None:
    """Cheap name guard for systemd unit files (.timer or .service)."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, f"invalid unit name: {name!r}")
    if not (name.endswith(".timer") or name.endswith(".service")):
        raise HTTPException(400, "name must end in .timer or .service")


async def _alert(settings: Any, message: str) -> None:
    """Best-effort Telegram on write actions. Never raises."""
    try:
        await post_telegram(
            settings.notifications, f"<b>[dashboard]</b> {message}", parse_mode="HTML"
        )
    except Exception as e:
        log.warning("dashboard_alert_failed", error=str(e))


# Module-level singleton so uvicorn can target it as ``orclaw.dashboard.app:app``.
app = create_app()


def main() -> None:  # pragma: no cover — CLI hook
    """Entrypoint used by ``orclaw dashboard serve``."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="info")


# Ensure JSON responses set a strict charset (Cloudflare strips it otherwise).
JSONResponse.media_type = "application/json; charset=utf-8"

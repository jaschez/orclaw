"""GitHub Actions self-hosted runner — health + activity monitor.

This module supervises the ``actions.runner.*`` systemd service that
processes ${TARGET_REPO}'s CI workflows, and the GitHub-side state around it.
Two concerns:

1. **Health & graceful fallback** (:func:`check_runner_health`)

   The self-hosted runner lives on the same VM as the engine — if it
   crashes, OOMs, or systemd marks it ``failed``, every workflow that
   targets ``self-hosted`` gets stuck "Queued" until we notice.

   Mitigation: workflows in ${TARGET_REPO} use ``runs-on: ${{ vars.ORCLAW_RUNNER
   || 'ubuntu-latest' }}``. This module flips the repo-level
   ``ORCLAW_RUNNER`` variable to ``ubuntu-latest`` the moment the runner
   goes down, so the next workflow firing falls back to GitHub-hosted
   automatically. When the runner recovers, it flips back to
   ``self-hosted``. Both transitions ping Telegram.

2. **Suspicious activity detection** (:func:`check_suspicious_workflow_runs`)

   The self-hosted runner is a juicy target: a malicious commit could
   exfiltrate secrets, install crypto miners, etc. We can't fully
   sandbox a runner without ephemeral VMs, so we settle for *detection*:

   - workflow runs lasting longer than ``LONG_RUN_THRESHOLD_MIN``
   - runs triggered by actors outside the trusted set
   - runs that fail unexpectedly

   Any of these → Telegram alert with the run URL so the operator
   investigates manually.

The whole thing is run via ``orclaw runner monitor`` from a systemd
timer firing every 2 minutes. It's idempotent and writes a thin amount
of state into ``engine_state`` (last alert IDs to de-dup).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from orclaw.github_client import GitHubAPIConfig, GitHubClient
from orclaw.logging import get_logger
from orclaw.notifications import post_telegram

if TYPE_CHECKING:
    import sqlite3

    from orclaw.config import Settings

log = get_logger(__name__)


# --- Tunables --------------------------------------------------------------

#: How long a single workflow run can take before we flag it as suspicious.
#: CI jobs on ${TARGET_REPO} should never exceed this — if they do, it's either
#: a hang or someone is using the runner for free compute.
LONG_RUN_THRESHOLD = timedelta(minutes=15)

#: Actors we expect to trigger workflows. Any other login → Telegram alert.
#: Includes the well-known bots that legitimately fire workflows
#: (``github-actions[bot]`` for re-runs, ``vercel[bot]`` for preview
#: deploys). Your own GitHub username gets added at runtime from the
#: ``ORCLAW_TRUSTED_ACTORS`` env var (comma-separated). Extend that env
#: var when you add collaborators or integrate a new bot.
_DEFAULT_TRUSTED = {"github-actions[bot]", "vercel[bot]"}
_extra = {a.strip() for a in os.environ.get("ORCLAW_TRUSTED_ACTORS", "").split(",") if a.strip()}
TRUSTED_ACTORS: frozenset[str] = frozenset(_DEFAULT_TRUSTED | _extra)

#: How far back to scan for suspicious runs each tick. Keep small so we
#: don't re-process the same runs forever — combined with the de-dup
#: cursor below.
SUSPICIOUS_SCAN_WINDOW = timedelta(minutes=10)

#: Repo-level variable name used by ${TARGET_REPO} workflows for the runs-on
#: selector. Workflows use:
#:   runs-on: ${{ vars.ORCLAW_RUNNER || 'ubuntu-latest' }}
RUNNER_VAR_NAME = "ORCLAW_RUNNER"

#: Systemd unit name for the GitHub Actions runner (matches what
#: ``svc.sh`` creates when you register a self-hosted runner). The
#: pattern is ``actions.runner.<owner>-<repo>.<runner_name>.service``.
#: Set via ``ORCLAW_RUNNER_SYSTEMD_UNIT`` env var since the value
#: depends on the owner/repo/name you used during registration.
RUNNER_SYSTEMD_UNIT = os.environ.get(
    "ORCLAW_RUNNER_SYSTEMD_UNIT", "actions.runner.self-hosted.service"
)

#: engine_state keys for de-dup.
_KEY_LAST_RUNNER_STATE = "runner_last_known_state"
_KEY_LAST_ALERTED_RUN_ID = "runner_last_alerted_run_id"


# --- Result DTOs -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunnerHealth:
    """Outcome of one health-check pass."""

    systemd_active: bool
    """Whether the runner systemd unit is currently active."""
    previous_state_was_active: bool
    """What we recorded last time. Used to detect *transitions*."""
    repo_variable_value: str | None
    """Current value of ORCLAW_RUNNER on the repo (None if not set)."""
    transitioned: bool
    """True iff systemd state flipped since the last check."""
    repo_variable_updated: bool
    """True iff this check modified ORCLAW_RUNNER."""


@dataclass(frozen=True, slots=True)
class SuspiciousActivity:
    """Outcome of one suspicious-workflow-runs pass."""

    flagged: list[tuple[int, str]] = field(default_factory=list)
    """``(run_id, reason)`` for each run we alerted on this pass."""


# --- Systemd probing -------------------------------------------------------


def _is_runner_active(unit: str = RUNNER_SYSTEMD_UNIT) -> bool:
    """Return True iff systemd considers the runner unit ``active``.

    Returns False on any error (unit not found, systemctl missing) — the
    assumption being "I can't confirm it's up → treat as down" so we
    fall back to GitHub-hosted aggressively rather than leaving CI stuck.
    """
    try:
        out = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("runner_systemd_probe_failed", error=str(e))
        return False
    return out.stdout.strip() == "active"


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO engine_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = datetime('now')",
        (key, value),
    )


# --- GitHub probing --------------------------------------------------------


async def _get_repo_variable(settings: Settings, name: str = RUNNER_VAR_NAME) -> str | None:
    """Read a repo-level Actions variable. ``None`` if not set / 404."""
    api = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
    async with GitHubClient(api) as gh:
        resp = await gh._http.get(f"/repos/{settings.github.repo}/actions/variables/{name}")
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        log.warning("repo_var_get_failed", status=resp.status_code)
        return None
    return str(resp.json().get("value", ""))


async def _set_repo_variable(settings: Settings, name: str, value: str) -> bool:
    """PUT a repo variable. Creates it if missing. Returns True on success."""
    api = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
    async with GitHubClient(api) as gh:
        # PATCH first (update). If 404, POST (create).
        resp = await gh._http.patch(
            f"/repos/{settings.github.repo}/actions/variables/{name}",
            json={"name": name, "value": value},
        )
        if resp.status_code == 404:
            resp = await gh._http.post(
                f"/repos/{settings.github.repo}/actions/variables",
                json={"name": name, "value": value},
            )
    if resp.status_code not in (200, 201, 204):
        log.warning("repo_var_set_failed", status=resp.status_code, body=resp.text[:200])
        return False
    return True


# --- Health check ----------------------------------------------------------


async def check_runner_health(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    unit: str = RUNNER_SYSTEMD_UNIT,
) -> RunnerHealth:
    """One health-check pass.

    Compares systemd state vs. recorded state vs. the repo variable, and
    reconciles all three. Emits a Telegram alert on transition.
    """
    is_active = _is_runner_active(unit)
    previous_str = _get_state(conn, _KEY_LAST_RUNNER_STATE)
    previous_active = previous_str == "active"
    transitioned = is_active != previous_active or previous_str is None

    current_var = await _get_repo_variable(settings)
    desired_var = "self-hosted" if is_active else "ubuntu-latest"

    repo_var_updated = False
    if current_var != desired_var:
        repo_var_updated = await _set_repo_variable(settings, RUNNER_VAR_NAME, desired_var)
        if repo_var_updated:
            log.info(
                "runner_repo_var_flipped",
                from_value=current_var,
                to_value=desired_var,
                reason="systemd_state",
            )

    # Persist the new state.
    _set_state(conn, _KEY_LAST_RUNNER_STATE, "active" if is_active else "inactive")

    # Alert only on transitions (to avoid spam every 2 min).
    if transitioned:
        if is_active:
            await post_telegram(
                settings.notifications,
                "<b>🟢 Self-hosted runner UP</b>\n\n"
                f"Unit: <code>{unit}</code>\n"
                f"ORCLAW_RUNNER: <code>{desired_var}</code>\n"
                "Workflows que usen <code>vars.ORCLAW_RUNNER</code> volverán "
                "a la VM en el próximo firing.",
                parse_mode="HTML",
            )
        else:
            await post_telegram(
                settings.notifications,
                "<b>🔴 Self-hosted runner DOWN</b>\n\n"
                f"Unit: <code>{unit}</code>\n"
                f"ORCLAW_RUNNER flipped to: <code>{desired_var}</code>\n\n"
                "Próximos workflows correrán en GitHub-hosted hasta que "
                "vuelva. Investigar con: <code>systemctl status "
                f"{unit}</code>",
                parse_mode="HTML",
            )

    return RunnerHealth(
        systemd_active=is_active,
        previous_state_was_active=previous_active,
        repo_variable_value=desired_var if repo_var_updated else current_var,
        transitioned=transitioned,
        repo_variable_updated=repo_var_updated,
    )


# --- Suspicious activity ---------------------------------------------------


def _classify_run(run: dict[str, object]) -> str | None:
    """Return a human reason string if the run is suspicious, else None.

    Pure function — easy to test with synthetic payloads.
    """
    actor_field = run.get("actor")
    actor = actor_field.get("login") if isinstance(actor_field, dict) else None
    status = run.get("status")
    conclusion = run.get("conclusion")
    created_at = run.get("created_at")
    updated_at = run.get("updated_at")

    if actor and actor not in TRUSTED_ACTORS:
        return f"actor not in trusted set: {actor!r}"

    if conclusion == "failure":
        return "workflow run failed"

    # Long run detection — only meaningful for in_progress runs (completed
    # ones tell us nothing actionable; failed ones already caught above).
    if status in {"in_progress", "queued"} and isinstance(created_at, str):
        try:
            started = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if datetime.now(UTC) - started > LONG_RUN_THRESHOLD:
                return f"running >{LONG_RUN_THRESHOLD} (started {created_at})"
        except ValueError:
            pass

    _ = updated_at  # reserved for future heuristics
    return None


async def check_suspicious_workflow_runs(
    settings: Settings,
    conn: sqlite3.Connection,
) -> SuspiciousActivity:
    """Poll recent workflow runs and Telegram-alert on anything unusual.

    De-duped via ``runner_last_alerted_run_id``: we only alert on runs
    with id > the last one we alerted on, so each run is reported once.
    """
    api = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
    since_iso = (datetime.now(UTC) - SUSPICIOUS_SCAN_WINDOW).isoformat()

    async with GitHubClient(api) as gh:
        resp = await gh._http.get(
            f"/repos/{settings.github.repo}/actions/runs",
            params={"per_page": 30, "created": f">={since_iso}"},
        )
    if resp.status_code != 200:
        log.warning("workflow_runs_fetch_failed", status=resp.status_code)
        return SuspiciousActivity()

    runs = resp.json().get("workflow_runs", [])

    last_alerted_str = _get_state(conn, _KEY_LAST_ALERTED_RUN_ID)
    last_alerted = int(last_alerted_str) if last_alerted_str else 0
    highest_seen = last_alerted

    flagged: list[tuple[int, str]] = []
    for run in runs:
        rid = int(run["id"])
        highest_seen = max(highest_seen, rid)
        if rid <= last_alerted:
            continue
        reason = _classify_run(run)
        if reason is None:
            continue
        flagged.append((rid, reason))
        await post_telegram(
            settings.notifications,
            "<b>⚠️ Suspicious workflow run</b>\n\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>Workflow:</b> <code>{run.get('name', '?')}</code>\n"
            f"<b>Branch:</b> <code>{run.get('head_branch', '?')}</code>\n"
            f"<b>URL:</b> {run.get('html_url', '?')}",
            parse_mode="HTML",
        )

    if highest_seen > last_alerted:
        _set_state(conn, _KEY_LAST_ALERTED_RUN_ID, str(highest_seen))

    return SuspiciousActivity(flagged=flagged)


# --- Public entrypoint -----------------------------------------------------


async def run_monitor(settings: Settings) -> tuple[RunnerHealth, SuspiciousActivity]:
    """One full monitor pass — runs both checks under one DB connection."""
    from orclaw.db import connect

    log.info("runner_monitor_started")
    with connect(settings.paths.db_path) as conn:
        health = await check_runner_health(settings, conn)
        activity = await check_suspicious_workflow_runs(settings, conn)
    log.info(
        "runner_monitor_completed",
        runner_active=health.systemd_active,
        transitioned=health.transitioned,
        suspicious=len(activity.flagged),
    )
    return health, activity


def main() -> None:  # pragma: no cover — entrypoint
    """CLI bridge: load settings + run monitor."""
    from orclaw.config import load_settings

    settings = load_settings(require_secrets=True)
    asyncio.run(run_monitor(settings))

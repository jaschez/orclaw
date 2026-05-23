"""Specialist tool catalogue.

Each function corresponds to one row of the operational cheatsheet. They
are designed to be wrapped as MCP tools: their type hints become the
JSON schema, their docstrings become the ``description`` that Claude
uses to choose between them.

Conventions:

- Return plain ``str``. MCP renders strings natively; markdown is OK.
- Never raise on expected failures — catch and return a human-readable
  error string. Raising would surface as a tool error in the chat.
- Async tools are fine for FastMCP, but we keep most sync because the
  underlying state.py functions are sync. Async-only ones (anything
  touching GitHubClient) are explicitly ``async``.

All settings are loaded via :func:`load_settings` on each call. That's
intentional: secrets may have been rotated between calls.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import timedelta

from orclaw.config import load_settings
from orclaw.db import connect
from orclaw.exceptions import OrclawError
from orclaw.github_client import GitHubAPIConfig, GitHubClient
from orclaw.logging import get_logger
from orclaw.orchestrator import (
    is_paused,
    orchestrator_tick,
    set_paused,
)
from orclaw.summary import build_summary, format_summary_telegram

log = get_logger(__name__)


# =========================================================================
# Helpers
# =========================================================================


def _safe(fn: str) -> str:
    """Trim user output to a reasonable size for chat display."""
    if len(fn) > 8000:
        return fn[:8000] + "\n\n…(truncated)"
    return fn


# =========================================================================
# READ-ONLY tools — safe by definition
# =========================================================================


def get_status() -> str:
    """Return a snapshot of the orchestrator's current state.

    Use this when the user asks "how's the engine doing?", "what's the
    status?", "is it paused?". Returns counts of batches by status, runs
    in flight, and the pause flag.
    """
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    with connect(settings.paths.db_path, read_only=True) as conn:
        paused = is_paused(conn)
        batch_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM batches GROUP BY status"
        ).fetchall()
        run_rows = conn.execute(
            "SELECT agent, status, COUNT(*) AS n FROM runs GROUP BY agent, status"
        ).fetchall()
        in_flight = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued', 'running')"
        ).fetchone()

    lines = [
        f"**Paused:** {'yes ⏸' if paused else 'no ▶'}",
        f"**In flight:** {in_flight['n']} / {settings.concurrency.max_in_flight}",
        "",
        "**Batches:**",
    ]
    if batch_rows:
        for row in batch_rows:
            lines.append(f"  - {row['status']}: {row['n']}")
    else:
        lines.append("  - (empty — run the planner)")

    lines.append("")
    lines.append("**Runs:**")
    if run_rows:
        for row in run_rows:
            lines.append(f"  - {row['agent']}.{row['status']}: {row['n']}")
    else:
        lines.append("  - (none yet)")

    return "\n".join(lines)


async def get_decision_preview() -> str:
    """Show what the orchestrator would dispatch on the next tick, without acting.

    Use this when the user asks "what's about to happen?", "what's next
    in the queue?", "preview the next tick". Equivalent to
    ``orclaw orchestrator tick`` (dry-run).
    """
    try:
        settings = load_settings(require_secrets=True)
        decision = await orchestrator_tick(settings, apply=False, notify=False)
    except OrclawError as e:
        return f"❌ Config error: {e}"
    except Exception as e:
        return f"❌ Tick failed: {e}"

    lines = [
        f"**Decision:** {'IDLE' if decision.is_idle else 'DISPATCH'}",
        f"**Reason:** {decision.reason}",
        f"**Layer:** {decision.layer_index}",
        f"**Would dispatch:** {decision.issues_to_dispatch or '(none)'}",
    ]
    if decision.prs_to_review:
        lines.append(f"**PRs awaiting review:** {decision.prs_to_review}")
    return "\n".join(lines)


def get_recent_logs(lines: int = 50, unit: str = "orclaw-orchestrator") -> str:
    """Tail recent journalctl output for one of the systemd units.

    Use this when the user wants to debug ("what happened last?",
    "show me the logs", "is something crashing?"). ``unit`` defaults to
    orclaw-orchestrator; other valid values: orclaw-batch-planner,
    orclaw-summary.
    """
    if unit not in {"orclaw-orchestrator", "orclaw-batch-planner", "orclaw-summary"}:
        return f"❌ Invalid unit '{unit}'. Valid: orclaw-orchestrator | orclaw-batch-planner | orclaw-summary."
    if lines < 1 or lines > 1000:
        return "❌ `lines` must be between 1 and 1000."

    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "❌ journalctl not available (are you on the engine host?)"
    except subprocess.TimeoutExpired:
        return "❌ journalctl timed out."

    return _safe(out.stdout or out.stderr or "(no output)")


def query_runs(limit: int = 20, agent: str | None = None, status: str | None = None) -> str:
    """List recent run rows from the SQLite DB.

    Use this when the user wants to see what's been happening: "what
    runs are in flight?", "what failed today?". Optional filters by
    ``agent`` (implementer / reviewer / specialist / batch_planner) and
    ``status`` (queued / running / success / failed / timeout / aborted /
    rate_limited).
    """
    if limit < 1 or limit > 200:
        return "❌ `limit` must be between 1 and 200."

    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    query = "SELECT id, agent, status, issue_number, pr_number, started_at, finished_at FROM runs"
    where: list[str] = []
    params: list[object] = []
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

    if not rows:
        return "(no runs match)"

    out = ["| id | agent | status | issue | pr | started |", "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| `{r['id']}` | {r['agent']} | {r['status']} | "
            f"{r['issue_number'] or '—'} | {r['pr_number'] or '—'} | "
            f"{r['started_at']} |"
        )
    return "\n".join(out)


def query_batches(limit: int = 50, status: str | None = None) -> str:
    """List batch rows from the SQLite DB.

    Use this when the user asks about the implementation queue: "what's
    pending?", "what's been merged today?", "show me the batches".
    Optional filter by ``status`` (pending / in_progress / merged /
    failed / skipped).
    """
    if limit < 1 or limit > 500:
        return "❌ `limit` must be between 1 and 500."

    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    query = "SELECT layer, issue_number, status, pr_number, updated_at FROM batches"
    params: list[object] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY layer, issue_number LIMIT ?"
    params.append(limit)

    with connect(settings.paths.db_path, read_only=True) as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        return "(no batches match)"

    out = ["| layer | issue | status | pr | updated |", "|---|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| {r['layer']} | #{r['issue_number']} | {r['status']} | "
            f"{('#' + str(r['pr_number'])) if r['pr_number'] else '—'} | "
            f"{r['updated_at']} |"
        )
    return "\n".join(out)


def get_summary(window_days: int = 1) -> str:
    """Build the activity digest (default last 24h, or N days).

    Use this when the user wants a high-level overview: "give me the
    daily summary", "how did the last week go?". For weekly digests
    pass ``window_days=7``.
    """
    if window_days < 1 or window_days > 30:
        return "❌ `window_days` must be between 1 and 30."

    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    with connect(settings.paths.db_path, read_only=True) as conn:
        snap = build_summary(conn, window=timedelta(days=window_days))
    return format_summary_telegram(snap)


def doctor() -> str:
    """Run the engine's diagnostic health-check.

    Use this when the user asks "is something broken?", "run a doctor
    check", "are the secrets configured?". Returns a per-check report.
    """
    checks: list[tuple[str, bool, str]] = []
    try:
        settings = load_settings(require_secrets=True)
        checks.append(("config + secrets", True, "ok"))
    except OrclawError as e:
        return f"❌ Config error: {e}\n\n(Other checks skipped.)"

    db_path = settings.paths.db_path
    if not db_path.is_file():
        checks.append(("database file", False, f"missing at {db_path}"))
    else:
        try:
            with connect(db_path, read_only=True) as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            expected = {"batches", "runs", "reviews", "engine_state", "token_ledger"}
            missing = expected - tables
            checks.append(
                (
                    "schema",
                    not missing,
                    "ok" if not missing else f"missing: {sorted(missing)}",
                )
            )
        except Exception as e:
            checks.append(("schema readable", False, str(e)))

    async def _gh() -> tuple[bool, str]:
        try:
            cfg = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
            async with GitHubClient(cfg) as gh:
                await gh.list_prs(state="open", base="develop")
            return True, "ok"
        except Exception as e:
            return False, str(e)

    ok, msg = asyncio.run(_gh())
    checks.append((f"github ({settings.github.repo})", ok, msg))

    checks.append(
        (
            "telegram",
            True,
            "configured" if settings.notifications.telegram_enabled else "disabled",
        )
    )
    checks.append(
        (
            "healthchecks",
            True,
            "configured" if settings.notifications.healthchecks_orchestrator_url else "disabled",
        )
    )

    out = ["| Check | Status | Detail |", "|---|---|---|"]
    for name, ok, detail in checks:
        out.append(f"| {name} | {'✓' if ok else '✗'} | {detail} |")
    return "\n".join(out)


# =========================================================================
# ACTION tools — change state
# =========================================================================


def pause_orchestrator() -> str:
    """Pause new dispatches (in-flight work continues).

    Use when the user wants to halt the engine without killing running
    agents — e.g., "pause the engine", "stop dispatching", "freeze it
    for a bit". To stop in-flight work too, use systemctl on the host.
    """
    try:
        settings = load_settings(require_secrets=False)
        with connect(settings.paths.db_path) as conn:
            set_paused(conn, True)
    except Exception as e:
        return f"❌ Failed to pause: {e}"
    return "⏸ Orchestrator paused. New dispatches blocked; in-flight work continues."


def resume_orchestrator() -> str:
    """Lift the pause flag, allowing new dispatches again.

    Use when the user says "resume", "unpause", "go ahead", "carry on".
    """
    try:
        settings = load_settings(require_secrets=False)
        with connect(settings.paths.db_path) as conn:
            set_paused(conn, False)
    except Exception as e:
        return f"❌ Failed to resume: {e}"
    return "▶ Orchestrator resumed."


async def force_tick() -> str:
    """Run one orchestrator tick *now* in apply mode (don't wait for the timer).

    Use when the user wants immediate action: "tick now", "run a cycle",
    "dispatch what's ready". Posts real @claude comments. Returns a
    summary of what happened.
    """
    try:
        settings = load_settings(require_secrets=True)
        decision = await orchestrator_tick(settings, apply=True, notify=True)
    except OrclawError as e:
        return f"❌ Config error: {e}"
    except Exception as e:
        return f"❌ Tick failed: {e}"

    lines = [
        f"**Result:** {'IDLE' if decision.is_idle else 'DISPATCH'}",
        f"**Reason:** {decision.reason}",
    ]
    if decision.dispatched:
        lines.append(
            "**Implementer dispatched:** "
            + ", ".join(f"#{d.issue_number}→{d.run_id}" for d in decision.dispatched)
        )
    if decision.reviewers_dispatched:
        lines.append(
            "**Reviewer dispatched:** "
            + ", ".join(f"PR#{r.pr_number}→{r.run_id}" for r in decision.reviewers_dispatched)
        )
    if not decision.pollback.is_noop:
        lines.append(
            f"**Pollback:** completed {len(decision.pollback.reviews_completed)} reviews, "
            f"merged {len(decision.pollback.batches_merged)} batches"
        )
    return "\n".join(lines)


async def run_planner() -> str:
    """Re-scan GitHub issues and recompute the batch layers.

    Use when the user has added/removed issues on GitHub and wants the
    engine to see them: "refresh the plan", "re-run the planner",
    "rescan issues".
    """
    try:
        settings = load_settings(require_secrets=True)
        from orclaw.batch_planner import run_planner as _run

        result = await _run(settings)
    except OrclawError as e:
        return f"❌ Config error: {e}"
    except Exception as e:
        return f"❌ Planner failed: {e}"

    return (
        f"📋 Planner finished.\n"
        f"- layers: `{result.layers_count}`\n"
        f"- issues scanned: `{result.issues_scanned}`\n"
        f"- issues planned: `{result.issues_planned}`\n"
        f"- newly added: `{result.issues_pending_added}`\n"
        f"- already tracked: `{result.issues_already_tracked}`"
    )


async def skip_issue(issue_number: int, reason: str = "specialist request") -> str:
    """Label an issue ``do-not-implement`` so the planner excludes it.

    Use when the user wants to take an issue out of the pipeline:
    "skip issue 99", "don't implement #X", "exclude that one". Adds the
    label on GitHub *and* re-runs the planner so the change takes effect
    on the next tick. ``reason`` is recorded in a comment on the issue.
    """
    if issue_number < 1:
        return f"❌ Invalid issue number: {issue_number}"

    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    api = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
    try:
        async with GitHubClient(api) as gh:
            await gh.add_label(issue_number, "do-not-implement")
            await gh.post_comment(
                issue_number,
                f"🚫 Excluded from the engine pipeline.\n\nReason: {reason}\n\n"
                f"(via specialist tool)",
            )
    except Exception as e:
        return f"❌ Failed to label/comment on #{issue_number}: {e}"

    # Re-run planner so the local DB reflects the exclusion.
    from orclaw.batch_planner import run_planner as _run

    try:
        await _run(settings)
    except Exception as e:
        return f"⚠️ Label applied, but planner re-run failed: {e}"

    return f"🚫 Issue #{issue_number} excluded ('do-not-implement' label + planner refreshed)."


async def require_human_review(pr_number: int, reason: str = "specialist request") -> str:
    """Label a PR ``requires-human-review`` so the reviewer agent skips it.

    Use when the user wants to gate a PR for manual review: "I want to
    review PR 142 myself", "require human review on that PR".
    """
    if pr_number < 1:
        return f"❌ Invalid PR number: {pr_number}"

    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    api = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
    try:
        async with GitHubClient(api) as gh:
            await gh.add_label(pr_number, "requires-human-review")
            await gh.post_comment(
                pr_number,
                f"👤 Routed to human review.\n\nReason: {reason}\n\n(via specialist tool)",
            )
    except Exception as e:
        return f"❌ Failed: {e}"

    return f"👤 PR #{pr_number} marked for human review (reviewer agent will skip)."


async def force_review(pr_number: int) -> str:
    """Remove all ``review:*`` labels on a PR so the reviewer dispatches it again.

    Use when the user wants to re-review a PR: "review PR 142 again",
    "redo the review", "the reviewer was wrong about that PR". Strips
    every review:* label, including ``review:pending``.
    """
    if pr_number < 1:
        return f"❌ Invalid PR number: {pr_number}"

    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    review_labels = [
        "review:pending",
        "review:approved",
        "review:minor-fixes-applied",
        "review:needs-changes",
        "review:hard-block",
    ]
    api = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
    removed: list[str] = []
    try:
        async with GitHubClient(api) as gh:
            for label in review_labels:
                try:
                    await gh.remove_label(pr_number, label)
                    removed.append(label)
                except Exception:
                    pass  # label not present, that's fine
    except Exception as e:
        return f"❌ Failed: {e}"

    if not removed:
        return f"PR #{pr_number} had no review:* labels — nothing to do."
    return f"🔁 PR #{pr_number}: removed {removed}. Next tick will re-dispatch the reviewer."


# =========================================================================
# Engine self-modification (propose changes to orclaw itself)
# =========================================================================
#
# The flow these tools enable:
#   1. User asks for a code change in chat.
#   2. Specialist (Claude) drafts the new file content.
#   3. propose_engine_change() commits + opens PR against orclaw main.
#   4. User reviews the PR (link returned), accepts.
#   5. merge_engine_pr() merges → GitHub fires the auto-deploy workflow on
#      our self-hosted runner → ~30s later the VM is running the new code.
#
# Hardcoded to the orclaw repo. If you want to edit ${TARGET_REPO} use
# the implementer dispatcher (which is what the orchestrator already
# does on every tick).

# The slug of the orclaw deployment itself. Used by the self-amend
# tools (propose_engine_change etc.) so the specialist can suggest
# edits to its own code. Set via ORCLAW_SELF_REPO env var; format
# ``owner/name``. Defaults to empty — the self-amend tools refuse to
# run if unset.
_ENGINE_REPO_SLUG = os.environ.get("ORCLAW_SELF_REPO", "")


async def propose_engine_change(
    branch: str,
    files: dict[str, str],
    commit_message: str,
    pr_title: str,
    pr_body: str = "",
) -> str:
    """Open a PR against orclaw main with one or more file changes.

    Use this when the user asks for a configuration / code tweak that
    should ship to the engine itself (raise a cap, change a default,
    add a tool, tweak the prompt). DO NOT use this for ${TARGET_REPO}
    business-logic changes — the implementer agent handles those.

    Parameters
    ----------
    branch:
        New branch name. MUST be prefixed (e.g. ``fix/raise-cap``,
        ``feat/add-helper``, ``chore/typo``). Must not already exist.
    files:
        Mapping of repo-rooted path → FULL new file content (UTF-8). To
        change one line in a 500-line file, you still send the full 500
        lines. Read the current content first with ``read_file()`` if
        unsure — but DON'T overwrite without seeing the current content,
        you'd discard intermediate edits.
    commit_message:
        Imperative single-line summary (e.g. ``fix(config): raise
        max_in_flight default to 2``).
    pr_title:
        Same as commit_message typically.
    pr_body:
        Markdown describing WHY the change is needed. Include enough
        context that you (the human reviewing on mobile) can decide to
        merge without re-asking.

    Returns a one-line confirmation with the PR URL. Merging that PR
    triggers the auto-deploy workflow.
    """
    if not branch or "/" not in branch:
        return "❌ branch must be prefixed (e.g. 'fix/...', 'feat/...', 'chore/...')"
    if not files:
        return "❌ files dict cannot be empty"
    if not commit_message.strip():
        return "❌ commit_message is required"

    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    from orclaw.github_repo_ops import commit_multi_file_change

    try:
        result = await commit_multi_file_change(
            token=settings.github.token,
            repo=_ENGINE_REPO_SLUG,
            branch=branch,
            files=files,
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=pr_body,
        )
    except Exception as e:
        return f"❌ Failed to open PR: {e}"

    return (
        f"✓ Opened PR **#{result.pr_number}** on {_ENGINE_REPO_SLUG}\n\n"
        f"- branch: `{result.branch}`\n"
        f"- commit: `{result.commit_sha[:7]}`\n"
        f"- URL: {result.pr_url}\n\n"
        f"Review with `view_engine_pr({result.pr_number})`. "
        f"Merge with `merge_engine_pr({result.pr_number})` — that will "
        f"trigger the auto-deploy workflow (~30s to take effect)."
    )


async def view_engine_pr(pr_number: int) -> str:
    """Show diff summary + status for a orclaw PR.

    Use this when the user wants to inspect a PR before merging
    ("show me #17", "what does that PR change?"). Returns the title, body,
    state, mergeable status, plus per-file additions/deletions and a
    truncated patch for each file.
    """
    if pr_number < 1:
        return f"❌ Invalid PR number: {pr_number}"

    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    from orclaw.github_repo_ops import get_pull_request, get_pull_request_files

    try:
        pr = await get_pull_request(
            token=settings.github.token, repo=_ENGINE_REPO_SLUG, pr_number=pr_number
        )
        files = await get_pull_request_files(
            token=settings.github.token, repo=_ENGINE_REPO_SLUG, pr_number=pr_number
        )
    except Exception as e:
        return f"❌ Failed to fetch PR: {e}"

    state = pr.get("state", "?")
    mergeable = pr.get("mergeable")
    merge_state = pr.get("mergeable_state", "?")
    title = pr.get("title", "?")
    body = (pr.get("body") or "")[:500]
    head_ref = (pr.get("head") or {}).get("ref", "?")  # type: ignore[union-attr]

    lines = [
        f"**PR #{pr_number}** — {title}",
        "",
        f"- state: `{state}` · mergeable: `{mergeable}` · merge_state: `{merge_state}`",
        f"- branch: `{head_ref}` → `main`",
        f"- URL: {pr.get('html_url')}",
        "",
        "**Body:**",
        body or "(empty)",
        "",
        "**Files:**",
    ]
    for f in files:
        path = f.get("filename", "?")
        adds = f.get("additions", 0)
        dels = f.get("deletions", 0)
        status = f.get("status", "?")
        patch = (f.get("patch") or "")[:800]
        lines.append(
            f"- `{path}` ({status}, +{adds} -{dels})"
            + (f"\n```diff\n{patch}\n```" if patch else "")
        )
    return _safe("\n".join(lines))


async def merge_engine_pr(
    pr_number: int,
    merge_method: str = "merge",
    confirm: bool = False,
) -> str:
    """Merge a orclaw PR. Triggers auto-deploy.

    Use this AFTER the user has explicitly confirmed they want it merged
    (``"merge it"``, ``"adelante"``, ``"go"``). Refuses unless ``confirm=True``
    — this is a guardrail against accidental merges mid-conversation.

    ``merge_method`` is one of ``"merge"`` (default), ``"squash"``,
    ``"rebase"``. The orclaw repo accepts standard merges.
    """
    if pr_number < 1:
        return f"❌ Invalid PR number: {pr_number}"
    if not confirm:
        return (
            f"⚠️ Refusing to merge PR #{pr_number} without confirm=True. "
            "Call again with confirm=True after the user has explicitly "
            "approved the merge."
        )
    if merge_method not in {"merge", "squash", "rebase"}:
        return f"❌ Invalid merge_method '{merge_method}'. Use merge|squash|rebase."

    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    from orclaw.github_repo_ops import merge_pull_request

    try:
        result = await merge_pull_request(
            token=settings.github.token,
            repo=_ENGINE_REPO_SLUG,
            pr_number=pr_number,
            merge_method=merge_method,
        )
    except Exception as e:
        return f"❌ Failed to merge PR: {e}"

    sha = result.get("sha", "?")
    return (
        f"✓ Merged PR #{pr_number} (method: `{merge_method}`)\n\n"
        f"- merge commit: `{sha[:12] if isinstance(sha, str) else sha}`\n"
        f"- auto-deploy workflow will trigger in seconds\n"
        f"- Telegram alert will arrive when deploy completes (~30s typical)\n"
        f"- to monitor: `get_recent_logs(unit='orclaw-orchestrator')` or check "
        f"GitHub Actions"
    )


# =========================================================================
# Internal event log (queryable from Claude)
# =========================================================================


def query_events(
    minutes: int = 60,
    level: str | None = None,
    event_pattern: str | None = None,
    limit: int = 30,
) -> str:
    """Inspect the engine's internal structured event log.

    Use this when the user is troubleshooting ("what happened around
    23:00 yesterday?", "show me every reviewer dispatch today",
    "any errors in the last hour?"). Returns a markdown table sorted
    most-recent first.

    Parameters
    ----------
    minutes:
        Look-back window in minutes (default 60, max 7 days = 10080).
    level:
        Optional filter: 'debug' | 'info' | 'warning' | 'error' | 'critical'.
    event_pattern:
        Optional SQL LIKE pattern on the event name. Examples:
        ``'%dispatch%'`` matches implementer/reviewer dispatches,
        ``'pollback_%'`` matches every pollback phase.
    limit:
        Max rows to return (default 30, max 200).
    """
    from datetime import UTC, datetime, timedelta

    from orclaw.event_store import query_events as _query

    if minutes < 1 or minutes > 10080:
        return "❌ `minutes` must be between 1 and 10080 (7 days)."
    if limit < 1 or limit > 200:
        return "❌ `limit` must be between 1 and 200."
    if level and level.lower() not in {
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    }:
        return f"❌ Invalid level '{level}'. Use debug|info|warning|error|critical."

    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        return f"❌ Config error: {e}"

    rows = _query(
        settings.paths.db_path,
        since=datetime.now(UTC) - timedelta(minutes=minutes),
        level=level,
        event_pattern=event_pattern,
        limit=limit,
    )
    if not rows:
        return "(no events match)"

    out = ["| ts | level | event | module | attrs |", "|---|---|---|---|---|"]
    for r in rows:
        attrs_str = ", ".join(f"`{k}`={v!r}" for k, v in (r["attrs"] or {}).items())
        if len(attrs_str) > 200:
            attrs_str = attrs_str[:200] + "…"
        out.append(
            f"| {r['ts']} | {r['level']} | `{r['event']}` | {r['module'] or '—'} | {attrs_str} |"
        )
    return _safe("\n".join(out))


# =========================================================================
# Tool registry — used by the MCP server module to register them all.
# =========================================================================


def all_tools() -> dict[str, object]:
    """Return name → callable for every tool. Helps tests + introspection."""
    return {
        # read-only
        "get_status": get_status,
        "get_decision_preview": get_decision_preview,
        "get_recent_logs": get_recent_logs,
        "query_runs": query_runs,
        "query_batches": query_batches,
        "get_summary": get_summary,
        "doctor": doctor,
        "query_events": query_events,
        "view_engine_pr": view_engine_pr,
        # actions
        "pause_orchestrator": pause_orchestrator,
        "propose_engine_change": propose_engine_change,
        "merge_engine_pr": merge_engine_pr,
        "resume_orchestrator": resume_orchestrator,
        "force_tick": force_tick,
        "run_planner": run_planner,
        "skip_issue": skip_issue,
        "require_human_review": require_human_review,
        "force_review": force_review,
    }


__all__ = [*all_tools().keys(), "all_tools"]

"""Command-line interface.

Top-level commands:

Sprint 1 (foundations):
- ``orclaw version`` — print version + git revision (when available)
- ``orclaw config show`` — print the resolved settings (no secrets)
- ``orclaw db init`` — create the SQLite database and apply the schema
- ``orclaw status`` — print the orchestrator status

Sprint 2 (planner + orchestrator skeleton):
- ``orclaw planner run`` — fetch issues, compute layers, persist
- ``orclaw planner show`` — print the current layers from SQLite
- ``orclaw batch show`` — print what the orchestrator would dispatch now
- ``orclaw pause`` / ``resume`` — toggle the orchestrator pause flag

Sprint 3 (implementer dispatcher):
- ``orclaw orchestrator tick`` — preview the next decision (dry-run)
- ``orclaw orchestrator tick --apply`` — post @claude comments + label

Sprint 4 (reviewer dispatcher):
- ``orchestrator tick`` now also detects open PRs needing review and, in
  apply mode, posts ``@claude review`` on them with priority over impl.

Sprint 5 (verdict poll-back):
- Apply-mode ticks first reconcile reviewer verdicts (parses ``review:*``
  labels into ``runs`` + ``reviews`` rows) and mark batches merged when
  their PR merges. The DB is now eventually consistent with GitHub.

Sprint 6 (observability):
- Apply-mode ticks now ping Healthchecks.io and emit Telegram alerts on
  saturation / unexpected error (de-duped via engine_state).
- ``orclaw summary daily|weekly`` builds a digest and posts to
  Telegram (or prints to stdout when ``--no-telegram``).

Sprint 7 (deploy):
- ``orclaw doctor`` — diagnostic health-check (config + DB +
  GitHub + notifications). Used by provision.sh and as a pre-deploy gate.
- Systemd units updated to ``Type=oneshot`` + ``.timer`` model so each
  tick is a fresh process. ``infra/scripts/provision.sh`` updated.
- ``docs/deploy-quickstart.md`` — 10-minute Oracle Cloud walkthrough.

Sprint 8 (specialist):
- ``orclaw specialist serve [--transport=stdio|http]`` — MCP
  server exposing 14 operational tools (status, query, pause/resume,
  force_tick, skip_issue, …) so Claude (Code or claude.ai) can operate
  the engine via natural language.
- See ``docs/specialist-mcp.md`` for local + Cloudflare Tunnel setup.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from orclaw import __version__
from orclaw.batch_planner import run_planner
from orclaw.config import load_settings
from orclaw.db import apply_migrations, backfill_repo, connect, init_db
from orclaw.exceptions import OrclawError
from orclaw.logging import configure_logging, get_logger
from orclaw.models import BatchStatus
from orclaw.notifications import post_telegram
from orclaw.orchestrator import (
    describe_next_action,
    load_state,
    orchestrator_tick,
    set_paused,
)
from orclaw.summary import build_summary, format_summary_telegram

console = Console()
err_console = Console(stderr=True, style="red")
log = get_logger(__name__)


# --- Decorators ------------------------------------------------------------


def _common_options(func):  # type: ignore[no-untyped-def]
    """Options shared by every command (logging verbosity, config dir)."""
    func = click.option(
        "--log-level",
        type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
        default=None,
        help="Override ORCLAW_LOG_LEVEL.",
    )(func)
    return func


# --- Group -----------------------------------------------------------------


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="orclaw — multi-agent engineering pipeline for ${TARGET_REPO}.",
)
@click.version_option(version=__version__, prog_name="orclaw")
def cli() -> None:
    pass


# --- version (extra info beyond click) -------------------------------------


@cli.command()
def version() -> None:
    """Print the version and (when available) the git revision."""
    rev = _git_revision()
    if rev:
        console.print(f"orclaw {__version__} (rev {rev})")
    else:
        console.print(f"orclaw {__version__}")


# --- config ---------------------------------------------------------------


@cli.group()
def config() -> None:
    """Inspect engine configuration."""


@config.command("show")
@_common_options
def config_show(log_level: str | None) -> None:
    """Print the resolved settings (no secrets)."""
    configure_logging(level=log_level or "WARNING")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    table = Table(title="Resolved settings", show_header=True, header_style="bold")
    table.add_column("Key")
    table.add_column("Value")

    _add_section(table, "github.repo", settings.github.repo)
    _add_section(table, "github.poll_interval_seconds", settings.github.poll_interval_seconds)
    _add_section(table, "github.token", "•••• set" if settings.github.token else "(not set)")

    _add_section(table, "concurrency.max_in_flight", settings.concurrency.max_in_flight)
    _add_section(table, "concurrency.default_in_flight", settings.concurrency.default_in_flight)

    _add_section(
        table,
        "backoff.saturation_threshold_failures",
        settings.backoff.saturation_threshold_failures,
    )
    _add_section(
        table, "backoff.saturation_window_minutes", settings.backoff.saturation_window_minutes
    )
    _add_section(
        table, "backoff.saturation_cooldown_minutes", settings.backoff.saturation_cooldown_minutes
    )

    _add_section(table, "paths.data_dir", str(settings.paths.data_dir))
    _add_section(table, "paths.mirror_dir", str(settings.paths.mirror_dir))
    _add_section(table, "paths.db_path", str(settings.paths.db_path))

    _add_section(
        table,
        "notifications.telegram_enabled",
        "yes" if settings.notifications.telegram_enabled else "no",
    )
    _add_section(table, "log_level", settings.log_level)

    console.print(table)


def _add_section(table: Table, key: str, value: object) -> None:
    table.add_row(key, str(value))


# --- db -------------------------------------------------------------------


@cli.group()
def db() -> None:
    """Database operations."""


@db.command("init")
@_common_options
def db_init(log_level: str | None) -> None:
    """Create the SQLite database and apply the schema."""
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=False)
        init_db(settings.paths.db_path)
    except OrclawError as e:
        err_console.print(f"DB init failed: {e}")
        sys.exit(1)
    console.print(f"[green]✓[/green] Initialised database at {settings.paths.db_path}")


@db.command("migrate")
@click.option(
    "--backfill-repo/--no-backfill-repo",
    "do_backfill",
    default=True,
    help="Stamp legacy rows (repo='') with ORCLAW_GITHUB_REPO. On by default.",
)
@_common_options
def db_migrate(do_backfill: bool, log_level: str | None) -> None:
    """Apply additive migrations to an existing database (idempotent).

    Safe to run on every deploy: adds the multi-repo ``repo`` column and
    indexes if missing, then (unless --no-backfill-repo) tags existing
    single-repo rows with the configured target repo.
    """
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=False)
        db_path = settings.paths.db_path
        if not db_path.is_file():
            err_console.print(f"Database not found at {db_path}. Run [bold]orclaw db init[/bold] first.")
            sys.exit(1)
        with connect(db_path) as conn:
            apply_migrations(conn)
            rows = backfill_repo(conn, settings.github.repo) if do_backfill else 0
    except OrclawError as e:
        err_console.print(f"DB migrate failed: {e}")
        sys.exit(1)
    msg = f"[green]✓[/green] Migrations applied to {db_path}"
    if do_backfill:
        msg += f" — backfilled {rows} legacy row(s) → repo='{settings.github.repo or '(unset)'}'"
    console.print(msg)


# --- status ---------------------------------------------------------------


@cli.command("status")
@_common_options
def status_cmd(log_level: str | None) -> None:
    """Print the orchestrator status.

    Sprint 1: skeleton — reads the DB and shows counts. Sprint 2+ adds the
    live in-flight + quota observation panes from ``docs/dashboard-and-integrations.md``.
    """
    configure_logging(level=log_level or "WARNING")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    db_path = settings.paths.db_path
    if not db_path.is_file():
        err_console.print(
            f"Database not found at {db_path}. Run [bold]orclaw db init[/bold] first."
        )
        sys.exit(2)

    with connect(db_path, read_only=True) as conn:
        batch_counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM batches GROUP BY status"
        ).fetchall()
        run_counts = conn.execute(
            "SELECT agent, status, COUNT(*) AS n FROM runs GROUP BY agent, status"
        ).fetchall()
        active_runs = conn.execute(
            "SELECT id, agent, issue_number FROM runs "
            "WHERE status IN ('queued', 'running') ORDER BY started_at DESC"
        ).fetchall()

    table = Table(title="Engine status", show_header=True, header_style="bold")
    table.add_column("Section")
    table.add_column("Value")

    if batch_counts:
        for row in batch_counts:
            table.add_row(f"batches.{row['status']}", str(row["n"]))
    else:
        table.add_row("batches", "[dim]empty[/dim]")

    if run_counts:
        for row in run_counts:
            table.add_row(f"runs.{row['agent']}.{row['status']}", str(row["n"]))
    else:
        table.add_row("runs", "[dim]empty[/dim]")

    table.add_row("active runs", str(len(active_runs)))
    console.print(table)

    if active_runs:
        active = Table(title="In flight", show_header=True, header_style="bold")
        active.add_column("Run id")
        active.add_column("Agent")
        active.add_column("Issue")
        for row in active_runs:
            active.add_row(row["id"][:12], row["agent"], str(row["issue_number"] or "-"))
        console.print(active)


# --- planner --------------------------------------------------------------


@cli.group()
def planner() -> None:
    """Batch planner: dependency graph → parallel layers."""


@planner.command("run")
@_common_options
def planner_run(log_level: str | None) -> None:
    """Fetch issues from GitHub, compute layers, persist to SQLite."""
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    if not settings.paths.db_path.is_file():
        err_console.print(
            f"Database not found at {settings.paths.db_path}. "
            f"Run [bold]orclaw db init[/bold] first."
        )
        sys.exit(2)

    try:
        result = asyncio.run(run_planner(settings))
    except OrclawError as e:
        err_console.print(f"Planner failed: {e}")
        sys.exit(1)

    table = Table(title="Planner run", show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("layers", str(result.layers_count))
    table.add_row("issues scanned", str(result.issues_scanned))
    table.add_row("issues in plan", str(result.issues_planned))
    table.add_row("new pending rows", str(result.issues_pending_added))
    table.add_row("already tracked", str(result.issues_already_tracked))
    console.print(table)


@planner.command("show")
@_common_options
def planner_show(log_level: str | None) -> None:
    """Print the current layers stored in SQLite."""
    configure_logging(level=log_level or "WARNING")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    if not settings.paths.db_path.is_file():
        err_console.print("Database not found. Run [bold]orclaw db init[/bold] first.")
        sys.exit(2)

    with connect(settings.paths.db_path, read_only=True) as conn:
        state = load_state(conn)

    if not state.batches.layers:
        console.print(
            "[dim]No batches in DB. Run [bold]orclaw planner run[/bold] first.[/dim]"
        )
        return

    table = Table(title="Layered batches", show_header=True, header_style="bold")
    table.add_column("Layer")
    table.add_column("Issues")
    table.add_column("Status mix")
    for layer_idx, issues in enumerate(state.batches.layers):
        statuses = [state.batches.status_by_issue.get(n) for n in issues]
        mix = ", ".join(
            f"{s.value}={statuses.count(s)}" for s in BatchStatus if statuses.count(s) > 0
        )
        table.add_row(
            str(layer_idx),
            ", ".join(f"#{n}" for n in issues),
            mix or "[dim]none[/dim]",
        )
    console.print(table)


# --- batch ----------------------------------------------------------------


@cli.group()
def batch() -> None:
    """Inspect the current and next executable batches."""


@batch.command("show")
@_common_options
def batch_show(log_level: str | None) -> None:
    """Print what the orchestrator would dispatch right now (dry-run)."""
    configure_logging(level=log_level or "WARNING")
    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    try:
        decision = asyncio.run(orchestrator_tick(settings))
    except OrclawError as e:
        err_console.print(f"Orchestrator tick failed: {e}")
        sys.exit(1)

    table = Table(title="Next decision", show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row(
        "verdict",
        "[yellow]idle[/yellow]" if decision.is_idle else "[green]dispatch[/green]",
    )
    table.add_row("layer", str(decision.layer_index))
    table.add_row(
        "issues",
        ", ".join(f"#{n}" for n in decision.issues_to_dispatch) or "[dim]none[/dim]",
    )
    table.add_row("reason", decision.reason)
    table.add_row("active runs", str(decision.state.active_run_count))
    table.add_row("paused", "yes" if decision.state.paused else "no")
    console.print(table)


# --- orchestrator ---------------------------------------------------------


@cli.group()
def orchestrator() -> None:
    """Orchestrator control."""


@orchestrator.command("tick")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Actually post @claude comments. Without this flag the tick is dry-run.",
)
@_common_options
def orchestrator_tick_cmd(apply_changes: bool, log_level: str | None) -> None:
    """Compute one orchestrator decision (and optionally dispatch it)."""
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=True)
        decision = asyncio.run(orchestrator_tick(settings, apply=apply_changes))
    except OrclawError as e:
        err_console.print(f"Orchestrator tick failed: {e}")
        sys.exit(1)

    label = "IDLE" if decision.is_idle else ("DISPATCH" if apply_changes else "PREVIEW")
    console.print(
        f"[bold]{label}[/bold]: layer={decision.layer_index} "
        f"issues={decision.issues_to_dispatch} "
        f"reason={decision.reason!r} "
        f"run_id={decision.orchestrator_run_id}"
    )
    console.print(f"[cyan]Next action:[/cyan] {describe_next_action(decision, settings)}")
    if not decision.pollback.is_noop:
        if decision.pollback.reviews_completed:
            console.print("[magenta]Reviews completed (verdicts ingested):[/magenta]")
            for pr_num, verdict in decision.pollback.reviews_completed:
                console.print(f"  PR #{pr_num} → {verdict}")
        if decision.pollback.batches_merged:
            console.print("[magenta]Batches marked merged:[/magenta]")
            for issue_num, pr_num in decision.pollback.batches_merged:
                console.print(f"  #{issue_num} via PR #{pr_num}")
    if decision.dispatched:
        console.print("[green]Implementer dispatched:[/green]")
        for d in decision.dispatched:
            console.print(f"  #{d.issue_number} → run {d.run_id} (comment {d.comment_id})")
    if decision.skipped:
        console.print("[yellow]Implementer skipped:[/yellow]")
        for issue_num, reason in decision.skipped:
            console.print(f"  #{issue_num}: {reason}")
    if decision.prs_to_review:
        console.print(f"[cyan]PRs needing review:[/cyan] {decision.prs_to_review}")
    if decision.reviewers_dispatched:
        console.print("[green]Reviewer dispatched:[/green]")
        for r in decision.reviewers_dispatched:
            console.print(f"  PR #{r.pr_number} → run {r.run_id} (comment {r.comment_id})")
    if decision.reviewers_skipped:
        console.print("[yellow]Reviewer skipped:[/yellow]")
        for pr_num, reason in decision.reviewers_skipped:
            console.print(f"  PR #{pr_num}: {reason}")


@cli.command()
@_common_options
def pause(log_level: str | None) -> None:
    """Pause the orchestrator (prevents new dispatches, in-flight work continues)."""
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)
    with connect(settings.paths.db_path) as conn:
        set_paused(conn, True)
    console.print("[yellow]⏸  Orchestrator paused.[/yellow]")


@cli.command()
@_common_options
def resume(log_level: str | None) -> None:
    """Resume the orchestrator."""
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)
    with connect(settings.paths.db_path) as conn:
        set_paused(conn, False)
    console.print("[green]▶  Orchestrator resumed.[/green]")


# --- notify (Telegram / etc. from shell scripts) -------------------------


@cli.group()
def notify() -> None:
    """Push a notification from a shell script (used by deploy scripts)."""


@notify.command("telegram")
@click.argument("message", required=True)
@click.option(
    "--parse-mode",
    type=click.Choice(["HTML", "Markdown", "MarkdownV2"], case_sensitive=False),
    default="HTML",
    help="Telegram parse_mode (default HTML — safest with code blocks).",
)
@_common_options
def notify_telegram(message: str, parse_mode: str, log_level: str | None) -> None:
    """Send MESSAGE to Telegram. No-op if TELEGRAM_* not configured.

    Designed for shell-script callers like orclaw-deploy.sh. Exit code
    is always 0 when settings load — Telegram outages must not break
    the calling script.
    """
    configure_logging(level=log_level or "WARNING")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    sent = asyncio.run(post_telegram(settings.notifications, message, parse_mode=parse_mode))
    if not sent and settings.notifications.telegram_enabled:
        err_console.print("(failed to send — see logs)")


# --- telegram bot (long-running, bidirectional) --------------------------


@cli.command("telegram-bot")
@_common_options
def telegram_bot_cmd(log_level: str | None) -> None:
    """Run the bidirectional Telegram bot (long-polling).

    Long-running process — designed to be supervised by
    ``orclaw-telegram-bot.service``. Exposes /status /pause /resume
    /skip /tick /quota /help commands to the chat ID configured in
    ORCLAW_TELEGRAM_CHAT_ID. Refuses to start without both bot token
    and chat ID set.
    """
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)
    if not settings.notifications.telegram_enabled:
        err_console.print(
            "[red]Telegram bot requires ORCLAW_TELEGRAM_BOT_TOKEN and "
            "ORCLAW_TELEGRAM_CHAT_ID to be set.[/red]"
        )
        sys.exit(1)
    from orclaw.telegram_bot import run_bot

    asyncio.run(run_bot(settings))


# --- demo-seed (populate local DB with synthetic data) -------------------


@cli.command("demo-seed")
@_common_options
def demo_seed_cmd(log_level: str | None) -> None:
    """Populate the local SQLite with synthetic batches + runs + events.

    Useful for exploring the dashboard before pointing it at a real
    target repo. Re-running clobbers the previous seed. The DB path
    comes from ``ORCLAW_DB_PATH`` (defaults to
    ``/var/lib/orclaw/orclaw.db``). For a throwaway:

        export ORCLAW_DB_PATH=$(mktemp -t orclaw-demo.XXXXXX.db)
        orclaw demo-seed
        orclaw dashboard serve --port 8888
    """
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)
    from orclaw.demo_seed import run as run_demo_seed

    run_demo_seed(settings.paths.db_path)
    console.print(f"[green]✓ demo data seeded to {settings.paths.db_path}[/green]")


# --- events (internal log retention + ad-hoc query) -----------------------


@cli.group()
def events() -> None:
    """Internal structured-event log management."""


@events.command("purge")
@click.option(
    "--older-than",
    "days",
    type=int,
    default=30,
    show_default=True,
    help="Delete events older than this many days.",
)
@_common_options
def events_purge(days: int, log_level: str | None) -> None:
    """Delete old event rows. Designed for a weekly systemd timer."""
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    from orclaw.event_store import count_events, purge_older_than

    before = count_events(settings.paths.db_path)
    deleted = purge_older_than(settings.paths.db_path, days=days)
    after = count_events(settings.paths.db_path)
    console.print(
        f"events: {before} → {after} ([yellow]-{deleted}[/yellow] purged, older than {days} days)"
    )


@events.command("count")
@_common_options
def events_count(log_level: str | None) -> None:
    """Print total rows in the events table."""
    configure_logging(level=log_level or "WARNING")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    from orclaw.event_store import count_events

    console.print(str(count_events(settings.paths.db_path)))


# --- runner (self-hosted GH Actions monitor) -----------------------------


@cli.group()
def runner() -> None:
    """Self-hosted GitHub Actions runner — health + activity monitor."""


@runner.command("monitor")
@_common_options
def runner_monitor_cmd(log_level: str | None) -> None:
    """One pass: check systemd health, flip ORCLAW_RUNNER var, scan workflows.

    Designed to run from the orclaw-runner-monitor.timer (every 2 min).
    Idempotent; alerts on transitions only.
    """
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=True)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    from orclaw.runner_monitor import run_monitor

    health, activity = asyncio.run(run_monitor(settings))

    console.print(
        f"runner.systemd_active=[{'green' if health.systemd_active else 'red'}]"
        f"{health.systemd_active}[/{'green' if health.systemd_active else 'red'}] "
        f"transitioned={health.transitioned} "
        f"ORCLAW_RUNNER={health.repo_variable_value!r} "
        f"suspicious_flagged={len(activity.flagged)}"
    )


# --- dashboard (FastAPI web UI) ------------------------------------------


@cli.group()
def dashboard() -> None:
    """Web dashboard — live status, controls, prompt editor."""


@dashboard.command("serve")
@click.option("--host", default="127.0.0.1", help="Bind address (loopback for tunnel).")
@click.option("--port", type=int, default=8766, help="HTTP port.")
@_common_options
def dashboard_serve(host: str, port: int, log_level: str | None) -> None:
    """Serve the dashboard. Designed to live behind Cloudflare Tunnel + Access."""
    configure_logging(level=log_level or "INFO")
    try:
        from orclaw.dashboard.app import app as _app
    except ImportError as e:
        err_console.print(
            f"Dashboard requires the [dashboard] extras. Install with:\n"
            f"  pip install -e '.[dashboard]'\n\n"
            f"Original error: {e}"
        )
        sys.exit(1)

    import uvicorn

    uvicorn.run(_app, host=host, port=port, log_level="info")


# --- specialist (MCP server) ---------------------------------------------


@cli.group()
def specialist() -> None:
    """Specialist agent — operate the engine via Claude (MCP)."""


@specialist.command("serve")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"], case_sensitive=False),
    default="stdio",
    help="stdio = launched by Claude Code locally; http = long-lived for remote access.",
)
@click.option("--host", default="127.0.0.1", help="HTTP bind address (http transport only).")
@click.option("--port", type=int, default=8765, help="HTTP port (http transport only).")
@_common_options
def specialist_serve(
    transport: str,
    host: str,
    port: int,
    log_level: str | None,
) -> None:
    """Start the MCP server exposing the specialist tools.

    For local use (Claude Code): leave as --transport=stdio. The Claude
    Code config will launch this command as a subprocess.

    For remote use (claude.ai or Claude Code from a phone): use
    --transport=http behind a reverse proxy that handles auth
    (Cloudflare Tunnel + Zero Trust).
    """
    configure_logging(level=log_level or "INFO")
    try:
        from orclaw.specialist.server import serve as _serve
    except ImportError as e:
        err_console.print(
            f"Specialist requires the 'mcp' package. Install with:\n"
            f"  pip install -e '.[specialist]'\n\n"
            f"Original error: {e}"
        )
        sys.exit(1)

    _serve(transport=transport, host=host, port=port)  # type: ignore[arg-type]


# --- summary ---------------------------------------------------------------


@cli.command("doctor")
@_common_options
def doctor(log_level: str | None) -> None:
    """Run diagnostic checks: config, DB, GitHub access, notifications.

    Exits 0 on a fully healthy engine. Non-zero on the first failed check
    — useful as a pre-deploy gate or a healthcheck script for systemd.
    """
    configure_logging(level=log_level or "WARNING")
    checks: list[tuple[str, bool, str]] = []

    # --- 1. Config loads (with secrets) ---
    try:
        settings = load_settings(require_secrets=True)
        checks.append(("config loads with secrets", True, "ok"))
    except OrclawError as e:
        checks.append(("config loads with secrets", False, str(e)))
        _render_doctor(checks)
        sys.exit(1)

    # --- 2. DB exists + schema OK ---
    db_path = settings.paths.db_path
    if not db_path.is_file():
        checks.append(("database file exists", False, f"missing at {db_path}"))
    else:
        try:
            with connect(db_path, read_only=True) as conn:
                # Probe each expected table.
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            expected = {"batches", "runs", "reviews", "engine_state", "token_ledger"}
            missing = expected - tables
            if missing:
                checks.append(
                    ("schema has all expected tables", False, f"missing: {sorted(missing)}")
                )
            else:
                checks.append(("schema has all expected tables", True, "ok"))
        except Exception as e:
            checks.append(("schema readable", False, str(e)))

    # --- 3. GitHub token works ---
    async def _check_github() -> tuple[bool, str]:
        from orclaw.github_client import GitHubAPIConfig, GitHubClient

        try:
            cfg = GitHubAPIConfig(repo=settings.github.repo, token=settings.github.token)
            async with GitHubClient(cfg) as gh:
                # Just hit any cheap endpoint — listing 1 open PR is fine.
                await gh.list_prs(state="open", base="develop")
            return True, "ok"
        except Exception as e:
            return False, str(e)

    ok, msg = asyncio.run(_check_github())
    checks.append((f"github access (repo={settings.github.repo})", ok, msg))

    # --- 4. Notifications (warning, not failure) ---
    if settings.notifications.telegram_enabled:
        checks.append(("telegram configured", True, "ok"))
    else:
        checks.append(
            ("telegram configured", True, "(disabled — set TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID)")
        )
    if settings.notifications.healthchecks_orchestrator_url:
        checks.append(("healthchecks orchestrator URL set", True, "ok"))
    else:
        checks.append(
            (
                "healthchecks orchestrator URL set",
                True,
                "(disabled — set HEALTHCHECKS_ORCHESTRATOR_URL)",
            )
        )

    _render_doctor(checks)
    if any(not ok for _, ok, _ in checks):
        sys.exit(1)


def _render_doctor(checks: list[tuple[str, bool, str]]) -> None:
    """Pretty-print the doctor results as a rich table."""
    table = Table(title="orclaw doctor", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for name, ok, detail in checks:
        status = "[green]✓ ok[/green]" if ok else "[red]✗ fail[/red]"
        table.add_row(name, status, detail)
    console.print(table)


@cli.group()
def summary() -> None:
    """Build engine activity digests (daily / weekly)."""


@summary.command("daily")
@click.option(
    "--no-telegram",
    "no_telegram",
    is_flag=True,
    default=False,
    help="Print to stdout only, skip Telegram even when configured.",
)
@_common_options
def summary_daily(no_telegram: bool, log_level: str | None) -> None:
    """Build the last-24h digest and (optionally) post it to Telegram."""
    _summary_cmd(window=timedelta(days=1), no_telegram=no_telegram, log_level=log_level)


@summary.command("weekly")
@click.option(
    "--no-telegram",
    "no_telegram",
    is_flag=True,
    default=False,
    help="Print to stdout only, skip Telegram even when configured.",
)
@_common_options
def summary_weekly(no_telegram: bool, log_level: str | None) -> None:
    """Build the last-7d digest and (optionally) post it to Telegram."""
    _summary_cmd(window=timedelta(days=7), no_telegram=no_telegram, log_level=log_level)


def _summary_cmd(*, window: timedelta, no_telegram: bool, log_level: str | None) -> None:
    configure_logging(level=log_level or "INFO")
    try:
        settings = load_settings(require_secrets=False)
    except OrclawError as e:
        err_console.print(f"Config error: {e}")
        sys.exit(1)

    with connect(settings.paths.db_path, read_only=True) as conn:
        snapshot = build_summary(conn, window=window)

    rendered = format_summary_telegram(snapshot)
    console.print(rendered)

    if no_telegram:
        return
    if not settings.notifications.telegram_enabled:
        console.print(
            "[dim](Telegram not configured — skipped post. Use --no-telegram to silence this.)[/dim]"
        )
        return

    sent = asyncio.run(post_telegram(settings.notifications, rendered))
    if sent:
        console.print("[green]✓[/green] Posted to Telegram.")
    else:
        err_console.print("Failed to post to Telegram (see logs).")


# --- Helpers --------------------------------------------------------------


def _git_revision() -> str | None:
    """Return short git rev if invoked from the repo, else None."""
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent.parent,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main() -> None:  # pragma: no cover — covered by integration runs
    try:
        cli(standalone_mode=False)
    except click.exceptions.Abort:
        sys.exit(130)
    except click.exceptions.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
    except OrclawError as e:
        err_console.print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()

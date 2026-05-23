"""Bidirectional Telegram bot — run the orchestrator from your phone.

Long-polls Telegram's getUpdates so we don't need to expose a webhook
endpoint (the orchestrator VM stays firewalled; only Cloudflare Tunnel
exposes the dashboard). All commands restrict themselves to the chat
ID in ``ORCLAW_TELEGRAM_CHAT_ID`` — strangers who DM the bot get a
silent rejection.

Commands
--------
``/start``     One-time hello + command list.
``/help``      Same as /start.
``/status``    Inline summary: paused?, in-flight, batch counts.
``/pause``     Sets the orchestrator pause flag.
``/resume``    Clears the pause flag.
``/skip N``    Marks issue #N as skipped (won't be picked again).
``/tick``      Forces a tick now (apply mode).
``/quota``     Pro-plan 5h budget estimate.

Why long-polling and not webhooks?
----------------------------------
The dashboard is the only public surface (behind Cloudflare Access).
Adding a Telegram webhook endpoint would require a second public route
and HMAC validation. Long-polling keeps the auth surface minimal — the
bot uses an outbound HTTPS connection to api.telegram.org. The only
secret involved is the bot token.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from orclaw.db import connect
from orclaw.logging import get_logger
from orclaw.orchestrator import (
    effective_max_in_flight,
    is_paused,
    orchestrator_tick,
    set_paused,
)

if TYPE_CHECKING:
    from orclaw.config import Settings

log = get_logger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 25  # seconds — Telegram allows up to 50
_HTTP_TIMEOUT = 30.0  # must exceed _POLL_TIMEOUT


# --- Outbound ---------------------------------------------------------------


async def _send(token: str, chat_id: str, text: str) -> None:
    """Best-effort sendMessage — never raises."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            await client.post(
                _TG_API.format(token=token, method="sendMessage"),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        log.warning("telegram_bot_send_failed", error=str(e))


# --- Command handlers -------------------------------------------------------


async def _cmd_status(settings: Settings) -> str:
    with connect(settings.paths.db_path, read_only=True) as conn:
        paused = is_paused(conn)
        cap = effective_max_in_flight(
            conn, settings_default=settings.concurrency.max_in_flight
        )
        in_flight_row = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued', 'running')"
        ).fetchone()
        in_flight = int(in_flight_row["n"]) if in_flight_row else 0
        batch_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM batches GROUP BY status"
        ).fetchall()
    batch_counts = {r["status"]: int(r["n"]) for r in batch_rows}
    state = "⏸ PAUSED" if paused else "▶ ACTIVE"
    lines = [
        f"<b>Orclaw</b> · {state}",
        f"in-flight: {in_flight} / {cap}",
        f"repo: <code>{settings.github.repo}</code>",
        "",
        "<b>batches</b>:",
    ]
    for status in ("pending", "in_progress", "merged", "failed", "skipped"):
        lines.append(f"  {status}: {batch_counts.get(status, 0)}")
    return "\n".join(lines)


async def _cmd_pause(settings: Settings) -> str:
    with connect(settings.paths.db_path) as conn:
        set_paused(conn, True)
    return "⏸ paused — no new dispatches until /resume"


async def _cmd_resume(settings: Settings) -> str:
    with connect(settings.paths.db_path) as conn:
        set_paused(conn, False)
    return "▶ resumed"


async def _cmd_skip(settings: Settings, args: list[str]) -> str:
    if not args or not args[0].lstrip("#").isdigit():
        return "usage: /skip <issue_number>"
    issue_num = int(args[0].lstrip("#"))
    # Lazy import to avoid pulling specialist deps when the bot starts.
    from orclaw.specialist.tools import skip_issue

    try:
        out = await skip_issue(issue_number=issue_num, reason="telegram /skip")
        return f"✓ issue #{issue_num} skipped: {out}"
    except Exception as e:
        return f"❌ skip failed: {e}"


async def _cmd_tick(settings: Settings) -> str:
    try:
        decision = await orchestrator_tick(settings, apply=True, notify=False)
    except Exception as e:
        return f"❌ tick failed: {e}"
    return (
        f"✓ tick ran\n"
        f"  layer: {decision.layer_index}\n"
        f"  dispatched: {len(decision.dispatched)}\n"
        f"  reviewers: {len(decision.reviewers_dispatched)}\n"
        f"  reason: {decision.reason}"
    )


async def _cmd_quota(settings: Settings) -> str:
    from datetime import UTC, datetime, timedelta

    since = datetime.now(UTC) - timedelta(hours=5)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    with connect(settings.paths.db_path, read_only=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runs "
            "WHERE started_at >= ? AND agent IN ('implementer', 'reviewer')",
            (since_str,),
        ).fetchone()
    used = int(row["n"]) if row else 0
    budget = 45
    pct = round(100.0 * used / budget, 1)
    return (
        f"<b>Pro-plan 5h budget</b> (lower bound)\n"
        f"  dispatches: {used} / {budget}\n"
        f"  used: {pct}%\n"
        f"  remaining: ~{max(0, budget - used)}\n"
        f"<i>Specialist chat from claude.ai isn't counted.</i>"
    )


def _help_text() -> str:
    return (
        "<b>Orclaw bot</b>\n\n"
        "/status — engine summary\n"
        "/pause — stop new dispatches\n"
        "/resume — resume dispatches\n"
        "/skip &lt;n&gt; — skip issue #n\n"
        "/tick — force a tick now\n"
        "/quota — 5h budget estimate\n"
        "/help — this message"
    )


_HANDLERS: dict[str, Any] = {
    "/start": lambda s, a: asyncio.sleep(0, result=_help_text()),
    "/help": lambda s, a: asyncio.sleep(0, result=_help_text()),
    "/status": lambda s, a: _cmd_status(s),
    "/pause": lambda s, a: _cmd_pause(s),
    "/resume": lambda s, a: _cmd_resume(s),
    "/skip": lambda s, a: _cmd_skip(s, a),
    "/tick": lambda s, a: _cmd_tick(s),
    "/quota": lambda s, a: _cmd_quota(s),
}


async def _dispatch(settings: Settings, text: str) -> str:
    """Map an incoming message to a handler. Unknown → help."""
    parts = text.strip().split()
    if not parts:
        return _help_text()
    cmd = parts[0].split("@", 1)[0].lower()  # strip /cmd@botname suffix
    args = parts[1:]
    handler = _HANDLERS.get(cmd)
    if handler is None:
        return f"unknown command: <code>{cmd}</code>\n\n{_help_text()}"
    return await handler(settings, args)


# --- Long-polling loop ------------------------------------------------------


async def run_bot(settings: Settings) -> None:
    """Long-poll Telegram forever, dispatching commands.

    Refuses to start unless both ``telegram_bot_token`` and
    ``telegram_chat_id`` are set — there's no useful insecure default.
    """
    notif = settings.notifications
    if not notif.telegram_enabled:
        raise RuntimeError(
            "telegram bot requires both ORCLAW_TELEGRAM_BOT_TOKEN and "
            "ORCLAW_TELEGRAM_CHAT_ID to be set"
        )
    token = notif.telegram_bot_token
    allowed_chat = str(notif.telegram_chat_id)

    log.info("telegram_bot_starting", chat_id=allowed_chat)
    offset = 0
    backoff = 1.0

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        while True:
            try:
                resp = await client.get(
                    _TG_API.format(token=token, method="getUpdates"),
                    params={"timeout": _POLL_TIMEOUT, "offset": offset},
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
                backoff = 1.0  # success → reset
            except Exception as e:
                log.warning("telegram_bot_poll_failed", error=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                if str(chat.get("id")) != allowed_chat:
                    log.warning(
                        "telegram_bot_rejected_chat",
                        from_chat=chat.get("id"),
                        from_user=msg.get("from", {}).get("username"),
                    )
                    continue
                text = msg.get("text", "")
                log.info("telegram_bot_command", text=text[:80])
                try:
                    reply = await _dispatch(settings, text)
                except Exception as e:
                    reply = f"❌ handler crashed: {e}"
                    log.exception("telegram_bot_handler_crash")
                await _send(token, allowed_chat, reply)

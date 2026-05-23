"""Outbound notifications: Telegram + Healthchecks.io.

Both integrations are **opt-in**: if the relevant settings are empty the
helpers return silently. This keeps tests and local development free of
configuration boilerplate.

Failure mode is "best effort": these helpers catch their own HTTP
exceptions and log them as warnings rather than propagating. We never
want a Telegram outage to fail an orchestrator tick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import httpx

from orclaw.logging import get_logger

if TYPE_CHECKING:
    from orclaw.config import NotificationsSettings

log = get_logger(__name__)


#: Telegram API limit per message (4096 chars). We truncate at 4000 to
#: leave headroom for any prefix the caller adds.
_TELEGRAM_MAX_LEN = 4000

#: Generous timeout — outbound notifications can be slow, but we don't
#: want them to hold up a tick longer than necessary.
_HTTP_TIMEOUT_SECONDS = 10.0


# --- Telegram --------------------------------------------------------------


async def post_telegram(
    notifications: NotificationsSettings,
    text: str,
    *,
    parse_mode: Literal["MarkdownV2", "HTML", "Markdown"] | None = "Markdown",
) -> bool:
    """Post a single message to the configured Telegram chat.

    Returns True iff the message was actually sent (settings configured +
    HTTP 200). When disabled or on failure, returns False after logging.
    """
    if not notifications.telegram_enabled:
        return False

    if len(text) > _TELEGRAM_MAX_LEN:
        text = text[: _TELEGRAM_MAX_LEN - 20] + "\n\n…(truncated)"

    url = f"https://api.telegram.org/bot{notifications.telegram_bot_token}/sendMessage"
    payload: dict[str, object] = {
        "chat_id": notifications.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            log.warning(
                "telegram_send_failed",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("telegram_send_error", error=str(e))
        return False


# --- Healthchecks.io -------------------------------------------------------


async def ping_healthchecks(
    url: str,
    *,
    status: Literal["success", "fail", "start"] = "success",
    body: str | None = None,
) -> bool:
    """Ping a Healthchecks.io check URL.

    - ``success`` (default): POST/GET the bare URL → check is healthy.
    - ``fail``: POST to ``{url}/fail`` → check fires the alert channel.
    - ``start``: POST to ``{url}/start`` → only useful for measuring
      run duration; we don't currently use this but support it.

    Returns True on HTTP 200. Empty ``url`` is a no-op (returns False).
    """
    if not url:
        return False

    target = url
    if status == "fail":
        target = f"{url.rstrip('/')}/fail"
    elif status == "start":
        target = f"{url.rstrip('/')}/start"

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            # Healthchecks accepts both GET (pixel-style) and POST. We POST
            # so we can include a body — useful for the tick summary log.
            resp = await client.post(target, content=body or "")
        if resp.status_code != 200:
            log.warning(
                "healthchecks_ping_failed",
                url=target,
                status=resp.status_code,
            )
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("healthchecks_ping_error", error=str(e))
        return False


# --- Convenience helpers used by the orchestrator -------------------------


def format_saturation_alert(
    *,
    active_runs: int,
    max_in_flight: int,
    pending_batches: int,
    prs_awaiting_review: int,
) -> str:
    """Render a human-friendly Telegram message for a saturated tick."""
    return (
        "*⚠️ orclaw saturation*\n\n"
        f"In flight: `{active_runs}/{max_in_flight}` (cap hit)\n"
        f"Pending batches: `{pending_batches}`\n"
        f"PRs awaiting review: `{prs_awaiting_review}`\n\n"
        "No new dispatches this tick. Will retry next tick."
    )


def format_error_alert(*, context: str, error: str) -> str:
    """Render a Telegram alert for an uncaught error during the tick."""
    return f"*🔥 orclaw error in {context}*\n\n```\n{error[:1500]}\n```"

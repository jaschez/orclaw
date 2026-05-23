# Telegram bot

Orclaw includes an optional bidirectional Telegram bot so you can
operate the orchestrator from your phone — pause, resume, force a
tick, skip an issue, check quota — without opening the dashboard.

## Why a bot (and not just push notifications)

Push notifications (which we already have via `post_telegram`) are
fine for "something happened, look at the dashboard". The bot is for
*responding* without leaving the chat — typically you're walking, in
a meeting, or on the couch. The use case is "I just got a saturation
alert, let me bump the cap to 4 from my pocket."

## Setup

1. **Create a bot** via [@BotFather](https://t.me/BotFather) →
   `/newbot` → name it (e.g. `orclaw-myproject-bot`) → grab the
   token.

2. **Get your chat ID** by messaging
   [@userinfobot](https://t.me/userinfobot). It replies with a
   numeric ID. Save it.

3. **Set the secrets** in `/etc/orclaw/secrets.env`:

   ```bash
   ORCLAW_TELEGRAM_BOT_TOKEN=1234567890:AAH...
   ORCLAW_TELEGRAM_CHAT_ID=987654321
   ```

4. **Start the service**:

   ```bash
   sudo systemctl enable --now orclaw-telegram-bot.service
   sudo journalctl -fu orclaw-telegram-bot.service
   ```

5. **DM the bot** `/start` to see the command list.

The bot will *only* respond to messages from the chat ID in
`ORCLAW_TELEGRAM_CHAT_ID`. Strangers who find the bot get silently
ignored — a `telegram_bot_rejected_chat` event is logged.

## Commands

| Command | Effect |
|---|---|
| `/start` · `/help` | Inline command list |
| `/status` | Paused state, in-flight count, batch counts, repo |
| `/pause` | Sets the orchestrator pause flag — no new dispatches |
| `/resume` | Clears the pause flag |
| `/skip <issue>` | Marks issue as `skipped`, won't be picked again |
| `/tick` | Forces a tick now (apply mode), reports verdict |
| `/quota` | 5-hour budget estimate (lower bound — see below) |

All commands write to the SQLite events table for audit, same as
dashboard actions.

## Quota math

`/quota` counts implementer + reviewer dispatches from the `runs`
table in the last 5 hours and reports it against a 45-message budget
(the Pro-plan rolling window). This is a **lower bound**: it doesn't
see specialist conversations from claude.ai (mobile/desktop), which
consume the same budget. Use it as "minimum used", not "exactly used".

## Architecture notes

- **Long-polling**, not webhooks. The VM stays firewalled — only the
  Cloudflare Tunnel exposes the dashboard. Adding a webhook would
  mean a second public route + HMAC validation. Long-polling uses an
  outbound HTTPS connection to api.telegram.org instead.
- **One process per chat ID** — the systemd unit runs a single bot
  worker. If you want to control multiple Orclaw instances from one
  Telegram chat, deploy one bot per instance (different tokens) or
  run a tiny router in front (out of scope here).
- **No third-party deps**. We use `httpx` directly (already a core
  dep), no `python-telegram-bot` library. The Telegram Bot API is
  simple enough that a ~250-line module covers it.
- **Reject by default**. Every message is checked against the
  configured `chat_id` *before* dispatch — even `/help` won't reply
  to strangers.

## Adding commands

`orclaw/telegram_bot.py` has a `_HANDLERS` dict mapping
`/command` → async callable `(settings, args) -> str`. Add yours
there. Keep them idempotent and quick (Telegram drops the connection
after ~50 seconds of no reply).

```python
async def _cmd_redeploy(settings: Settings) -> str:
    # ...
    return "✓ redeployed"

_HANDLERS["/redeploy"] = lambda s, a: _cmd_redeploy(s)
```

Then update `_help_text()` so users discover it.

## Limitations / not yet

- No inline keyboards (Telegram's "buttons under a message") yet —
  every command is text-based.
- No file uploads (e.g., dump the SQLite to chat as a backup).
- No per-user permissions — it's chat-wide.

PRs welcome.

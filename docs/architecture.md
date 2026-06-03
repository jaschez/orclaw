# Architecture

This is a tour of the subsystems and why each one looks the way it does.
For the day-one happy path see [`deploy-quickstart.md`](deploy-quickstart.md);
for the layered planner internals see [`batch-algorithm.md`](batch-algorithm.md).

## 1,000-foot view

Orclaw separates *what to do next* from *actually doing it*. The
orchestrator decides; the doing happens inside GitHub Actions, on a
self-hosted runner you own, paid by your Claude Pro plan.

```
       you write issues
              │
              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  PLANNER         (every 10 minutes)                             │
  │   reads issues, parses "Depends on #N" edges, runs Kahn         │
  │   topological sort, persists layered batches to SQLite.         │
  ├─────────────────────────────────────────────────────────────────┤
  │  ORCHESTRATOR    (webhook-driven; polling every 30s as fallback)│
  │   reviewer pass: open PRs needing review → @claude review       │
  │   implementer pass: next layer + within cap → @claude implement │
  │   single GitHub fetch reused across both passes.                │
  │   cap defaults to 1 — a single Claude task at a time.           │
  ├─────────────────────────────────────────────────────────────────┤
  │  POLLBACK        (top of every orchestrator tick)               │
  │   read review:* labels → mark reviewer runs complete            │
  │   read recently-merged PRs → mark batches merged                │
  ├─────────────────────────────────────────────────────────────────┤
  │  OBSERVABILITY                                                  │
  │   saturation detection → Telegram (de-duped per episode)        │
  │   healthchecks.io ping every tick                               │
  │   structlog → SQLite events table (queryable)                   │
  └─────────────────────────────────────────────────────────────────┘
              │
              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  ACTION CHANNELS                                                │
  │   • Dashboard (FastAPI + vanilla JS)                            │
  │   • Telegram bot (long-polling, bidirectional)                  │
  │   • Specialist MCP (for claude.ai / Claude Code)                │
  └─────────────────────────────────────────────────────────────────┘
```

All four subsystems share **one SQLite database** at
`/var/lib/orclaw/orclaw.db`. There is no separate queue, no Redis, no
broker. Single-writer workloads (everything goes through the
orchestrator) make this trivial.

## The Pro-plan trick

Anthropic's [`claude-code-action`](https://github.com/anthropics/claude-code-action)
listens for `@claude` mentions on GitHub issues + PRs. When it runs on
a **self-hosted runner**, it can authenticate using a long-lived
Claude session token instead of an API key — meaning every dispatch
draws from your $20/mo Pro plan's 5-hour rolling window (~45 messages)
rather than your metered API balance.

Orclaw orchestrates *when* to mention `@claude` and *what to say*. It
never calls the Anthropic API directly. The whole orchestrator could
run on a Raspberry Pi for what it costs.

## Subsystem-by-subsystem

### `orclaw.batch_planner`

Reads open issues, parses "Depends on #N" / "Blocked by #N" / "Closes #N"
edges, runs Kahn topological sort, persists the resulting layers to the
`batches` table. Runs every 10 minutes (timer-driven).

Key constraint: an issue is *eligible* only when (a) all blockers are
merged and (b) no PR already references it. The second guard prevents
double-dispatch when a human opens a PR by hand.

### `orclaw.orchestrator`

Three modules:

- **`state.py`** — the snapshot the loop reads at the top of each tick.
  Exposes engine_state key/value for runtime flags (`paused`,
  `max_in_flight_override`, etc.).
- **`dispatcher.py`** — renders the implementer/reviewer prompt
  templates (read fresh each call, so dashboard edits land instantly)
  and posts the `@claude` comment + applies tracking labels.
- **`loop.py`** — the tick logic. Reviewer pass first (frees in-flight
  slots), then implementer pass against the active layer.

### `orclaw.overlay`

User-managed overlay rooted at `/var/lib/orclaw/overrides/`. The
dashboard writes here when you edit a prompt or a timer; the engine
reads from here transparently and falls back to the git-tracked
defaults. Auto-deploy re-publishes overlays after each sync, so they
survive `git reset --hard`.

### `orclaw.dashboard`

FastAPI app on `127.0.0.1:8766`. ~20 endpoints (read + write +
overlay). Sub-500-line vanilla JS frontend, mobile-responsive.
Designed to sit behind Cloudflare Access — the app trusts every
request that reaches it.

### `orclaw.telegram_bot`

Long-polling bot (no webhook = no public endpoint). Restricted to one
chat ID. Six commands: `/status /pause /resume /skip /tick /quota`.

### `orclaw.specialist`

An MCP server (Streamable-HTTP transport) exposing 13 tools for
read-only inspection + a few safe write actions. Designed to be
consumed from claude.ai (mobile + desktop), Claude Code, Cursor, etc.

## Why SQLite

- Zero ops. Backup is a file copy.
- Single-writer. The orchestrator is the only thing that mutates
  batches/runs; the dashboard mutates `engine_state` (low contention)
  and the overlay (filesystem, not the DB). No locking pain.
- WAL mode for read concurrency.
- Migrating to Postgres later is a few hours of work if you ever
  need multi-writer.

## Why Cloudflare for identity

We don't want to implement auth. CF Access cookies do JWT validation
at the edge; CF Access for SaaS does OAuth + dynamic client
registration for the MCP. The dashboard + MCP code never see a token
or a session — they just trust the request. Less code, smaller blast
radius.

## Event ingestion: webhooks + polling

The orchestrator reacts to GitHub **webhooks** (`/webhook/github` on the
dashboard, HMAC-authenticated) for low latency, and keeps **polling** as
a reconciliation fallback for any delivery that gets dropped. A burst of
deliveries is coalesced into a single trailing tick. See
[`webhooks.md`](webhooks.md).

## Concurrency: one Claude task at a time

The effective concurrency cap defaults to **1** — a single in-flight
`@claude` dispatch. On one Pro/Max seat the seat's 5h window is the real
bottleneck, not the orchestrator, so single-flight is the safe default.
`settings.concurrency.max_in_flight` is a **hard ceiling**: the dashboard
runtime override can only dial the cap *down*, never above it. Raise the
ceiling in config only when you have the quota (e.g. multiple seats) to
run more than one dispatch in parallel.

## What's intentionally NOT here

- **Web UI for editing issues**. GitHub already has one. We're an
  orchestrator, not a project management tool.
- **Multi-repo support**. One orclaw instance == one target repo. If
  you want two, run two — the install is 5 minutes.
- **A custom queue / scheduler**. SQLite + systemd timers cover it.
- **An "AI dev" abstraction**. We just mention `@claude`. All the
  intelligence lives in the prompts.

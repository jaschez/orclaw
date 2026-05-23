# Sample issues

Paste these into your target repo (one issue per `##` block) to give
Orclaw something to chew on. They illustrate the dependency parsing
and how layered scheduling falls out of it.

The resulting plan, after `orclaw planner run`, will be:

```
Layer 0: #1, #2          ← independent, both dispatch immediately
Layer 1: #3, #4          ← both depend on Layer 0
Layer 2: #5              ← depends on Layer 1
```

If you set `max_in_flight=2`, Orclaw dispatches `#1` + `#2` in parallel,
waits for both PRs to merge, then dispatches `#3` + `#4`, etc.

---

## Issue 1 — Add a `/health` endpoint

Add a `GET /health` endpoint to the API that returns
`{"status": "ok"}` with a 200. Include a unit test.

Labels: `P0`

---

## Issue 2 — Wire structured logging

Replace `print(...)` calls in `src/` with `structlog` at INFO level.
Output should be JSON when `LOG_FORMAT=json`, console-friendly
otherwise.

Labels: `P0`

---

## Issue 3 — Add `/metrics` Prometheus endpoint

Expose Prometheus metrics on `/metrics` using `prometheus_client`.
At minimum: request count, request latency, in-flight requests.

Depends on #1

Labels: `P1`

---

## Issue 4 — Forward logs to the events table

Send structured log lines at WARNING+ to a SQLite `events` table
(schema attached) so the dashboard can show them.

Depends on #2

Labels: `P1`

---

## Issue 5 — Daily summary email

Wire a cron job that reads the last 24h of metrics + events and emails
a digest at 23:00 UTC. Reuse the existing notifications module.

Depends on #3, #4

Labels: `P2`

---

## How Orclaw reads this

Run `orclaw planner run` once the issues are open. The planner will:

1. Fetch all open issues.
2. Parse `Depends on #N` lines from each body.
3. Run Kahn's algorithm to layer them.
4. Persist to the `batches` table — visible on the dashboard's
   **orchestration** tab.

Then on every tick (every 30s), the orchestrator picks the next
issue from the lowest non-complete layer that fits within
`max_in_flight`, posts `@claude implement #N`, and waits for the
PR to merge.

You can watch the whole thing happen live from:

- The web dashboard (`http://127.0.0.1:8766`, or your CF Access URL).
- Telegram (`/status`).
- `orclaw status` from any shell.

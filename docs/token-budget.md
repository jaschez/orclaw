# Pro-plan quota — observation and control strategy

> **Auth decision**: zero Anthropic API key. All Claude calls go
> through `@claude` mentions in GitHub Actions, consuming the Pro plan
> via OAuth. See [`pro-plan-strategy.md`](pro-plan-strategy.md).

## Quota reality

- Pro plan: ~225 messages / 5h rolling window (not publicly documented
  exactly; this is our working estimate).
- Shared across: the owner's interactive use + every engine action
  (specialist, implementer, reviewer).
- No API to query remaining % in real time.
- When saturated, Claude returns 429 / auth error → `claude.yml` fails
  with conclusion `failure`.

## What we do NOT track

- ❌ Input/output tokens (Anthropic doesn't expose them for Pro).
- ❌ USD/EUR cost (no invoice, n/a).
- ❌ Cache hit ratio (not measurable from our side).

## What we DO measure (proxies)

- ✅ **Number of `claude.yml` workflow runs** per hour / day / 5h
  window.
- ✅ **Duration of each run** (high latency = saturated backend).
- ✅ **Conclusion of each run** (success / failure / cancelled /
  skipped).
- ✅ **Frequency of auth errors** (direct saturation proxy).
- ✅ **PRs opened per minute** (the system's effective output).

## `runs` table in SQLite

```sql
-- defined in orchestrator/state/schema.sql
runs (
  id TEXT PRIMARY KEY,
  agent TEXT,            -- 'specialist' | 'implementer' | 'reviewer'
  issue_number INTEGER,
  pr_number INTEGER,
  status TEXT,           -- 'queued' | 'running' | 'success' | 'failed' | 'rate_limited' | 'timeout'
  started_at TEXT,
  finished_at TEXT,
  duration_seconds INTEGER,
  workflow_run_id INTEGER,  -- GH Actions run ID for correlation
  notes TEXT
)
```

Every `@claude` mention the engine posts is recorded at post time
(status `queued`) and updated after the matching workflow run is
observed.

## Concurrency

```toml
# config/concurrency.toml
[concurrency]
max_in_flight = 2          # never more than 2 live @claude mentions at once
default_in_flight = 1      # sequential by default, burst to 2 only when quota is healthy

[backoff]
# If we see 2 failures with auth errors in the last 10 min → assume saturation
saturation_threshold_failures = 2
saturation_window_minutes = 10
saturation_cooldown_minutes = 30        # after detecting saturation, wait this long

# If a single action fails, exponential retry
retry_initial_seconds = 60
retry_max_attempts = 3
retry_max_total_minutes = 30
```

## How the orchestrator paces itself

Pseudocode:

```python
async def orchestrator_loop():
    while running:
        if is_saturated():
            await sleep(saturation_cooldown_minutes * 60)
            continue

        active = active_in_flight_count()
        if active >= max_in_flight:
            await sleep(30)
            continue

        batch = next_executable_batch()
        if not batch:
            await sleep(60)  # nothing to do
            continue

        # Post @claude mention for the next issue
        issue = batch.next()
        await post_comment(issue, build_implementer_prompt(issue))
        record_run(issue, status='queued')

        # Wait a bit before posting the next, even if the cap allows it
        # (gives claude.yml time to start without flooding GH webhooks)
        await sleep(30)


def is_saturated() -> bool:
    failures = db.recent_failures(window_minutes=saturation_window_minutes)
    return len(failures) >= saturation_threshold_failures


def active_in_flight_count() -> int:
    # Issues with the agent:start label AND no merged PR yet
    return db.count_active_runs(status_in=['queued', 'running'])
```

## Dashboard `/status/quota`

What it shows:

```
┌─────────────────────────────────────────────────────────┐
│ Quota observation (Pro plan)                            │
│                                                         │
│ Last 5h window:                                         │
│   Total @claude mentions posted:    23                  │
│   Successful runs:                    20                │
│   Failed runs:                         1                │
│   Rate-limited:                        2                │
│   Avg run duration:               4m 12s                │
│                                                         │
│ Saturation status: 🟢 healthy                           │
│ (heuristic: 0 failures in last 10 min)                  │
│                                                         │
│ Currently in flight: 1                                  │
│ - #142 cookie banner (implementer, queued 0m32s ago)    │
│                                                         │
│ Last 24h:                                               │
│   88 mentions · 76 success · 8 failed · 4 rate_limited  │
│                                                         │
│ Last 7d:                                                │
│   524 mentions · 89% success rate                       │
└─────────────────────────────────────────────────────────┘
```

## Anomaly detection

The orchestrator alerts if:

- **>10 failures in 1h** → something is wrong (auth broken? token
  expired?).
- **0 successes in 4h with active mentions** → engine stuck.
- **>200 mentions in 5h** → suspected loop, auto-pause.

## Manual actions for the owner

```bash
# Show quota observation
orclaw quota show

# Force-pause the orchestrator
orclaw pause

# Resume
orclaw resume

# List recent runs
orclaw runs list --limit 20

# Force a saturation analysis now
orclaw quota check
```

## Honest trade-off

This model **does NOT give massive parallelism**. If the Pro plan
becomes a critical bottleneck (you want 10× more throughput), the only
escape hatches are:

1. Migrate implementer + reviewer to an API key (~$50-100/month).
2. Keep the specialist on Pro (still convenient).

The engine is designed so that switch is a **config change**, not a
rewrite. Prompts live in `prompts/`, models in `config/`. To migrate
you'd just:

- `config/auth.toml`: add `anthropic_api_key_env = "ANTHROPIC_API_KEY"`.
- Orchestrator detects the API key and uses the SDK directly instead
  of posting `@claude`.
- The rest of the flow is identical.

But **as long as there's no clear bottleneck signal**, Pro plan +
impersonation is the right call.

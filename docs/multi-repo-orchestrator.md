# Multi-repo orchestrator — design plan

> Status: **design proposal** (no code yet). Scope: turn the single-repo
> orchestrator into one that schedules across several target repos, and
> nail down the identity/permissions model for an external dispatcher.

This document is a technical plan, not an implementation. It records the
constraints, the decisions, and a phased migration path so the actual
PRs can be small and reviewable.

## 1. Premise and hard constraints

The current design (`docs/architecture.md`, `docs/pro-plan-strategy.md`)
is already an **external dispatcher that drives CI/CD**: a server posts
`@claude` comments; the heavy work runs in GitHub Actions on a
self-hosted runner billed to a Claude Pro/Max seat. The server never
calls the metered API.

Two constraints dominate every decision below.

### 1.1 One subscription seat → throughput is capped, not the orchestrator

With **a single Pro/Max seat** (one `CLAUDE_CODE_OAUTH_TOKEN`), real
concurrency is ~2 in-flight `@claude` invocations
(`pro-plan-strategy.md` §"Real concurrency"). Adding repos does **not**
add throughput — it shares the *same* quota across more repos.

> **Therefore: multi-repo here means better _delegation and fairness_,
> not more horsepower.** The win is a single brain that schedules a
> global, dependency-aware queue across N repos and spends the scarce
> ~2 slots where they matter most — plus a clean seam to add seats later
> without re-architecting.

This is the honest framing. If the goal were raw parallel throughput,
the only levers are (a) more seats (a token pool) or (b) the metered
API for overflow. Both are out of scope for the 1-seat case and are
called out as future seams in §8.

### 1.2 The dispatch comment must come from a human-identity PAT

A GitHub App **cannot reliably trigger `claude.yml` via comments**:

- Events originating from the default `GITHUB_TOKEN` do not trigger
  further workflows (GitHub's recursion guard).
- Even with a custom App token, the comment author appears as
  `<app>[bot]`, which collides with the anti-loop filters
  (`github.actor != 'claude[bot]'`) and the actor permission check in
  `claude-code-action` (see anthropics/claude-code-action issues #591,
  #625).

The proven trigger path is a **classic/fine-grained PAT acting as a real
user** — exactly what orclaw does today via `GITHUB_TOKEN` in
`config.py`. **Decision: the dispatch identity stays a user PAT.** A
GitHub App, if introduced, is confined to read/observability/webhook
ingestion (§7) and never authors the `@claude` trigger comment.

## 2. What is single-repo today (the things that must change)

| Concern | Where it's hardwired | File |
|---|---|---|
| Target repo | `GitHubSettings.repo: str` (one `owner/name`) | `orclaw/config.py:67` |
| State | one SQLite DB per instance, no `repo` dimension | `orchestrator/state/schema.sql`, `architecture.md` |
| Dispatch | `gh.post_comment` / `add_label` against the single client | `orclaw/orchestrator/dispatcher.py` |
| Runner | registered to one repo (`actions.runner.jaschez-${TARGET_REPO}`) | `docs/self-hosted-runner.md` |
| Event ingestion | polling one repo every 30s | `pro-plan-strategy.md` (open question: webhooks) |
| Surfaces | dashboard / Telegram / MCP assume one repo | `orclaw/dashboard`, `telegram_bot.py`, `specialist/` |

The dispatch *mechanism* (post a comment, add a label) is already
repo-agnostic — `GitHubClient` just needs a repo target. The real work
is in **state, scheduling, runner registration, and event ingestion**.

## 3. Identity & permissions (the answer to "do we have the perms?")

Yes — for the dispatch side, today, across many repos:

- **Classic PAT** with `repo` + `workflow` + `read:org` already acts on
  every repo the owner can reach. One token can drive N repos with zero
  new auth plumbing.
- **Fine-grained PAT** can be scoped to an explicit allowlist of repos
  with least-privilege permissions (Issues: RW, Pull requests: RW,
  Contents: R, Actions: RW for `workflow_dispatch`). Preferred for
  blast-radius control — one token, N named repos.

Recommendation for the 1-seat / mixed-ownership case: **a single
fine-grained PAT with an explicit repo allowlist.** It is the smallest
change that unlocks multi-repo dispatch and keeps the trigger working
(unlike an App, per §1.2).

GitHub App is **deferred**, not adopted: keep it as an option for the
read/webhook plane only (it brings per-install tokens, higher read rate
limits, and a revocable audit trail), but it must never be the comment
author. Revisit only if/when external clients (§8) make per-tenant token
isolation worth the added complexity.

Runner permissions: a repo-pinned runner can't serve N repos. Move to an
**organization runner group** (or an account-level runner) so several
repos dispatch into the same pool. This is a registration change, not a
code change (`docs/self-hosted-runner.md` §1–2).

## 4. State model changes

Add a **`repo` dimension** to the schema rather than spinning up N
databases — keeps a single global scheduler and a single dashboard.

- New column `repo TEXT NOT NULL` on `batches`, `runs`, `reviews`
  (and any issue/PR-keyed table). Backfill existing rows with the
  current target repo as default.
- Composite keys become `(repo, issue_number)` / `(repo, pr_number)`.
  Today uniqueness is on the number alone; that breaks the moment two
  repos both have issue #1.
- Indexes: `(repo, status)` on `batches`/`runs` to keep the per-tick
  snapshot query cheap as repos grow.
- Keep **one SQLite file**. The single-writer property still holds — the
  orchestrator remains the only mutator of `batches`/`runs`. WAL mode
  unchanged. (The `architecture.md` "Why SQLite" rationale survives; the
  Postgres escape hatch only matters if we later add multi-writer per-repo
  workers, which the 1-seat model does not.)

Migration: an additive schema migration + a backfill. No destructive
change; old single-repo installs read as "one repo named X".

## 5. Config changes

`GitHubSettings.repo: str` → a list of targets. Minimal shape:

```toml
# config/repos.toml (new) or ORCLAW_GITHUB_REPOS env (comma-separated)
[[repos]]
slug = "owner/app-frontend"
priority = 10          # higher = served first when slots are scarce
max_in_flight = 1      # optional per-repo sub-cap

[[repos]]
slug = "owner/app-backend"
priority = 20
```

- Backward compatible: if `ORCLAW_GITHUB_REPO` (singular) is set and the
  list is empty, synthesize a one-element list. Existing installs keep
  working untouched.
- `GitHubClient` becomes per-repo (one instance per slug) or takes a
  `repo` argument per call. The dispatcher already passes a `gh` handle;
  the loop picks the right handle for the chosen issue's repo.
- The **global concurrency cap stays global** (it models the shared
  seat). Per-repo `max_in_flight` is only an *upper* sub-cap so one repo
  can't monopolize the 2 slots; the sum is still bounded by the global
  cap.

## 6. Scheduler: one global queue across repos

The planner today runs Kahn layering **within** a repo. Multi-repo keeps
that per-repo (dependencies don't cross repo boundaries in the common
case) and adds a **cross-repo arbiter** on top:

1. Per repo, compute the eligible frontier exactly as today (next layer,
   nothing already in a PR).
2. Merge all frontiers into one candidate set tagged by repo.
3. While free global slots remain, pick the highest-priority eligible
   candidate, respecting each repo's `max_in_flight` sub-cap.
4. Fairness guard: round-robin tiebreak among equal priorities so a
   noisy repo can't starve the others across ticks (track
   `last_dispatched_at` per repo).

Cross-repo dependencies (e.g. "backend #12 blocks frontend #7") are
**out of scope for v1** — document the limitation; model them as a
manual gate (a `blocked:external` label the human clears) rather than
building a distributed dependency graph.

Reviewer pass stays first (it frees slots) and also becomes global:
review any repo's open PR that needs it before dispatching new
implementers anywhere.

## 7. Event ingestion: polling → webhooks

Polling one repo every 30s is fine. Polling N repos every 30s multiplies
API calls and latency. Two options:

- **Short term (v1):** keep polling but stagger and batch — one
  consolidated fetch per repo per tick, reuse the existing single-fetch
  pattern (`architecture.md` ORCHESTRATOR box). Acceptable up to a
  handful of repos.
- **Target (v2):** a webhook receiver (the one public surface we've
  avoided so far). A GitHub App or a per-repo webhook delivers
  `issue_comment` / `pull_request` / `workflow_run` to the server; the
  orchestrator reacts instead of polling. This is the natural home for
  the **read-only GitHub App** from §3 — it ingests events but still
  never authors the trigger comment.

Webhooks also fix the quota-visibility gap: `workflow_run` completion
events give exact start/finish timing per dispatch, sharpening the
saturation inference in `pro-plan-strategy.md` §"How we measure the
quota".

## 8. Future seams (explicitly out of scope for 1 seat)

Designed-for, not built now:

- **Seat pool.** Replace the single `CLAUDE_CODE_OAUTH_TOKEN` with N
  tokens (N seats). The global cap becomes `2 × seats`; the scheduler
  already speaks "global slots", so this is a capacity knob, not a
  rewrite. This is the only way to get real parallel throughput on the
  subscription model.
- **API overflow / per-client billing.** If the "agency" grows external
  clients, the metered API is the only path to clean per-client cost
  attribution. Add it as an overflow lane behind the same scheduler with
  per-repo/per-client token accounting. Keep it dark until a client
  actually needs an invoice.
- **Read-only GitHub App** for per-tenant audit and webhook isolation
  (see §7).

The state `repo` column, the global-slot scheduler, and the repo
priority config are exactly the seams that make all three additive.

## 9. Phased migration

| Phase | Deliverable | Risk | Status |
|---|---|---|---|
| 0 | This plan + verify the org runner group registration on a throwaway repo | none | — |
| 1 | Schema migration: add `repo` column + backfill; composite keys | low (additive) | **done** |
| 2 | Config: repo list + per-repo `GitHubClient`; singular→list shim | low | next |
| 3 | Scheduler: global slot arbiter + fairness; reviewer pass global | medium | |
| 4 | Surfaces: repo filter in dashboard/MCP/Telegram `/status` | low | |
| 5 | Webhook receiver (when repo count hurts polling) | medium | shipped early¹ |

¹ The webhook receiver (`POST /webhook/github`, [`webhooks.md`](webhooks.md))
landed ahead of the multi-repo work — it's repo-agnostic and useful on a
single repo today.

### Phase 1 — what landed

- `repo TEXT NOT NULL DEFAULT ''` on `batches`, `runs`, `reviews`
  (`orchestrator/state/schema.sql`).
- Idempotent migration `db.apply_migrations()` (runs on every `init_db`)
  + `db.backfill_repo()` + a `orclaw db migrate` CLI command for existing
  installs. Adds the column, swaps the active-uniqueness index to
  `(repo, issue_number)`, adds `(repo, status)` / `(repo)` indexes.
- Writes are repo-tagged: planner batch inserts, `create_run` (via
  `GitHubClient.repo`), and reviewer reconciliation all stamp the target
  repo. `''` remains the legacy/single-repo sentinel.
- **Reads and the scheduler are unchanged** — still single-repo. Phase 2
  (config repo-list) and Phase 3 (cross-repo arbiter) build on this.

Each phase ships independently; after phase 3 the system is genuinely
multi-repo on one seat.

## 10. Risks & open questions

- **Quota starvation across repos.** With 2 global slots, a 3rd+ repo
  waits. The fairness guard bounds latency but cannot create throughput.
  Set repo priorities deliberately; surface "waiting on global quota" per
  repo on the dashboard so it's never a mystery.
- **PAT blast radius.** One PAT now touches N repos. Use a fine-grained
  PAT with an explicit allowlist + 90-day expiry; keep the kill switch
  (`pro-plan-strategy.md` §"Impersonation").
- **`claude.yml` drift across repos.** Every target repo must carry a
  compatible `claude.yml` + `auto-merge.yml` + `cleanup-agent-start.yml`
  and the `ORCLAW_RUNNER` variable. Add a `doctor` check that verifies
  each configured repo has the required workflows + variable before the
  orchestrator will dispatch to it.
- **Cross-repo dependencies** are unmodeled in v1 (manual label gate).
- **Open question:** confirm the org runner group + fine-grained PAT
  combination triggers `claude.yml` on a non-default branch repo before
  committing phase 2 — validate on a throwaway repo in phase 0.

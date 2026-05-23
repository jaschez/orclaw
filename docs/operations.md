# Operations — day-to-day playbook

Your manual for when the engine is in production.

## 1. Working with the engine on a normal day

```bash
# Morning (~5 min)
ssh orclaw@<server>
orclaw status                      # quick view of current state
# or in the browser: https://<your-domain>/status

# Talk to the Specialist about what to build next
orclaw specialist
> [conversation about the new feature]
> yes, go ahead and create the issues

# Rest of the day: forget about it
# The engine runs batches, opens PRs, reviewer agents review,
# auto-merge merges. Your only human touchpoints: a PR landing on
# "needs-changes" or an OPS issue assigned to you.

# End of day
orclaw status                      # what closed today, what OPS are pending
```

## 2. When YOU have to act

| Situation | What to do |
|---|---|
| OPS issue (label `ops`) | Run it locally with the right MCPs/CLIs (DB, Stripe, Vercel, etc.) |
| PR with label `needs-changes` | Read the reviewer agent's feedback, decide whether you fix it yourself or ask the specialist to re-scope the issue |
| PR with label `requires-human-review` | Your human review before merging |
| Auto-opened issue `engine:budget-hard-stop` | Decide: pay more this month or wait until next, then `orclaw budget resume` |
| Auto-opened issue `engine:dep-cycle-detected` | Edit the bodies of the involved issues to break the cycle |
| Auto-opened issue `engine:implementer-failed-3x` | Inspect the failed runs' logs. Probably the issue is ill-defined or missing context |
| Server down (Healthchecks alert) | SSH in, `journalctl -u orclaw-orchestrator -n 100`, decide |

## 3. Typical `orclaw` CLI commands

```bash
orclaw status                              # ASCII dashboard in the terminal

orclaw specialist                          # enter conversational mode
orclaw specialist --resume <id>            # resume a previous conversation

orclaw planner run                         # force a batch recomputation now
orclaw planner show                        # show layers + current state

orclaw batch start                         # spawn implementer for the current batch (if not auto)
orclaw batch cancel                        # abort the in-flight batch

orclaw implementer logs <issue_number>     # logs from the #N implementation
orclaw implementer retry <issue_number>    # force retry

orclaw reviewer review <pr_number>         # run the reviewer manually on a PR

orclaw budget show                         # monthly budget status
orclaw budget pause                        # stop spawning new work
orclaw budget resume                       # resume
orclaw budget set --monthly-eur 80         # update target

orclaw config edit                         # open config in $EDITOR
orclaw config validate                     # validate config without applying
```

## 4. Controlled pauses

If for any reason you need to pause the engine (holiday, debugging,
budget exhausted):

```bash
# Soft pause: let in-flight batches finish, don't start new ones
orclaw pause

# Hard pause: abort everything in flight as well
orclaw pause --hard

# Resume
orclaw resume
```

What was in flight resumes where it left off (soft pause) or is
marked `pending` for retry (hard pause).

## 5. Anti-patterns the engine does NOT do

So you know what to expect:

- **Does NOT merge PRs labelled `do-not-auto-merge` or
  `requires-human-review`** — those go through you.
- **Does NOT open PRs against issues labelled `ops` or
  `do-not-implement`.**
- **Does NOT touch issues labelled `wontfix` or already closed.**
- **Does NOT modify workflows under `.github/workflows/`** unless the
  issue carries BOTH `infra` AND `agent-allowed`.
- **Does NOT commit secrets** (hard check in the reviewer).
- **Does NOT force-push** (force push is banned on any branch other
  than its own PR branch).
- **Does NOT merge to `main`** — only to `develop`.

## 6. How the Specialist surfaces decisions to you

The Specialist is conversational but structured. Typical outputs:

**A) Scoping conversation**

> Let's see. I reviewed the V1 spec and the open issues. Before
> generating new issues for "X", I need you to decide:
>
> 1. Does this apply to paid events, free events, or both?
> 2. Is it mandatory or optional for the organiser?
> 3. Do you want analytics attached?
>
> If you want the fast path, reply "default" and I'll pick whatever
> feels most consistent with the rest of the product.

**B) Issue plan**

> Plan generated: 4 issues + 1 OPS, 2 batches.
>
> **Batch 1** (parallel, no deps):
> - #201 [P0] coupons table + RLS + admin RPC
> - #202 [P0] frontend: coupon-code input at checkout
>
> **Batch 2** (depends on batch 1):
> - #203 [P0] stripe-create-ticket-checkout: apply coupon
> - #204 [P1] /manage/[event]/coupons: CRUD UI
>
> **OPS** (YOURS):
> - #205 [OPS] apply migration (`npx <your-db-cli> import migrations/...`)
>
> Estimated cost: ~$2.50 in tokens. Estimated total time: 1.5-2h real.
>
> Should I create them? [yes/edit/abort]

**C) Risk notice**

> Heads up: the feature you propose touches the Stripe payment flow.
> The reviewer agent does NOT apply auto-merge in that area — you'll
> have to approve those PRs manually.
>
> This is because of the `requires-human-review` label I auto-apply
> to changes in:
> - `<your-payments-dir>/stripe-*`
> - `src/services/payment*`
> - schema changes to `tickets` or `events`
>
> Proceed anyway?

## 7. How the Reviewer Agent talks to you

The reviewer ALWAYS comments on the PR. Typical outputs:

**A) Approved**

> ✅ **Reviewer Agent: approved**
>
> Programmatic checklist:
> - ✓ Closes #201 in body
> - ✓ Tests added: `coupons.test.ts`, `useCoupons.test.jsx`
> - ✓ No secrets, no console.log, no debugger
>
> Qualitative analysis:
> - Meets the issue's acceptance criteria
> - Edge cases covered: expired code, code without uses, code applied
>   twice (rejected in tests)
> - Style consistent with the rest of the hooks
>
> Applying label `auto-merge`. The workflow will merge it once CI is
> green.

**B) Minor fixes applied**

> 🔧 **Reviewer Agent: minor fixes applied**
>
> I found two minor things I fixed directly:
> - Missing aria-label on the coupon input → commit `a1b2c3d`
> - Broken test snapshot after the change → regenerated in commit
>   `e4f5g6h`
>
> Re-evaluating... ✅ approved. Applying `auto-merge`.

**C) Needs your hand**

> ⚠️ **Reviewer Agent: needs-changes**
>
> Blocking:
> - The PR introduces direct use of `process.env.STRIPE_SECRET_KEY`
>   on the frontend (`src/services/coupons.js:42`). This breaks the
>   server/client separation and leaks the secret to the bundle.
>
> Suggestions:
> - Move the operation to a new edge function or extend the existing
>   `stripe-create-ticket-checkout` with a new parameter.
>
> Applying label `needs-changes`. Did NOT apply `auto-merge`. Decide:
> ask the specialist to re-scope the issue so the implementer gets it
> right, or fix it yourself?

## 8. Healthchecks you should set up

Recommended: free account at [healthchecks.io](https://healthchecks.io).
Create:

| Check | Frequency | What it notifies |
|---|---|---|
| `orclaw-orchestrator alive` | every 5 min | the orchestrator pings the ping URL |
| `orclaw-backup daily` | every 24h | the backup script pings on completion |
| `orclaw-batch-planner` | every 15 min | the planner pings after each run |

Notification: email + Slack if you have it. If a check doesn't ping in
its window, you get an alert.

## 9. Audit / compliance

If you ever need to know who/what did what:

- **Every API call is logged in `token_ledger`** (which agent, which
  issue/PR, when, how much).
- **Every implementer run is in the `runs` table** (input, output,
  status, branch, commits).
- **Every batch state change is in `batch_history`**.
- **GitHub events already store commits + PRs + comments**.

For "what happened in May":

```sql
SELECT date(started_at) AS day,
       SUM(cost_usd_cents)/100.0 AS usd,
       COUNT(DISTINCT issue_number) AS issues_worked,
       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes
FROM token_ledger
WHERE started_at >= '2026-05-01' AND started_at < '2026-06-01'
GROUP BY day
ORDER BY day;
```

## 10. When to override manually

Almost never. But sometimes:

- **You need an issue done NOW**, no planner wait: `orclaw implementer
  start <issue_number>` (skip the queue).
- **You know the reviewer will reject it but you want to merge anyway**
  (rare, e.g. experimental spike): add `force-merge` label on the PR
  + leave a justification comment. The engine accepts `force-merge`
  only if YOU are the comment author (not an agent).
- **You want to test a specialist-prompt change without risk**:
  `orclaw specialist --dry-run` doesn't create issues, only shows what
  it would create.

## 11. Safety anti-loops

The engine does NOT allow:

- Creating more than 100 issues in a single Specialist session
  (runaway guard).
- Spawning more than 10 simultaneous implementers (hard cap above the
  budget cap).
- Re-implementing the same issue more than 3 times without your
  intervention.
- Merging a PR whose last commit was made by the reviewer agent
  (anti-circular self-approval) — the reviewer can commit fixes but
  CANNOT be the last committer of the merge.

If any of these triggers fires, alert + auto-pause.

# Pro-plan-only execution strategy

## Decision

**100% of the agent work runs on the Claude Pro plan**, via the
`claude.yml` workflow that lives in `${TARGET_REPO}`. **Zero use of
the metered Anthropic API.**

The concrete mechanism:

1. The engine (remote server) acts as a dispatcher: decides what to do,
   when, where, with what prompt.
2. For each action, the engine **posts a comment on an issue or PR**
   mentioning `@claude` + the instructions.
3. The comment appears under the **owner's GitHub identity** via a
   Personal Access Token with `repo` scope. This is legitimate — the
   owner explicitly authorises the server to act on their behalf inside
   their own repo.
4. GitHub fires the `claude.yml` workflow (already in place).
5. Claude responds using the `CLAUDE_CODE_OAUTH_TOKEN`, which is bound
   to the Pro plan.
6. All the work (implementation, review, issue planning) consumes the
   Pro-plan quota — no extra cost.

## Why this model

- **Extra cost: $0.** The earlier API-key hypothesis budgeted
  ~$20-100/month. Eliminated.
- **One auth surface with Anthropic** (OAuth Pro), already configured
  and validated.
- **No per-cent token-tracking logic** — the budget is the Pro plan,
  no invoice line items.
- **Reuses existing workflows** (`claude.yml`, `cleanup-agent-start.yml`,
  `auto-merge.yml`).

## What we trade for it

| Limitation | Mitigation |
|---|---|
| Pro plan ~225 msg/5h shared across ALL activity | Real concurrency cap = 1-2 in flight. Sequential by design |
| No direct visibility of remaining Pro quota | Inferred indirectly (workflow durations, 429s, conclusion=failure with auth errors) |
| If your interactive use + the engine coincide, you block yourself | The specialist agent also goes through `@claude` → minimises conflict |
| Higher latency (claude.yml takes ~10-30s to start after the comment) | Acceptable for this use case. Not real-time |
| No mass parallelism: even if we post 5 `@claude` comments at once, Claude's backend rate-limits | Engine respects the cap, never floods. If Pro blocks, exponential retry |

## How the cycle works, step by step

### Phase 1 — Planning (Specialist)

1. The owner creates / reopens the **specialist conversation issue**
   (e.g. a dedicated issue titled `[META] Specialist conversation`).
2. They comment their idea/requirement in natural language. **Without**
   `@claude` (this is for the owner only — nothing fires).
3. When ready, they comment: `@claude specialist: act as the specialist
   agent, analyse and propose a plan`.
4. `claude.yml` fires → Claude reads the full thread + repo spec →
   responds with a structured plan.
5. The owner confirms with `@claude proceed` or edits the requested
   tweaks.
6. Claude creates the issues with the `gh` CLI from inside the Action
   runner.

Simpler alternative: the engine ships a local CLI `orclaw specialist`
that just wraps posting the comment to the meta issue and reading the
reply. Same thing with nicer UX.

### Phase 2 — Implementation (Implementer)

1. The Batch Planner (on the server) recomputes which issues are ready.
2. The Orchestrator selects N (with N small, 1-2) from the current
   batch.
3. For each one, it posts a comment on the issue:
   ```
   @claude implement: implement this issue following the acceptance
   criteria. Spec in docs/. Branch feat/<num>-<slug>. PR body with
   "Closes #N". Apply auto-merge label if CI is green.
   ```
4. `claude.yml` fires → Claude implements → opens a PR against `develop`.
5. The Orchestrator observes the `pull_request.opened` event and moves
   to phase 3.

### Phase 3 — Review

1. The Orchestrator detects an open PR that closes one of the batch's
   issues.
2. Posts a comment on the PR:
   ```
   @claude review: act as the reviewer agent (see prompts/reviewer.md
   in orclaw). Apply hard checks + qualitative analysis. Decide
   approved / needs-changes.
   ```
3. `claude.yml` fires → Claude reads PR + acceptance criteria + spec →
   comments the verdict.
4. If approved → Claude applies the `auto-merge` label (it has the
   permission via OAuth).
5. `auto-merge.yml` merges once CI is green.
6. `cleanup-agent-start.yml` removes `agent:start` when the issue
   closes.

### Phase 4 — Advance

1. The Orchestrator detects that ALL the batch's issues are `merged` or
   `failed`.
2. Advances to the next layer.
3. Loops back to phases 2-3.

## Real concurrency

Since EVERYTHING goes through a single OAuth token / Pro plan:

- **Hard limit: 2 concurrent `@claude` invocations in flight.** More is
  counterproductive (rate limit → fails → retry → quota burned).
- When in doubt, **sequential**. Slower but predictable.
- If the Pro-plan rolling window just reset, we can do a mini-burst of
  2-3 quick actions and then drop back to 1.

## How we measure the quota without direct access

Anthropic doesn't expose remaining Pro-plan %. We infer it:

1. **Workflow run latency**: if `claude.yml` takes > 5 min to start
   (queued), Pro is saturated.
2. **Conclusion=failure with auth error**: the OAuth token returns
   401/429 when exhausted → logged in `engine.db.runs` with
   `status='rate_limited'`.
3. **Run frequency over the last 5h**: an approximation of consumed
   quota. The engine counts runs (not tokens — that's not exposed).

When we detect saturation:

- Wait 5 min, retry.
- If 3 retries fail → mark the slot as `wait_for_quota`.
- Notify the owner ("Pro quota saturated, expect ~30-60 min").
- Resume when the next probe action returns success.

## Impersonation: the owner's PAT

The engine needs a classic GitHub PAT under the owner's account with
scopes:

- `repo` (full): to comment on issues/PRs, read state, apply labels.
- `project`: to move cards on Project V2.
- `workflow`: in case it needs `workflow_dispatch`.
- `read:org`: optional, if you use organisation-level features.

Store this PAT as `GITHUB_TOKEN` in `/etc/orclaw/secrets.env` on the
server.

**Risk**: if someone compromised the server, they could comment as the
owner and fire Claude runs that drain the Pro plan. **Mitigation**:

- Hardened server (SSH key only, ufw, no root, no password).
- PAT with explicit expiry (90 days, manual renewal).
- Healthchecks that detect anomalous activity (>X comments/hour).
- Kill switch: close the tracker issue → engine stops all `@claude`
  mentions.

## The `claude.yml` side: anything to change?

`${TARGET_REPO}/.github/workflows/claude.yml` stays as-is. It accepts:

- `@claude` in issue comments.
- `@claude` in PR comments (review).
- `@claude` in the body when opening / assigning an issue.

The **prompt** Claude receives depends on the **comment content**.
That's why it's critical for the engine to post well-structured comments
(see the next section).

## Comment convention the engine posts

Each comment starts with a mode tag the agent recognises:

```
@claude implement: <extended implementer prompt>
@claude review: <extended reviewer prompt>
@claude specialist: <extended specialist prompt>
@claude triage: <prompt to classify orphan issues>
```

The extended prompt lives in `prompts/*.md` in this repo and is
injected verbatim when posting. Versioned, auditable.

Example comment the engine posts to implement:

```markdown
@claude implement

Follow the implementer agent's instructions in `prompts/implementer.md`
of the orclaw repo. Summary:

- Read the full body of this issue + the spec in docs/.
- New branch: feat/<NUM>-<slug>
- PR body MUST contain: "Closes #<NUM>"
- Tests per the issue's "Test coverage" section.
- If CI is green after push, apply the `auto-merge` label.
- DO NOT touch .github/workflows/ unless the issue carries both
  `area:infra` and `agent-allowed` labels.

Issue: #142
Spec: ${TARGET_REPO}/docs/...
Conventions: ${TARGET_REPO}/CLAUDE.md

---
🤖 Posted by the orclaw orchestrator (run 8a2f...)
```

Repeatable structure. Auditable. Passes the `@claude` filter in
`claude.yml`.

## Summary of the final model

```
       ORCLAW SERVER                ${TARGET_REPO} + GH Actions
       ────────────────────                ──────────────────────────

   ┌──────────────────────┐
   │ Specialist (CLI or   │ ───┐
   │ issue meta-thread)   │    │
   └──────────────────────┘    │ posts @claude specialist:
                                │
   ┌──────────────────────┐    │
   │ Batch Planner        │    │
   │ (pure algorithm)     │    │
   └──────────┬───────────┘    │
              │                 │
              ▼                 │
   ┌──────────────────────┐    │
   │ Orchestrator         │ ───┴──────┐
   │ (state machine)      │            │ posts @claude implement:
   └──────────┬───────────┘            │ posts @claude review:
              │                         │
              │                         ▼
              │                  ┌─────────────────────┐
              │                  │ claude.yml          │
              │                  │ (OAuth, Pro plan)   │
              │                  │ - implementer       │
              │                  │ - reviewer          │
              │                  │ - specialist        │
              │                  └──────────┬──────────┘
              │                              │
              │                              ▼
              │                  ┌─────────────────────┐
              │                  │ Claude responds     │
              │                  │ - opens PR          │
              │                  │ - comments review   │
              │                  │ - applies labels    │
              │                  └──────────┬──────────┘
              │                              │
              │                              ▼
              │                  ┌─────────────────────┐
              │                  │ auto-merge.yml      │
              │                  │ cleanup-agent-start │
              │                  └──────────┬──────────┘
              │                              │
              │     observes via webhooks/poll│
              └──────────────────────────────┘
```

## Open questions

1. **Specialist mode**: local CLI invoked from your machine vs. issue
   meta-thread on GitHub? The latter is 100% pro-only-via-GitHub, but
   the UX is slower. Pick one.
2. **Webhooks vs polling**: does the engine receive GitHub webhooks
   (requires a public endpoint) or poll every N seconds (simpler, worse
   latency)?
3. **PAT lifetime**: 90 days with manual renewal reminder, or no
   expiry? (Recommend 90d.)

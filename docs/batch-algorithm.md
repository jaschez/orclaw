# Batch algorithm — how parallel batches are formed

## Goal

Given a set of issues with dependencies declared in their bodies
(`Blocked by #N`), produce a sequence of **batches**:

- **Inside** a batch: issues that are fully independent → parallel.
- **Across** batches: sequential (batch K+1 waits for ALL of batch K's
  issues to be merged).

This is exactly the model: parallel batches with no internal
dependencies, sequential across batches.

## Definitions

- **Open non-OPS issue**: `state=OPEN` and does NOT carry the `ops`
  label.
- **Open dep**: a `Blocked by #N` where issue #N is `state=OPEN`.
- **Layer (Kahn)**: minimum depth in the dependency graph.

## Algorithm

```python
def compute_batches(issues: list[Issue]) -> list[list[Issue]]:
    """
    Returns layers of issues, where each layer can be processed in parallel.
    Layer 0 has no dependencies. Layer K depends only on layers < K.
    """
    # 1. Build adjacency: blocker → blocked
    blocks = defaultdict(set)
    blocked_by = defaultdict(set)
    candidates = {i.number: i for i in issues if not i.is_ops()}

    for issue in candidates.values():
        for dep_num in parse_blocked_by(issue.body):
            # Only count deps that are still OPEN — closed ones don't block
            if dep_num in candidates:
                blocks[dep_num].add(issue.number)
                blocked_by[issue.number].add(dep_num)

    # 2. Kahn's topological layering
    layers = []
    in_degree = {n: len(blocked_by[n]) for n in candidates}
    remaining = set(candidates.keys())

    while remaining:
        # Current layer = all issues with zero remaining deps
        current_layer = sorted(
            [n for n in remaining if in_degree[n] == 0],
            key=lambda n: (priority_rank(candidates[n]), n),
        )

        if not current_layer:
            # Cycle in dependencies — should not happen. Surface as error.
            raise DependencyCycle(remaining=remaining, in_degree=in_degree)

        layers.append([candidates[n] for n in current_layer])
        for n in current_layer:
            remaining.discard(n)
            # Decrement in_degree of issues that this one blocked
            for unblocked in blocks[n]:
                in_degree[unblocked] -= 1

    return layers


def priority_rank(issue: Issue) -> int:
    """P0 first, P1 second, anything else last."""
    labels = {l.name for l in issue.labels}
    if "P0" in labels: return 0
    if "P1" in labels: return 1
    return 2


def parse_blocked_by(body: str) -> set[int]:
    """Extract Ns from 'Blocked by #N' patterns. Case-insensitive."""
    return {int(m) for m in re.findall(r"[Bb]locked by #(\d+)", body or "")}
```

## How a batch is executed

The Orchestrator picks the first non-empty layer that is NOT blocked
by active implementations:

```python
def next_executable_batch(layers: list[list[Issue]], state: OrchestratorState) -> list[Issue]:
    """
    Pick the first layer where NO issue has an open PR or active implementer.
    Apply concurrency cap based on budget.
    """
    for layer in layers:
        issues_in_progress = state.issues_with_open_pr() | state.issues_with_active_implementer()
        if not any(i.number in issues_in_progress for i in layer):
            # This layer is ready to start
            cap = budget.current_concurrency_cap()  # 2..5 depending on remaining quota
            return layer[:cap]
    return []  # All layers fully in progress, just wait
```

## Extra filters applied to the chosen batch

Before spawning implementers, the Orchestrator filters each issue in
the batch:

1. **`ops` label** → exclude (OPS issues are yours, not the agent's).
2. **`do-not-implement` label** → exclude (per-issue kill switch).
3. **PR open referencing the issue (closingIssuesReferences)** →
   exclude.
4. **`agent:start` label already present** → exclude (a previous
   process already picked it up).
5. **Previous implementation failed ≥ 3 times** → exclude + alert the
   owner.

## Worked example with real data

State after a cleanup. Open non-OPS, non-closed issues:

```
#110 [P1] Sentry integration: no deps (every "Blocked by" is closed)
#131 [META] Daily tracker: META, not processed
```

After the cleanup few are left, so the current batch is:

```
Layer 0 = [#110]
Layer 1+ = empty
```

Batch to execute: just `#110`. The Orchestrator spawns 1 implementer,
waits, and once it finishes the array empties → engine idle until the
Specialist generates new issues.

## A more interesting example (initial V1 state)

Had we booted the engine at the very start of plan V1 with the 29
original issues:

```
Layer 0:  [#88 schema, #107 legal pages, #108 cookie banner, #116 test infra]
          (4 parallel, none depend on anything open)

Layer 1:  [#89, #91, #95, #98, #109, #112]
          (all depend on #88 and/or #107, which are in layer 0)

Layer 2:  [#90, #92, #94, #96, #97, #99, #100, #111, #113, #114]

Layer 3:  [#93, #101, #102, #103, #104, #106]

Layer 4:  [#105, #115]

Layer 5+: already closed or empty
```

With a concurrency cap of 5 (generous budget), each layer takes ≈ the
time of the slowest implementer (~20 min) plus review. ~30-40 min per
layer. 5 layers = 2-3h real time for all of V1, assuming Claude
doesn't fail and there are no conflicts.

With a cap of 2 (tight budget), ~5-6h real time.

## Batch Planner triggers

The planner re-runs the algorithm when:

| Event | Latency |
|---|---|
| Systemd cron timer (`orclaw-batch-planner.timer`) | Every 10 min |
| GH webhook `issues.closed` or `pull_request.closed.merged` | < 30s |
| Manual: `orclaw batch-planner run` | immediate |
| After a `claim_batch` from the Orchestrator | immediate |

## Planner state persistence

SQLite `engine.db`:

```sql
CREATE TABLE batches (
  id INTEGER PRIMARY KEY,
  layer INTEGER NOT NULL,
  issue_number INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'in_progress', 'merged', 'failed', 'skipped')),
  implementer_run_id TEXT,
  pr_number INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_batches_layer_status ON batches(layer, status);
CREATE UNIQUE INDEX idx_batches_issue ON batches(issue_number) WHERE status != 'failed';
```

`status` lifecycle:
- `pending` — computed by the planner, waiting for the orchestrator to
  pick it up.
- `in_progress` — orchestrator spawned the implementer.
- `merged` — PR merged, issue closed.
- `failed` — implementer failed 3 times, requires human review.
- `skipped` — the issue was closed externally without our PR.

The orchestrator only advances to layer K+1 when ALL issues in layer K
are in `merged | skipped | failed`. (`failed` doesn't block — it's
reported and the loop continues.)

## Edge cases handled

- **Issue's deps change while it's queued**: the planner recomputes on
  every run. If a `pending` issue's deps change, it's reassigned to a
  different layer.
- **Issue closed manually (human) while `in_progress`**: the
  orchestrator cancels the implementer at its next health check,
  marks status `skipped`.
- **Accidental dep cycle** (A blocked by B, B blocked by A): the
  planner detects + blocks the whole cycle, opens an internal issue
  `engine:dep-cycle-detected` with the involved IDs.
- **OPS issue mistakenly marked `agent:ready`**: the planner ALWAYS
  excludes `ops` — top-priority filter. To make an OPS issue
  automatable, remove the `ops` label first.

## Planner metrics

Each planner run emits to `engine.db.metrics`:

- Total number of layers computed.
- Number of issues in `current_batch`.
- Compute time (should be sub-second for <500 issues).
- "Orphan" issues detected (no expected labels, no parseable deps).

Visible on the dashboard at `/status/planner`.

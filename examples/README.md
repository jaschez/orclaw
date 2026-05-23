# Examples

End-to-end examples of how a "target repo" should be wired so Orclaw
can drive it. Pick the closest match to your project, copy the pieces
you need.

## Quick map

| Example | What it shows |
|---|---|
| [`toy-target-repo/`](toy-target-repo/) | Minimum viable target repo: `claude.yml` workflow, sample issues with `Depends on` edges, what labels Orclaw expects. **Start here.** |
| [`demo-seed.md`](demo-seed.md) | How to populate a local Orclaw with synthetic data so you can explore the dashboard without firing real `@claude` dispatches. |

## How Orclaw sees your repo

Once you point `ORCLAW_GITHUB_REPO=owner/yourrepo` at any repo and
register a self-hosted runner there, Orclaw expects exactly two
things from the repo:

1. **A `claude.yml` workflow** that triggers on `issue_comment` and
   runs `anthropics/claude-code-action`. The
   [`toy-target-repo/.github/workflows/claude.yml`](toy-target-repo/.github/workflows/claude.yml)
   below is the canonical recipe.
2. **Issues with optional `Depends on #N` lines** in the body. Orclaw
   parses these to build the dependency DAG; absence of any "depends"
   line means "this issue is in layer 0 — pick any time."

That's it. No magic comments in your code, no commits to your repo
required up front. The first time Orclaw runs, it'll just read the
issues you already have.

## Label vocabulary

Orclaw applies these labels as it drives the work — your CI should
respect them but you don't need to *create* them; the engine creates
them on first use.

| Label | Set by | Meaning |
|---|---|---|
| `agent:start` | Orchestrator | Cleanup hook; tells you the implementer was dispatched |
| `agent:ready` | claude-code-action | Implementer finished, PR ready |
| `review:pending` | Orchestrator | Reviewer was dispatched; waiting for verdict |
| `review:approved` | Reviewer | Clean PR — auto-merge eligible |
| `review:needs-changes` | Reviewer | Has comments, won't auto-merge |
| `review:hard-block` | Reviewer | Don't merge under any circumstances |
| `review:minor-fixes-applied` | Reviewer | Reviewer pushed trivial fixes themselves |
| `requires-human-review` | You | Opt-out: this PR needs a human, skip reviewer |

## What lives in your target repo vs. Orclaw

```
your-target-repo/                    orclaw VM (separate, persistent)
├── .github/workflows/               ├── /opt/orclaw/        — code
│   └── claude.yml         ───┐      ├── /etc/orclaw/        — secrets
├── issues (GitHub-hosted)    │      ├── /var/lib/orclaw/    — SQLite + overlays
└── src/, tests/, etc.        │      └── systemd timers      — scheduling
                              │
                              │  Orclaw posts @claude here
                              └─────►  claude-code-action runs
                                       on YOUR self-hosted runner
                                       (also on the orclaw VM)
```

Note the self-hosted runner can live on the same VM as Orclaw or on a
separate machine — both work. Same-machine is the cheapest setup and
what the cloud-init template provisions.

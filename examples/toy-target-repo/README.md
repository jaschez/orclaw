# Toy target repo

A minimum example of what your project repo needs to wire so Orclaw
can drive it. Copy the pieces — they're meant to be modified.

## Tree

```
toy-target-repo/
├── .github/
│   └── workflows/
│       └── claude.yml          ← THE one file you actually need
├── ISSUES.md                   ← sample issues with "Depends on" edges
└── README.md                   ← (this file)
```

## What you need to do in your real repo

### 1. Drop `claude.yml` into `.github/workflows/`

Trigger: comments on issues/PRs that mention `@claude`. The action:

- Authenticates as your Pro-plan session (configured at the runner
  level — see [Anthropic's docs](https://docs.anthropic.com/en/docs/claude-code/github-actions#using-pro-plan-on-self-hosted-runners)).
- Runs Claude with read/write access to the repo via `GITHUB_TOKEN`.
- Opens or pushes to a PR named after the issue.

The recipe is in [`.github/workflows/claude.yml`](.github/workflows/claude.yml).

### 2. Register a self-hosted GitHub Actions runner

In your target repo: **Settings → Actions → Runners → New self-hosted
runner**. Follow GitHub's script — about 90 seconds. The runner can
live on the same VM as Orclaw (cheapest) or a separate machine.

The Pro-plan billing **only** works on self-hosted runners. The hosted
GitHub runners will fall back to charging the API.

### 3. Write issues normally

Plain GitHub issues. The only thing Orclaw cares about is **dependency
edges** in the body. Three recognised formats (all case-insensitive):

```
Depends on #42
Blocked by #42, #43
Closes #100   ← only for PRs; ignored on issues
```

If an issue has no `Depends on` line, it lands in layer 0 and is
eligible immediately.

See [`ISSUES.md`](ISSUES.md) for sample issue bodies you can paste.

### 4. (Optional) Add a priority label

Orclaw respects `P0` / `P1` / `P2` labels for ordering *within a
layer*. Issues without one fall to the end.

## What Orclaw does NOT need

- ❌ No custom commit hooks.
- ❌ No `orclaw.yml` config in the repo.
- ❌ No agent files in your codebase.
- ❌ No specific branching strategy (it works with trunk-based or
  GitFlow; the reviewer just looks at PR base).
- ❌ No SDK or library dependency in your code.

It's just GitHub + your runner + a single workflow file.

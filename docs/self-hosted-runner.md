# Self-hosted GitHub Actions runner

The Oracle Cloud VM doubles as a self-hosted runner for `${TARGET_REPO}`'s
workflows. This document covers:

1. How the runner is installed and isolated
2. How workflows opt in
3. How the engine monitors the runner and falls back automatically
4. Security model + audit

## 1. Install layout

| Path | Owner | Notes |
|---|---|---|
| `/home/github-runner/` | `github-runner` | Dedicated user, no sudo |
| `/home/github-runner/runner/` | `github-runner` | actions-runner binary + config |
| `/home/github-runner/runner/_work/` | `github-runner` | Where each workflow checks code out |
| `/etc/systemd/system/actions.runner.jaschez-${TARGET_REPO}.self-hosted-runner-1.service` | root | systemd unit installed by `svc.sh install` |
| `/etc/orclaw/secrets.env` | `root:engine` mode 640 | **github-runner cannot read this** ✓ |

The runner reports its labels as `[self-hosted, linux, ARM64, oracle, orclaw]`.

## 2. Workflow opt-in

Workflows in ${TARGET_REPO} use a repo-level variable for the runner selector:

```yaml
jobs:
  ci:
    runs-on: ${{ vars.ORCLAW_RUNNER || 'ubuntu-latest' }}
```

The variable `ORCLAW_RUNNER` lives at the repo level (Settings → Secrets and
variables → Actions → Variables). The orclaw sets it to:

- `self-hosted` when the runner systemd unit is `active`
- `ubuntu-latest` when the runner is `inactive` / `failed` / unreachable

Workflows therefore **never break** when the VM goes down — the next firing
just lands on GitHub-hosted runners.

If you want to migrate a workflow:

```diff
- runs-on: ubuntu-latest
+ runs-on: ${{ vars.ORCLAW_RUNNER || 'ubuntu-latest' }}
```

That's the only line that changes. Start with CI workflows that:

- Don't depend on Linux x64-specific binaries (most won't)
- Don't push images to a registry (or, if they do, that the registry
  accepts ARM64)
- Don't take >15 min (our suspicious-activity threshold)

**Recommended migration order**: lint → unit tests → build → integration
tests → claude-code action (last, because it's the heaviest).

## 3. Automatic monitoring

The `orclaw-runner-monitor.timer` (every 2 minutes) does:

1. `systemctl is-active actions.runner....service` — is the runner up?
2. If state changed since last check, flip `ORCLAW_RUNNER` repo variable
   via the GitHub API and fire a Telegram alert.
3. Scan recent workflow runs (last 10 minutes) for:
   - Runs longer than 15 minutes (`LONG_RUN_THRESHOLD`)
   - Runs from actors outside `{jaschez, github-actions[bot]}`
   - Runs ending in `conclusion: failure`
   Each match → one Telegram alert (de-duped per run ID).

Manual run:

```bash
orclaw runner monitor
```

Output line example:

```
runner.systemd_active=true transitioned=False ORCLAW_RUNNER='self-hosted' suspicious_flagged=0
```

## 4. Security model

### What github-runner can do

✅ Read/write inside `/home/github-runner/`
✅ Call GitHub API with its own runner token (different from your PAT)
✅ Run any tool installed system-wide (`gh`, `git`, `node`, `npm`, `python`)
✅ Make outbound network requests

### What it cannot do

❌ Read `/etc/orclaw/secrets.env` (mode `640 root:engine`)
❌ Use sudo (not in `sudoers`)
❌ Touch `/opt/orclaw/` (mode `755 engine:engine`)
❌ Modify systemd units (no root)
❌ Read `/etc/cloudflared/` or other root-owned config

### Defense in depth

- **Telegram alerts on every transition** — you know the moment a runner
  goes offline or comes back, and on every failed/long/foreign workflow run.
- **No persistent secrets in the runner**: workflow secrets come from
  GitHub Secrets via the runner protocol, never from the filesystem.
- **`_work` directory wiped per job** by actions/runner.
- **Audit logs**: `journalctl -u actions.runner....service` shows every
  workflow run with timing + exit code.

### Increasing isolation (future)

If you want stronger sandboxing, the next step is **ephemeral runners**:
each workflow gets a fresh runner instance that's destroyed afterward.
Two ways:

1. `--ephemeral` flag on `config.sh` — runner exits after one job. You'd
   need a wrapper to re-register it on next start.
2. `actions-runner-controller` (Kubernetes operator) — heavy infrastructure
   for an MVP, but the cleanest model.

For now we accept "persistent runner + monitoring" as adequate for a
private repo with a trusted contributor set of size 1.

## 5. Operating cheatsheet

| Action | Command |
|---|---|
| Status of the runner | `systemctl status actions.runner.jaschez-${TARGET_REPO}.self-hosted-runner-1` |
| Tail runner logs | `journalctl -u actions.runner.jaschez-${TARGET_REPO}.self-hosted-runner-1 -f` |
| Restart the runner | `sudo systemctl restart actions.runner.jaschez-${TARGET_REPO}.self-hosted-runner-1` |
| Force fallback to GH-hosted | `gh api -X PATCH /repos/${TARGET_REPO}/actions/variables/ORCLAW_RUNNER -f value=ubuntu-latest` |
| Force back to self-hosted | `gh api -X PATCH /repos/${TARGET_REPO}/actions/variables/ORCLAW_RUNNER -f value=self-hosted` |
| One-shot monitor pass | `orclaw runner monitor` |
| Unregister runner | `cd /home/github-runner/runner && sudo -u github-runner ./config.sh remove --token <removal-token>` |

The orclaw specialist (MCP server) does NOT expose runner controls
yet — by design. Restarting a runner mid-workflow is destructive, so
that stays manual via SSH.

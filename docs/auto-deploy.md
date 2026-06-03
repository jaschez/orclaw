# Auto-deploy — push-driven, no polling

Every push to `main` on `jaschez/orclaw` triggers an auto-deploy
on the Oracle Cloud VM. Latency: typically 10–30 seconds.

## How it works

```
git push to main
   │ GitHub webhook (instant)
   ▼
GitHub Actions queues a job
   │
   ▼
self-hosted runner (self-hosted-runner-1, already running on the VM)
   │
   ▼
.github/workflows/auto-deploy.yml runs:
   │   sudo /opt/orclaw/infra/scripts/orclaw-deploy.sh
   ▼
orclaw-deploy.sh
   ├─ detect change (HEAD vs origin/main)
   ├─ git pull --ff-only
   ├─ pip install -e '.[specialist]'      (catches new deps)
   ├─ cp infra/systemd/* /etc/systemd/    (sync units)
   ├─ orclaw db migrate --if-exists       (additive schema migrations)
   │     └─ fails → roll back to prev SHA
   ├─ daemon-reload
   ├─ try-restart orclaw-* units            (graceful restart)
   ├─ orclaw doctor                  (smoke test)
   │     ├─ green → Telegram "🚀 deployed <sha>"
   │     └─ red   → git reset --hard <prev SHA>
   │                pip install -e '.[specialist]'
   │                daemon-reload
   │                Telegram "💥 rollback applied"
   └─ exit
```

No timer. No polling. The runner sits idle when nothing is happening
and consumes ~16 MB RAM total.

## One-time setup (must be done before first auto-deploy works)

The runner runs as the `github-runner` user, which has no sudo by default.
The deploy script needs root (to copy systemd units, daemon-reload, etc.),
so we grant it a single narrowly-scoped sudoers entry:

```bash
# On the VM, as a user with sudo:
sudo visudo -f /etc/sudoers.d/orclaw-runner-deploy
```

Paste exactly this:

```
github-runner ALL=(ALL) NOPASSWD: /opt/orclaw/infra/scripts/orclaw-deploy.sh
```

Save + exit. Verify:

```bash
sudo -u github-runner sudo -n /opt/orclaw/infra/scripts/orclaw-deploy.sh --noop 2>&1 | head -3
# Should NOT prompt for password.
```

That's it. After this, every push to main auto-deploys.

> ⚠️ This sudoers entry grants root for EXACTLY one path. If someone
> compromised the `engine` user (which owns `/opt/orclaw/`), they
> could rewrite the script and get root via the runner. We accept this:
> the engine user already has access to the SQLite DB containing
> dispatch state + tokens, so it's not a step up.

## Triggering manually

If the runner was offline during a push, or you just want to re-run:

1. Go to **GitHub → Actions → Auto-deploy → Run workflow** (top-right button).
2. Pick `main` → Run.

The same script runs.

## Watching a deploy live

```bash
# On the VM:
sudo journalctl -u actions.runner.jaschez-orclaw.self-hosted-runner-1.service -f
```

OR check the run on https://github.com/jaschez/orclaw/actions

OR wait for the Telegram alert ("🚀 Engine auto-deployed ...").

## Manual rollback

If somehow auto-rollback didn't fire and you need to revert:

```bash
# On the VM:
cd /opt/orclaw
sudo -u engine git log --oneline -5    # find the SHA you want to go back to
sudo /opt/orclaw/infra/scripts/orclaw-deploy.sh  # NO — this would re-pull
# Instead, hard-reset + force re-install:
sudo -u engine git reset --hard <SHA>
sudo -u engine /opt/orclaw/.venv/bin/pip install -e '.[specialist]'
sudo cp /opt/orclaw/infra/systemd/*.service /etc/systemd/system/
sudo cp /opt/orclaw/infra/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl try-restart orclaw-orchestrator.timer orclaw-batch-planner.timer orclaw-summary.timer orclaw-specialist.service
```

If the bad commit was already in main, also `git revert` it on GitHub
to prevent the next auto-deploy from re-applying it.

## Conversational flow (using the specialist)

The specialist MCP exposes three tools designed exactly for this:

- `propose_engine_change(branch, files, commit_message, pr_title, pr_body)`
  — Creates a branch + commits the file changes + opens a PR. Returns
  the PR URL.

- `view_engine_pr(pr_number)`
  — Shows the PR title/body/state + per-file diff. Use before merging.

- `merge_engine_pr(pr_number, merge_method, confirm=True)`
  — Merges the PR. The `confirm=True` flag is a guardrail — Claude
  must be explicitly told by you to merge.

Example session from your phone:

> **You**: Raise the orchestrator concurrency cap from 2 to 3 in the default config.
>
> **Claude** (specialist):
>   1. Calls `view_file('orclaw/config.py')` to find the line
>   2. Calls `propose_engine_change(branch='chore/raise-cap-to-3', files={'orclaw/config.py': '<new content>'}, commit_message='chore(config): raise max_in_flight to 3', pr_title='chore(config): raise max_in_flight to 3', pr_body='User request from chat')`
>   3. Replies: "Opened PR #42 — github.com/.../pull/42. Review and let me know if you want me to merge it."
>
> **You**: Merge it.
>
> **Claude**:
>   1. Calls `merge_engine_pr(42, confirm=True)`
>   2. Replies: "Merged. Auto-deploy will run in seconds. Telegram alert when complete."
>
> Within ~30s your phone vibrates: "🚀 Engine auto-deployed abc1234 → def5678 · doctor ✓"

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Push to main, no workflow runs | Self-hosted runner offline | `sudo systemctl restart actions.runner...` then re-run workflow |
| Workflow runs but exit code 2 | sudoers entry missing or path mismatch | Re-check `/etc/sudoers.d/orclaw-runner-deploy` exactly matches the path |
| Workflow runs, doctor fails, no rollback | Script crashed before rollback | Manual rollback above |
| Telegram silent on deploy | `TELEGRAM_*` not set in `/etc/orclaw/secrets.env` | Run `orclaw notify telegram 'test'` and check exit |
| Deploys repeatedly fail with `pip install` errors | New deps need extras / native build | SSH in, run `pip install -e '.[specialist]'` manually, fix, push |

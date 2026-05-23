# Deploy quickstart — Oracle Cloud Free Tier

This is the 10-minute path from a fresh Ubuntu 24.04 VM to a running
orchestrator. For background on *why* each piece exists, see
[`deployment.md`](deployment.md).

## Prerequisites

- Oracle Cloud Free Tier account (or any Ubuntu 24.04 host with 1 GB+ RAM)
- A GitHub Personal Access Token (PAT) with `repo` scope, authored as the
  user you want the orchestrator to impersonate (typically the CEO/CTO)
- *(Optional)* Telegram bot token + chat id, Healthchecks.io check URLs

## 1. Create the engine user

On the VM as root (or via Oracle's `cloud-init`):

```bash
adduser --disabled-password --gecos "" engine
usermod -aG sudo engine
# Allow passwordless sudo for the provision script:
echo "engine ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/engine
```

Switch to the engine user:

```bash
sudo -u engine -i
```

## 2. Run the provision script

```bash
curl -fsSL https://raw.githubusercontent.com/jaschez/orclaw/develop/infra/scripts/provision.sh -o /tmp/provision.sh
bash /tmp/provision.sh
```

The script is idempotent — you can re-run it after a `git pull` to pick
up engine updates.

## 3. Configure secrets

```bash
sudo nano /etc/orclaw/secrets.env
```

Required:

```ini
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_REPO=${TARGET_REPO}
```

Optional (engine works without these, just no notifications):

```ini
TELEGRAM_BOT_TOKEN=123456:abc-def
TELEGRAM_CHAT_ID=987654321
HEALTHCHECKS_ORCHESTRATOR_URL=https://hc-ping.com/uuid-for-orch
HEALTHCHECKS_PLANNER_URL=https://hc-ping.com/uuid-for-planner
HEALTHCHECKS_BACKUP_URL=https://hc-ping.com/uuid-for-backup
```

## 4. Sanity check

```bash
/opt/orclaw/.venv/bin/orclaw doctor
```

Every row must show `✓ ok` (notifications can show "disabled" — that's
fine if you skipped step 3's optionals).

## 5. Seed the planner

The orchestrator can't dispatch anything until the planner has computed
at least one batch. Run it once manually:

```bash
sudo -u engine -i
source /etc/orclaw/secrets.env
/opt/orclaw/.venv/bin/orclaw planner run
/opt/orclaw/.venv/bin/orclaw batch show
```

You should see one or more layers with issue numbers in them.

## 6. Enable the timers

```bash
sudo systemctl enable --now \
  orclaw-orchestrator.timer \
  orclaw-batch-planner.timer \
  orclaw-summary.timer
```

| Timer | Cadence | What it does |
|---|---|---|
| `orclaw-orchestrator.timer` | every 30s | One full tick: pollback → reviewer pass → implementer pass |
| `orclaw-batch-planner.timer` | every 10min | Re-scans GitHub issues, recomputes layers |
| `orclaw-summary.timer` | daily at 09:00 | Posts the 24h digest to Telegram |

## 7. Watch it work

```bash
journalctl -u orclaw-orchestrator -f
```

You should see structured JSON logs every 30s. If anything is amiss,
Telegram will get a saturation/error alert.

## Pause / resume

To stop new dispatches without disrupting work already in flight:

```bash
/opt/orclaw/.venv/bin/orclaw pause   # sets engine_state.orchestrator_paused = 'true'
# ...
/opt/orclaw/.venv/bin/orclaw resume
```

To fully stop the engine:

```bash
sudo systemctl stop orclaw-orchestrator.timer
```

The timer's `.service` will finish its current tick (it's `Type=oneshot`),
and no new ticks will fire.

## Cloudflare Tunnel (optional)

If you want the dashboard / specialist accessible at
`orclaw.<YOUR_TEAM>.com`, see [`dashboard-and-integrations.md`](dashboard-and-integrations.md)
for the Tunnel + Zero Trust setup.

## Updating the engine

```bash
sudo -u engine -i
cd /opt/orclaw
git pull --ff-only
source .venv/bin/activate
pip install -q -e .
# Re-copy systemd units in case they changed:
sudo cp infra/systemd/*.service infra/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

The next timer firing picks up the new code automatically.

## Troubleshooting

- **`orclaw doctor` fails on GitHub** → token scope wrong, or repo
  name mistyped. Re-check `GITHUB_TOKEN` and `GITHUB_REPO`.
- **Timer fires but tick exits "no batches in DB"** → run the planner
  manually (step 5).
- **Telegram silent** → run `orclaw summary daily` interactively
  to see if it can reach the API.
- **Healthchecks alerting "late"** → the timer fired but the tick took
  >`grace_period` seconds. Check `journalctl -u orclaw-orchestrator` for
  the slow path.

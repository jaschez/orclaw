# Web dashboard

Live read + write view of the engine, accessible from any browser
behind Cloudflare Access.

## URL

After setup: `https://orclaw.<YOUR_TEAM>.com`

## Sections (all in one scrollable page)

| Section | What it shows | Live? |
|---|---|---|
| **status** | paused/active flag, in-flight count vs cap, batch totals by status | 5s |
| **orchestration** | next-tick decision preview — verdict, layer, issues that would dispatch, PRs awaiting review | 5s |
| **batches** | full batches table, filter by status | 15s |
| **runs** | last 100 runs, filter by agent + status | 10s |
| **events** | structured event log tail — minutes window, level filter, LIKE pattern | 10s |
| **controls** | pause/resume · force tick · run planner · skip issue · force review · require human review | on click |
| **prompts** | edit `prompts/*.md` — save creates a PR which auto-deploys on merge | on click |
| **config** | read-only JSON view of resolved settings (no secrets) | 60s |

Each write action logs to the `events` table and sends a Telegram alert
with `[dashboard]` prefix — visible audit trail of who did what.

## Setup (one-time, ~5 min)

### 1. Install + start the service on the VM

Auto-deploy after sprint 13 lands. Manually if needed:

```bash
# on the VM (engine user):
cd /opt/orclaw
git pull --ff-only
/opt/orclaw/.venv/bin/pip install -e '.[dashboard]'
sudo cp infra/systemd/orclaw-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orclaw-dashboard.service
```

Verify locally:

```bash
curl -sI http://127.0.0.1:8766/ | head -5
# Should return HTTP 200, content-type text/html
```

### 2. Add Cloudflare Tunnel hostname route

In Cloudflare Zero Trust dashboard → **Networks → Tunnels → `orclaw-oracle` → Public Hostname → Add a public hostname**:

| Field | Value |
|---|---|
| Subdomain | `orclaw` |
| Domain | `<YOUR_TEAM>.com` |
| Path | (empty) |
| Type | `HTTP` |
| URL | `localhost:8766` |
| HTTP Settings → HTTP Host Header | `localhost` |

Save.

### 3. Cloudflare Access — self-hosted application (cookie auth, fine for browsers)

**Zero Trust → Access → Applications → Add an application → Self-hosted**:

| Field | Value |
|---|---|
| Application name | `Orclaw Dashboard` |
| Session duration | 24h |
| Application domain | `orclaw.<YOUR_TEAM>.com` (path empty = protect everything) |

Policy:
- Name: `Only owner`
- Action: Allow
- Selector: Emails
- Value: `<YOUR_EMAIL>`

Save.

### 4. First visit

Open `https://orclaw.<YOUR_TEAM>.com` in any browser. Cloudflare
Access prompts for your email → PIN by email → session cookie set for 24h
→ you're in.

## Daily use

- **Mobile**: bookmark the URL. Status banner + controls all fit on one
  scroll.
- **Desktop**: leave it open in a tab. Auto-refreshes per section.
- **Edit a prompt**: navigate to `#prompts`, click a file, edit textarea,
  write a one-liner summary, click `open PR`. You get a GitHub link.
  Merge on GitHub (mobile or desktop), and ~30s later the auto-deploy
  workflow rolls out the new prompt.

## Security model

| Layer | Job |
|---|---|
| Cloudflare Access | Identity gating (one-time PIN to your email) |
| Cloudflare Tunnel | Origin protection (no public ports on the VM) |
| FastAPI app | No auth code — trusts every request that reaches it |
| Telegram | Audit trail on every write action |
| Event log | Persisted record of every action (queryable) |

If your Cloudflare Access cookie leaks: 24h max blast radius, plus
you can rotate the application instantly in the dashboard.

## Troubleshooting

| Symptom | Likely | Fix |
|---|---|---|
| 502 Bad Gateway | `orclaw-dashboard.service` not running | `sudo systemctl status orclaw-dashboard` |
| 421 Invalid Host header | Tunnel not rewriting Host | Set HTTP Host Header to `localhost` in tunnel ingress |
| Stuck on Cloudflare Access login | Access policy too restrictive or email typo | Edit policy in CF dashboard |
| Buttons do nothing | Backend exception — check Network tab | `journalctl -u orclaw-dashboard -f` |
| Prompt save returns "PR creation failed" | GITHUB_TOKEN lacks `repo` scope on orclaw | Regenerate PAT with full `repo` |

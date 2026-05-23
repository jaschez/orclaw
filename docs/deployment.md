# Deployment — Oracle Cloud (Always Free)

Deploy the engine on a free ARM Ampere VPS in Oracle Cloud's Always
Free tier. ~45-60 min of setup the first time (Oracle Cloud is fiddlier
than Hetzner but free forever).

## Why Oracle Cloud

- **Always Free tier**: 4 OCPU ARM Ampere A1 + 24 GB RAM free forever
  (overkill for the engine).
- **Object Storage**: 10 GB free, perfect for SQLite backups
  (<1 MB compressed).
- **Networking**: 10 TB/month outbound free.
- **Known caveats**:
  - Getting an Ampere A1 may take several attempts (high demand in
    some regions).
  - Fiddlier console than Hetzner — you need to set up VCN, subnet,
    security list.
  - Oracle reserves the right to reclaim "idle" instances, but in
    practice it's rare if you actually use it.

## Provisioning

### 1. Oracle Cloud account

1. Create an account at https://www.oracle.com/cloud/free/.
2. Verify email + card (no charges — identity check only).
3. Pick home region — Frankfurt (`eu-frankfurt-1`) or Amsterdam
   (`eu-amsterdam-1`) for EU latency.
4. Wait 5-10 min for the account to activate.

### 2. Create the Ampere A1 VM

1. **Compute → Instances → Create instance**.
2. Configuration:
   - Name: `orclaw`
   - Image: **Canonical Ubuntu 24.04** (Always Free eligible, ARM64).
   - Shape: `VM.Standard.A1.Flex` with **4 OCPU + 24 GB RAM** (the full
     Ampere free tier).
   - If it says "Out of capacity": change region or retry. Sometimes
     you need to insist at different hours.
3. **Networking**: leave the default VCN Oracle auto-creates
   (`vcn-<timestamp>`).
4. **SSH key**: upload your public key (`~/.ssh/id_ed25519.pub` or
   equivalent).
5. **Storage**: 50 GB boot volume (50 is the minimum, free up to 200
   GB total).
6. Click **Create**. Wait ~2 min for provisioning to finish.

Note the **public IP** shown on the instance page.

### 3. Open SSH in the security list

Oracle defaults to allowing SSH only from your IP at create time.
If your IP changes (residential DHCP), adjust:

1. **Networking → Virtual Cloud Networks → your VCN → Security Lists →
   Default Security List**.
2. **Ingress Rules → Add ingress rule**:
   - Source CIDR: `0.0.0.0/0` (or your fixed IP if you have one).
   - IP Protocol: TCP.
   - Destination port: 22.
3. Save.

If you want dashboard access over the IP (not recommended — no TLS),
also open 8080. Better: SSH tunnel — see Dashboard section below.

### 4. Connect

```bash
ssh ubuntu@<ip>
```

Oracle uses `ubuntu` as the default user on the Ubuntu image.

### 5. Base hardening

```bash
sudo apt update && sudo apt upgrade -y

# Create a dedicated user (no root, no ubuntu in production)
sudo adduser engine --gecos "" --disabled-password
sudo usermod -aG sudo engine
sudo mkdir -p /home/engine/.ssh
sudo cp ~/.ssh/authorized_keys /home/engine/.ssh/
sudo chown -R engine:engine /home/engine/.ssh
sudo chmod 700 /home/engine/.ssh
sudo chmod 600 /home/engine/.ssh/authorized_keys

# SSH key-only, no root
sudo sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

# iptables firewall (Oracle Ubuntu ships iptables, no ufw by default).
# Oracle's security list already filters at the cloud level — this is defence-in-depth.
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw --force enable

# Unattended security updates
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

exit
# Reconnect as engine, not ubuntu
ssh engine@<ip>
```

### 6. Base dependencies

```bash
sudo apt install -y \
  python3.11 python3.11-venv python3-pip \
  sqlite3 \
  git \
  curl \
  jq

# gh CLI (official)
sudo mkdir -p -m 755 /etc/apt/keyrings
out=$(mktemp); wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg
sudo install -m 644 $out /etc/apt/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update
sudo apt install -y gh

# OCI CLI (for Object Storage backups)
bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
# Accept the defaults. Afterwards: oci setup config (needs your OCI user OCID, tenancy OCID, key)

# Verify
python3.11 --version  # >= 3.11
sqlite3 --version
gh --version
oci --version
```

### 7. Clone the engine

```bash
sudo mkdir -p /opt/orclaw
sudo chown engine:engine /opt/orclaw

# Auth gh with a PAT (scopes: repo, project, workflow)
gh auth login --with-token < /path/to/token.txt
# Interactive alternative: gh auth login

git clone https://github.com/jaschez/orclaw.git /opt/orclaw
cd /opt/orclaw
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 8. Mirror of `${TARGET_REPO}`

```bash
sudo mkdir -p /var/lib/orclaw
sudo chown engine:engine /var/lib/orclaw
cd /var/lib/orclaw
git clone --depth 50 https://github.com/${TARGET_REPO}.git target-repo-mirror
cd target-repo-mirror
git config core.fsmonitor false
```

### 9. Secrets

```bash
sudo mkdir -p /etc/orclaw
sudo chmod 750 /etc/orclaw
sudo nano /etc/orclaw/secrets.env
```

Contents (reminder: no Anthropic API key needed):

```env
# The server only needs a GitHub PAT that impersonates the repo owner.
# Claude runs via claude.yml in ${TARGET_REPO}, on OAuth Pro (already configured).
GITHUB_TOKEN=ghp_...
GITHUB_REPO=${TARGET_REPO}

# Optional outbound webhooks (no domain needed)
SLACK_WEBHOOK_URL=                              # optional
HEALTHCHECKS_URL=                               # optional, ping on start + every N min
HEALTHCHECKS_BACKUP_URL=                        # optional, ping in backup script

# Oracle Object Storage for backups (alternative to B2/S3)
OCI_OBJECT_STORAGE_NAMESPACE=                   # from: oci os ns get
OCI_OBJECT_STORAGE_BUCKET=orclaw-backups        # bucket name
```

```bash
sudo chmod 640 /etc/orclaw/secrets.env
sudo chown root:engine /etc/orclaw/secrets.env
```

The PAT in `GITHUB_TOKEN` must be a classic token with scopes: `repo`,
`project`, `workflow`. Generate it at https://github.com/settings/tokens.
Recommended expiry: 90 days.

### 10. SQLite DB

```bash
mkdir -p /var/lib/orclaw/data
sqlite3 /var/lib/orclaw/data/engine.db < /opt/orclaw/orchestrator/state/schema.sql
```

### 11. Object Storage bucket in Oracle

From the Oracle Cloud console:

1. **Storage → Buckets → Create Bucket**
2. Name: `orclaw-backups`
3. Storage tier: `Standard`
4. Encryption: managed by Oracle (default)
5. Leave the rest as defaults

Note the namespace (also visible via `oci os ns get` once OCI CLI is
configured).

## Systemd units

Identical to the original plan (provider-agnostic):

- `/etc/systemd/system/orclaw-orchestrator.service` (always-on)
- `/etc/systemd/system/orclaw-batch-planner.{service,timer}` (every 10 min)
- `/etc/systemd/system/orclaw-mirror-sync.{service,timer}` (every 5 min)
- `/etc/systemd/system/orclaw-backup.{service,timer}` (daily at 04:00 UTC)

Unit files live in `/opt/orclaw/infra/systemd/`. Copy them:

```bash
sudo cp /opt/orclaw/infra/systemd/*.service /etc/systemd/system/
sudo cp /opt/orclaw/infra/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  orclaw-orchestrator.service \
  orclaw-batch-planner.timer \
  orclaw-mirror-sync.timer \
  orclaw-backup.timer

sudo systemctl status orclaw-orchestrator
sudo systemctl list-timers | grep orclaw
```

## Status dashboard — Cloudflare Tunnel + Zero Trust

Dashboard access from desktop + mobile via
**`orclaw.<YOUR_TEAM>.com`**, protected by Cloudflare Zero Trust
(email OTP / Google auth). The server opens NO public ports — the
`cloudflared` daemon makes an outbound connection to Cloudflare and
CF forwards authenticated traffic.

### Prerequisites

- A domain `<YOUR_TEAM>.com` with nameservers on Cloudflare (DNS
  managed by CF).
- A Cloudflare account with Zero Trust enabled (free tier up to 50
  users — plenty).

### Tunnel setup

#### 1. In the Cloudflare Zero Trust dashboard

1. https://one.dash.cloudflare.com → Zero Trust dashboard.
2. **Networks → Tunnels → Create a tunnel**.
3. Connector type: `Cloudflared`.
4. Name: `orclaw`.
5. Save. You get a long **tunnel token** (`eyJhIjoi...`). Copy it —
   we'll use it on the server.

#### 2. On the server (Oracle Cloud)

Install `cloudflared`:

```bash
# ARM64 (Ampere A1)
curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb

# Verify
cloudflared --version
```

Set it up as a systemd service with the token:

```bash
# Replace TOKEN_HERE with the one you copied above
sudo cloudflared service install TOKEN_HERE

# Verify
sudo systemctl status cloudflared
```

`cloudflared` now connects to Cloudflare. Back in the Zero Trust
dashboard, the tunnel should show as `HEALTHY`.

#### 3. Public hostname

On the same tunnel screen:

1. **Public Hostname → Add a public hostname**.
2. Subdomain: `orclaw`.
3. Domain: `<YOUR_TEAM>.com`.
4. Service: `HTTP` `localhost:8080`.
5. Save.

CF creates the DNS automatically. `https://orclaw.<YOUR_TEAM>.com`
now points at the tunnel (won't respond yet — Access policy missing).

#### 4. Access policy (Zero Trust auth)

1. **Access → Applications → Add an application**.
2. Type: `Self-hosted`.
3. Application name: `Orclaw Dashboard`.
4. Application domain: `orclaw.<YOUR_TEAM>.com`.
5. Session duration: 24 hours (or whatever you prefer).
6. **Policies → Add a policy**:
   - Name: `Only owner`.
   - Action: `Allow`.
   - Include → Selector `Emails` → your email (`<YOUR_EMAIL>` or
     another).
7. Identity providers: leave `One-time PIN` on (email OTP). Optional:
   add Google.
8. Save.

#### 5. Test

Visit `https://orclaw.<YOUR_TEAM>.com` in your browser. You're
redirected to Cloudflare Access:

1. It asks for your email.
2. Sends you an OTP code.
3. You enter it.
4. Session valid for 24h, you see the dashboard.

Works the same from mobile. **Zero open ports on Oracle**. **Zero auth
on the server**.

### Alternative access (when CF is down or it's just easier)

For quick debug without going through CF:

```bash
# SSH tunnel from your laptop
ssh -L 8080:localhost:8080 engine@<ip>
# Open http://localhost:8080 in your local browser
```

Or pure CLI from the server: `orclaw status`.

### If you lose tunnel access

```bash
# Logs
sudo journalctl -u cloudflared -f

# Reinstall
sudo systemctl restart cloudflared
```

If the token expires or is rotated, generate a new one in the CF
dashboard and run `sudo cloudflared service install NEW_TOKEN`.

## Dashboard access — other modes (fallback)

### Mode A — CLI over SSH (always available)

Zero HTTP infra. The orchestrator exposes data via the `orclaw status`
CLI:

```bash
ssh engine@<ip>
orclaw status

┌──────────────────────────────────────────────────────────┐
│ Orclaw · Status                                          │
│ Uptime: 12d 4h · Last batch: 8 min ago                   │
├──────────────────────────────────────────────────────────┤
│ Active in flight (cap 2):  1                             │
│ Quota observation:         🟢 healthy (0 fails in 10m)   │
│ Mentions last 5h:          18 (15 success, 1 fail)       │
│ Mentions last 24h:         88 (76 success, 89% rate)     │
│                                                          │
│ Current batch (layer 3):                                 │
│   #142 cookie banner    [implementer · queued 32s]       │
│                                                          │
│ Next layer (waiting):                                    │
│   #143 GDPR form  · #145 dashboard KPIs                  │
│                                                          │
│ OPS pending (your hand):                                 │
│   #129 procure legal text                                │
│   #130 Sentry signup                                     │
└──────────────────────────────────────────────────────────┘
```

Other CLI commands:

```bash
orclaw status                    # the view above
orclaw status --watch            # refresh every 5s
orclaw runs list --limit 20      # last @claude runs
orclaw quota show                # detailed quota observation
orclaw specialist                # enter specialist mode (see below)
orclaw pause                     # pause the orchestrator
orclaw resume
```

### Mode B — SSH tunnel (local debug)

If CF is down or you want a bypass for debugging:

```bash
ssh -L 8080:localhost:8080 engine@<ip>
# Open http://localhost:8080 in your local browser
```

## External integrations (no domain needed)

All **outbound only**, no public IP with TLS required:

| Integration | What for | How |
|---|---|---|
| **Slack/Discord webhook** | Saturation alerts, hard-stops, errors | Webhook URL → `SLACK_WEBHOOK_URL` env var |
| **Healthchecks.io** | Detect if the orchestrator stops pinging (server down) | Check URL → `HEALTHCHECKS_URL` env var |
| **Email** | Optional daily reports | SMTP via Resend / Mailgun / Gmail App Password |
| **Telegram** | Phone notifications without an extra app | Bot token + chat ID |

Zero exposed surface. Zero TLS to maintain. Zero DNS to configure.

## Backup with Oracle Object Storage

`/opt/orclaw/infra/scripts/backup.sh` (Oracle version):

```bash
#!/usr/bin/env bash
set -euo pipefail

DB=/var/lib/orclaw/data/engine.db
STAMP=$(date -u +%Y%m%d-%H%M%S)
TMP=/tmp/engine-backup-$STAMP.sqlite
GZIP=$TMP.gz

# Hot backup (safe while the orchestrator is writing)
sqlite3 "$DB" ".backup '$TMP'"
gzip -9 "$TMP"

# Upload to Oracle Object Storage
oci os object put \
  --namespace-name "$OCI_OBJECT_STORAGE_NAMESPACE" \
  --bucket-name "$OCI_OBJECT_STORAGE_BUCKET" \
  --name "engine-db/$(date -u +%Y/%m)/engine-backup-$STAMP.sqlite.gz" \
  --file "$GZIP" \
  --content-encoding gzip \
  >/dev/null

# Keep local 7 days
find /tmp/engine-backup-*.sqlite.gz -mtime +7 -delete 2>/dev/null || true

# Health ping (optional)
[ -n "${HEALTHCHECKS_BACKUP_URL:-}" ] && curl -fsS -m 10 --retry 3 "$HEALTHCHECKS_BACKUP_URL" >/dev/null || true

echo "Backup uploaded: engine-db/$(date -u +%Y/%m)/engine-backup-$STAMP.sqlite.gz"
```

OCI CLI setup (one-time, follow the interactive prompts):

```bash
oci setup config
# It'll ask for: user OCID, tenancy OCID, region, generate new API key pair
# Find the user OCID in the console: Profile → User Settings → OCID
# Tenancy OCID: Profile → Tenancy → OCID

# After setup, upload the generated public key to your user in the Oracle console:
# Profile → User Settings → Add API Key → Paste public key

# Verify
oci os ns get
```

## Logs and diagnostics

```bash
# Live tail of the orchestrator
sudo journalctl -u orclaw-orchestrator -f

# Last hour of all orclaw-*
sudo journalctl --since "1 hour ago" -u 'orclaw-*'

# Current state
sudo systemctl status 'orclaw-*'

# Inspect SQLite
sqlite3 /var/lib/orclaw/data/engine.db
> .tables
> SELECT * FROM batches WHERE status='in_progress';
> SELECT agent, status, COUNT(*) FROM runs
  WHERE started_at >= date('now', 'start of day')
  GROUP BY agent, status;
```

## Update workflow

No real downtime (orchestrator restarts in ~5s):

```bash
ssh engine@<ip>
cd /opt/orclaw
git pull
source .venv/bin/activate
pip install -r requirements.txt   # if there are new deps
sudo systemctl restart orclaw-orchestrator
```

## Monthly cost

| Item | Cost |
|---|---|
| Oracle Cloud Always Free (VM Ampere A1 4 OCPU 24 GB) | **$0** |
| Oracle Cloud Object Storage (10 GB free) | **$0** |
| Networking (10 TB egress free) | **$0** |
| Anthropic API tokens | **$0** (Pro plan already paid, no extras) |
| **Monthly total** | **$0** |

## When to migrate off Oracle Free

If at some point:

- The Ampere A1 gets reclaimed for being idle (rare but it happens).
- You need more resources (>24 GB RAM, >4 OCPU).
- You're tired of the Oracle console.

→ Migrating to Hetzner CX22 (~$5/month) is trivial: rerun this
document against a Hetzner VM. The engine is portable.

## Disaster recovery

If the VM blows up or Oracle reclaims it:

1. Create a new Ampere A1 instance (5-15 min).
2. Rerun this document from step 4 (~30 min).
3. Restore `engine.db` from the latest backup in Object Storage (1 min):
   ```bash
   oci os object get --namespace-name "$NS" --bucket-name "$BUCKET" \
     --name "engine-db/2026/05/engine-backup-XXX.sqlite.gz" --file /tmp/restore.gz
   gunzip /tmp/restore.gz
   mv /tmp/restore /var/lib/orclaw/data/engine.db
   ```
4. Restart services (1 min).
5. Engine picks up where it left off. The only thing lost: runs in
   flight at the moment of the crash (the next planner reclassifies
   them as `pending`).

#!/usr/bin/env bash
# Orclaw installer — provisions a fresh Ubuntu 24.04 host end-to-end.
#
#   curl -fsSL https://raw.githubusercontent.com/${GITHUB_USERNAME}/orclaw/main/install.sh | bash
#
# What it does (idempotent — safe to re-run):
#   1.  Validates the host (Ubuntu 24.04 LTS, root or sudo).
#   2.  Installs OS deps (python3.12, git, unzip, jq, cloudflared, deno).
#   3.  Creates the `orclaw` system user.
#   4.  Clones the repo to /opt/orclaw and creates a venv with deps.
#   5.  Lays out /etc/orclaw + /var/lib/orclaw with correct perms.
#   6.  Runs an interactive wizard to populate /etc/orclaw/secrets.env.
#   7.  Installs + enables systemd units (orchestrator, planner, dashboard).
#   8.  Prints next-steps for: GitHub Actions self-hosted runner, Cloudflare
#       Tunnel, Telegram chat ID lookup.
#
# Flags:
#   --non-interactive   skip the wizard; expects /etc/orclaw/secrets.env to
#                       already exist (useful for cloud-init / Proxmox).
#   --branch <name>     install from a non-main branch.
#   --skip-systemd      don't install systemd units (CI / local-dev).
#
# Exit codes:
#   0 success · 1 user error · 2 environment error · 3 install failure.

set -euo pipefail

# ---------- pretty output ----------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { printf "${CYAN}[orclaw]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}[orclaw]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[orclaw]${NC} %s\n" "$*"; }
err()  { printf "${RED}[orclaw]${NC} %s\n" "$*" >&2; }
die()  { err "$*"; exit "${2:-1}"; }

# ---------- defaults + flags -------------------------------------------------
ORCLAW_USER="orclaw"
INSTALL_DIR="/opt/orclaw"
CONFIG_DIR="/etc/orclaw"
DATA_DIR="/var/lib/orclaw"
REPO_URL="https://github.com/${GITHUB_USERNAME}/orclaw.git"
BRANCH="main"
INTERACTIVE=1
INSTALL_SYSTEMD=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) INTERACTIVE=0; shift ;;
    --branch)          BRANCH="$2"; shift 2 ;;
    --skip-systemd)    INSTALL_SYSTEMD=0; shift ;;
    --help|-h)         sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown flag: $1" 1 ;;
  esac
done

# ---------- 1. preflight -----------------------------------------------------
log "preflight check"

if [[ $EUID -ne 0 ]]; then
  die "must be run as root (try: sudo bash install.sh)" 2
fi

if ! command -v lsb_release >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq lsb-release
fi

OS_ID=$(lsb_release -is 2>/dev/null || echo unknown)
OS_VER=$(lsb_release -rs 2>/dev/null || echo unknown)
if [[ "$OS_ID" != "Ubuntu" ]] || [[ ! "$OS_VER" =~ ^(22\.04|24\.04)$ ]]; then
  warn "untested on $OS_ID $OS_VER — proceeding anyway (file an issue if it works/breaks)"
fi

# ---------- 2. OS deps -------------------------------------------------------
log "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3.12 python3.12-venv python3.12-dev \
  git curl unzip jq ca-certificates build-essential \
  sqlite3 systemd

# cloudflared (for tunnel — optional but assumed)
if ! command -v cloudflared >/dev/null 2>&1; then
  log "installing cloudflared"
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    | tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  apt-get update -qq && apt-get install -y -qq cloudflared
fi

# Deno (needed by claude-code-action when CI runs Deno-based edge tests).
if ! command -v deno >/dev/null 2>&1; then
  log "installing deno (system-wide)"
  TMP_DENO=$(mktemp -d)
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$TMP_DENO" sh -s -- -y >/dev/null
  install -m 0755 "$TMP_DENO/bin/deno" /usr/local/bin/deno
  rm -rf "$TMP_DENO"
fi
ok "system deps ready · python=$(python3.12 --version | cut -d' ' -f2) · deno=$(deno --version | head -1 | awk '{print $2}')"

# ---------- 3. user ----------------------------------------------------------
if ! id -u "$ORCLAW_USER" >/dev/null 2>&1; then
  log "creating system user '$ORCLAW_USER'"
  useradd --system --create-home --shell /bin/bash --user-group "$ORCLAW_USER"
fi
# Passwordless sudo for orclaw — required by the overlay layer (sudo systemctl
# daemon-reload, sudo cp to /etc/systemd/system, etc.).
cat > /etc/sudoers.d/orclaw <<EOF
$ORCLAW_USER ALL=(ALL) NOPASSWD: ALL
EOF
chmod 0440 /etc/sudoers.d/orclaw
ok "user '$ORCLAW_USER' ready"

# ---------- 4. clone + venv --------------------------------------------------
if [[ -d "$INSTALL_DIR/.git" ]]; then
  log "updating existing checkout at $INSTALL_DIR"
  sudo -u "$ORCLAW_USER" git -C "$INSTALL_DIR" fetch --quiet origin
  sudo -u "$ORCLAW_USER" git -C "$INSTALL_DIR" checkout "$BRANCH"
  sudo -u "$ORCLAW_USER" git -C "$INSTALL_DIR" pull --ff-only
else
  log "cloning $REPO_URL → $INSTALL_DIR (branch=$BRANCH)"
  mkdir -p "$INSTALL_DIR"
  chown "$ORCLAW_USER:$ORCLAW_USER" "$INSTALL_DIR"
  sudo -u "$ORCLAW_USER" git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
  log "creating venv"
  sudo -u "$ORCLAW_USER" python3.12 -m venv "$INSTALL_DIR/.venv"
fi
log "pip install -e .[specialist,dashboard]"
sudo -u "$ORCLAW_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$ORCLAW_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR[specialist,dashboard]"
ok "orclaw installed at $INSTALL_DIR"

# ---------- 5. dirs + perms --------------------------------------------------
log "setting up $CONFIG_DIR and $DATA_DIR"
install -d -m 0750 -o root -g "$ORCLAW_USER" "$CONFIG_DIR"
install -d -m 0755 -o "$ORCLAW_USER" -g "$ORCLAW_USER" "$DATA_DIR"
install -d -m 0755 -o "$ORCLAW_USER" -g "$ORCLAW_USER" "$DATA_DIR/overrides/prompts"
install -d -m 0755 -o "$ORCLAW_USER" -g "$ORCLAW_USER" "$DATA_DIR/overrides/systemd"

# ---------- 6. interactive wizard --------------------------------------------
SECRETS_FILE="$CONFIG_DIR/secrets.env"
if [[ ! -f "$SECRETS_FILE" ]]; then
  if [[ "$INTERACTIVE" == "1" ]]; then
    log "configuration wizard (answers go to $SECRETS_FILE)"
    echo
    read -rp "  Target GitHub repo (owner/name): " TARGET_REPO
    read -rsp "  GitHub PAT (repo scope) — won't echo: " GH_TOKEN; echo
    read -rp "  Telegram bot token (leave empty to skip Telegram): " TG_TOKEN
    if [[ -n "$TG_TOKEN" ]]; then
      read -rp "  Your Telegram numeric chat ID: " TG_CHAT
    fi
    read -rp "  Max concurrent @claude dispatches [2]: " CAP
    CAP="${CAP:-2}"
    cat > "$SECRETS_FILE" <<EOF
# Generated by orclaw install.sh on $(date -Iseconds)
ORCLAW_GITHUB_REPO=$TARGET_REPO
GITHUB_TOKEN=$GH_TOKEN
ORCLAW_CONCURRENCY_MAX_IN_FLIGHT=$CAP
ORCLAW_CONCURRENCY_DEFAULT_IN_FLIGHT=$CAP
ORCLAW_TELEGRAM_BOT_TOKEN=$TG_TOKEN
ORCLAW_TELEGRAM_CHAT_ID=${TG_CHAT:-}
ORCLAW_DATA_DIR=$DATA_DIR
ORCLAW_DB_PATH=$DATA_DIR/orclaw.db
ORCLAW_LOG_LEVEL=INFO
ORCLAW_EVENTS_TO_DB=1
EOF
    chown root:"$ORCLAW_USER" "$SECRETS_FILE"
    chmod 0640 "$SECRETS_FILE"
    ok "secrets written to $SECRETS_FILE"
  else
    warn "non-interactive mode + no $SECRETS_FILE — copy .env.example and edit before starting services"
    cp "$INSTALL_DIR/.env.example" "$SECRETS_FILE"
    chown root:"$ORCLAW_USER" "$SECRETS_FILE"
    chmod 0640 "$SECRETS_FILE"
  fi
else
  ok "$SECRETS_FILE already exists — leaving it"
fi

# ---------- 7. systemd -------------------------------------------------------
if [[ "$INSTALL_SYSTEMD" == "1" ]]; then
  log "installing systemd units"
  cp "$INSTALL_DIR"/infra/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
  cp "$INSTALL_DIR"/infra/systemd/*.timer   /etc/systemd/system/ 2>/dev/null || true
  systemctl daemon-reload
  systemctl enable --now \
    orclaw-orchestrator.timer \
    orclaw-batch-planner.timer \
    orclaw-summary.timer \
    orclaw-dashboard.service \
    >/dev/null 2>&1 || warn "some units failed to enable (check 'systemctl status orclaw-*')"

  # Bot only if both token and chat ID are populated.
  if grep -q "^ORCLAW_TELEGRAM_BOT_TOKEN=." "$SECRETS_FILE" && \
     grep -q "^ORCLAW_TELEGRAM_CHAT_ID=." "$SECRETS_FILE"; then
    systemctl enable --now orclaw-telegram-bot.service >/dev/null 2>&1 \
      && ok "telegram bot enabled" \
      || warn "telegram bot failed to start (check journalctl -u orclaw-telegram-bot)"
  fi
  ok "systemd units enabled"
fi

# ---------- 8. doctor --------------------------------------------------------
log "running orclaw doctor"
if sudo -u "$ORCLAW_USER" bash -c "set -a; source $SECRETS_FILE; set +a; $INSTALL_DIR/.venv/bin/orclaw doctor"; then
  ok "doctor ✓"
else
  warn "doctor reported issues — see output above"
fi

# ---------- next steps -------------------------------------------------------
cat <<EOF

${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
${GREEN}  Orclaw installed.${NC}

  Next steps:

  1. ${CYAN}Register a GitHub Actions self-hosted runner${NC} in your target repo:
       Settings → Actions → Runners → New self-hosted runner → Linux ARM/x64
     The Pro-plan billing only works when the @claude action runs on a
     runner you own. Run the registration script as the 'github-runner'
     user (it'll be created on first registration).

  2. ${CYAN}Wire claude-code-action${NC} in your target repo (if not already):
       https://github.com/anthropics/claude-code-action

  3. ${CYAN}Set up Cloudflare Tunnel${NC} to expose the dashboard:
       cloudflared tunnel login
       cloudflared tunnel create orclaw
       # then point a hostname at http://127.0.0.1:8766 with Access policy
     Full guide: /opt/orclaw/docs/dashboard.md

  4. ${CYAN}Verify everything is up:${NC}
       systemctl status orclaw-orchestrator.timer
       systemctl status orclaw-dashboard.service
       curl -s http://127.0.0.1:8766/api/status | jq

  5. ${CYAN}Drop your first issues${NC} in $TARGET_REPO and watch them get
     picked up on the next 30s tick. Layer dependencies via "Depends on #N"
     in the issue body — the planner auto-discovers the DAG.

  Tail logs:  journalctl -fu orclaw-orchestrator.service
  Dashboard:  http://127.0.0.1:8766  (front it with cloudflared)
  Docs:       /opt/orclaw/docs/
${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}
EOF

#!/usr/bin/env bash
# Provision script — run on a fresh Ubuntu 24.04 VPS as user `engine`.
# Idempotent. See docs/deploy-quickstart.md for the full walkthrough.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/${GITHUB_USERNAME}/orclaw.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/orclaw}"
DATA_DIR="${DATA_DIR:-/var/lib/orclaw}"
MIRROR_REPO="${MIRROR_REPO:-https://github.com/${TARGET_REPO}.git}"

# Pick the newest CPython >= 3.11 that the host has, unless overridden.
# Order matters: we prefer the highest available so Ubuntu 24.04+ users
# don't need to think about it (24.04 ships 3.12, 22.04 ships 3.10 + can
# install 3.11/3.12 from deadsnakes).
detect_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
      # Confirm it's >= 3.11
      version_ok=$("$candidate" -c 'import sys; print(int(sys.version_info >= (3,11)))' 2>/dev/null || echo 0)
      if [ "$version_ok" = "1" ]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON="${PYTHON:-$(detect_python)}"
if [ -z "$PYTHON" ]; then
  echo "ERROR: no python3.11+ found. Install with:" >&2
  echo "  sudo apt install -y python3.12 python3.12-venv" >&2
  exit 1
fi
echo "==> Using Python: $($PYTHON --version) at $(command -v "$PYTHON")"

echo "==> Sanity"
[ "$EUID" -eq 0 ] && { echo "Run as non-root user 'engine', not as root" >&2; exit 1; }

echo "==> APT packages"
sudo apt update -qq
# Install python + venv for the detected interpreter. Pip is provided by
# the venv module; we install python3-pip as a fallback for hosts where
# ensurepip is missing.
PYTHON_PKG=$(basename "$PYTHON")
sudo apt install -y -qq \
  "$PYTHON_PKG" "${PYTHON_PKG}-venv" python3-pip \
  sqlite3 git curl jq

echo "==> gh CLI (used by humans, not by the engine itself)"
if ! command -v gh &>/dev/null; then
  sudo mkdir -p -m 755 /etc/apt/keyrings
  out=$(mktemp)
  wget -qO "$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg
  sudo install -m 644 "$out" /etc/apt/keyrings/githubcli-archive-keyring.gpg
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
  sudo apt update -qq
  sudo apt install -y -qq gh
fi

echo "==> Engine repo"
if [ ! -d "$INSTALL_DIR/.git" ]; then
  sudo mkdir -p "$INSTALL_DIR"
  sudo chown "$USER:$USER" "$INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
# git pull is best-effort: on a brand-new VM the credential helper may
# not be configured yet (we recommend `gh auth login` for that), and an
# auth prompt would hang the script. If the pull fails we just continue
# with the existing checkout — which is fine for the initial provision.
if ! GIT_TERMINAL_PROMPT=0 git pull --ff-only 2>&1; then
  echo "  (skipped git pull — auth not configured? Using existing checkout."
  echo "   Run \`gh auth login\` later to enable in-place updates.)"
fi

echo "==> Python venv + editable install"
if [ ! -d ".venv" ]; then
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
# Editable install picks up the `orclaw` console script via
# pyproject.toml's [project.scripts]. .venv/bin/orclaw is what
# the systemd units invoke.
pip install -q -e '.[specialist,dashboard]'

echo "==> Data dirs"
sudo mkdir -p "$DATA_DIR/data" "$DATA_DIR/worktrees" "$DATA_DIR/backups"
sudo chown -R "$USER:$USER" "$DATA_DIR"

echo "==> ${TARGET_REPO} mirror (optional — only used by the implementer agent)"
if [ ! -d "$DATA_DIR/${TARGET_REPO}-mirror/.git" ]; then
  git clone --depth 50 "$MIRROR_REPO" "$DATA_DIR/${TARGET_REPO}-mirror" || \
    echo "  (couldn't clone mirror — non-fatal; agents will clone on demand)"
fi

echo "==> Secrets dir"
sudo mkdir -p /etc/orclaw
# Group-readable so the engine user can `source` it interactively; world
# stays blocked. Owner is root so only sudo edits it.
sudo chown root:"$USER" /etc/orclaw
sudo chmod 750 /etc/orclaw
if [ ! -f /etc/orclaw/secrets.env ]; then
  sudo tee /etc/orclaw/secrets.env > /dev/null <<'EOF'
# orclaw secrets — fill in before enabling systemd units.
# Required for orchestrator + planner:
GITHUB_TOKEN=
GITHUB_REPO=${TARGET_REPO}

# Optional observability (engine runs fine without these):
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
HEALTHCHECKS_ORCHESTRATOR_URL=
HEALTHCHECKS_PLANNER_URL=
HEALTHCHECKS_BACKUP_URL=

# Engine knobs (defaults shown):
ORCLAW_LOG_LEVEL=INFO
ORCLAW_MAX_IN_FLIGHT=1

# Tap structlog events into SQLite for incident analysis. Comment out
# to disable (zero overhead). Inspect via `orclaw specialist`
# query_events tool or `sqlite3 ... SELECT * FROM events`.
ORCLAW_LOG_EVENTS_DB=/var/lib/orclaw/data/engine.db
EOF
  echo "  Created /etc/orclaw/secrets.env — edit and add GITHUB_TOKEN before enabling units."
fi
# Make sure permissions are right whether the file was just created or
# already existed from a previous (failed) provision run:
sudo chown root:"$USER" /etc/orclaw/secrets.env
sudo chmod 640 /etc/orclaw/secrets.env

echo "==> SQLite init"
DB="$DATA_DIR/data/engine.db"
if [ ! -f "$DB" ]; then
  sqlite3 "$DB" < "$INSTALL_DIR/orchestrator/state/schema.sql"
fi

echo "==> Systemd units"
sudo cp "$INSTALL_DIR/infra/systemd/"*.service /etc/systemd/system/
sudo cp "$INSTALL_DIR/infra/systemd/"*.timer /etc/systemd/system/
sudo systemctl daemon-reload

echo "==> Doctor (skipped if GITHUB_TOKEN not set yet)"
# Read-test first: if for any reason the file isn't readable, skip with
# a hint rather than aborting (set -e would otherwise kill the script).
if ! [ -r /etc/orclaw/secrets.env ]; then
  echo "  Skipping — /etc/orclaw/secrets.env not readable by $USER."
  echo "  Run: sudo chmod 640 /etc/orclaw/secrets.env"
  echo "       sudo chown root:$USER /etc/orclaw/secrets.env"
elif grep -q "^GITHUB_TOKEN=$" /etc/orclaw/secrets.env; then
  echo "  Skipping — GITHUB_TOKEN empty. Set it then run: /opt/orclaw/.venv/bin/orclaw doctor"
else
  # shellcheck disable=SC1091
  set -a; source /etc/orclaw/secrets.env; set +a
  /opt/orclaw/.venv/bin/orclaw doctor || true
fi

echo
echo "===================================================="
echo "Provision complete."
echo
echo "Next steps:"
echo "  1. sudo nano /etc/orclaw/secrets.env"
echo "     (fill GITHUB_TOKEN; optionally TELEGRAM_* + HEALTHCHECKS_*)"
echo
echo "  2. /opt/orclaw/.venv/bin/orclaw doctor"
echo "     (must show all green ✓)"
echo
echo "  3. sudo systemctl enable --now \\"
echo "       orclaw-orchestrator.timer \\"
echo "       orclaw-batch-planner.timer \\"
echo "       orclaw-summary.timer"
echo
echo "  4. journalctl -u orclaw-orchestrator -f"
echo "===================================================="

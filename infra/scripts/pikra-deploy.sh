#!/usr/bin/env bash
# orclaw-deploy.sh — idempotent auto-deploy of orclaw on the VM.
#
# Invoked by `.github/workflows/auto-deploy.yml` running on the
# self-hosted runner. Triggered by every push to main.
#
# Runs as root (via sudoers NOPASSWD entry — see docs/auto-deploy.md).
# Switches to the `engine` user for git + pip operations so file
# ownership stays correct.
#
# Flow:
#   1. Compare HEAD vs origin/main; no-op if equal.
#   2. Save current SHA for rollback.
#   3. git pull --ff-only.
#   4. pip install -e '.[specialist]' (catches new deps).
#   5. Copy any new/modified systemd units; daemon-reload.
#   6. try-restart all orclaw-* units (in-place restart, no downtime).
#   7. Run `orclaw doctor` to verify the new state.
#   8a. Success → Telegram alert with new SHA.
#   8b. Failure → git reset --hard previous SHA + reinstall + Telegram alert.
#
# Logs to journalctl (the workflow captures stdout to GitHub Actions UI too).

set -euo pipefail

INSTALL_DIR=/opt/orclaw
ENGINE_USER=engine

log() { echo "[$(date -Iseconds)] $*"; }

# --- 0. sanity ---
if [ "$(id -u)" -ne 0 ]; then
  log "ERROR: must run as root (the github-runner uses sudoers NOPASSWD for this)"
  exit 2
fi
if [ ! -d "$INSTALL_DIR/.git" ]; then
  log "ERROR: $INSTALL_DIR is not a git checkout"
  exit 2
fi

cd "$INSTALL_DIR"

# --- 1. detect change ---
PREV_SHA=$(sudo -u "$ENGINE_USER" git rev-parse HEAD)
sudo -u "$ENGINE_USER" git fetch --quiet origin main
NEW_SHA=$(sudo -u "$ENGINE_USER" git rev-parse origin/main)
log "previous=$PREV_SHA"
log "incoming=$NEW_SHA"

if [ "$PREV_SHA" = "$NEW_SHA" ]; then
  log "no new commits — nothing to do"
  exit 0
fi

# --- helper: best-effort telegram notify (never aborts the deploy) ---
notify() {
  local msg="$1"
  sudo -u "$ENGINE_USER" bash -c "
    set -a; source /etc/orclaw/secrets.env 2>/dev/null; set +a
    /opt/orclaw/.venv/bin/orclaw notify telegram '$msg' 2>&1 || true
  "
}

# --- 2. apply ---
log "applying pull"
sudo -u "$ENGINE_USER" git pull --ff-only

log "pip install -e (in case deps changed)"
sudo -u "$ENGINE_USER" /opt/orclaw/.venv/bin/pip install --quiet -e '.[specialist,dashboard]' || {
  log "pip install failed — rolling back"
  sudo -u "$ENGINE_USER" git reset --hard "$PREV_SHA"
  sudo -u "$ENGINE_USER" /opt/orclaw/.venv/bin/pip install --quiet -e '.[specialist,dashboard]'
  notify "<b>💥 Auto-deploy ROLLBACK</b> pip install failed for ${NEW_SHA:0:7}; reverted to ${PREV_SHA:0:7}"
  exit 1
}

log "syncing systemd units"
cp infra/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
cp infra/systemd/*.timer /etc/systemd/system/ 2>/dev/null || true

# Re-apply dashboard-managed overrides AFTER the deploy copies the
# shipped versions, so user overrides win again. Without this, every
# auto-deploy would silently roll back the user's dashboard edits.
# Overrides live under /var/lib/orclaw/overrides/systemd/ —
# created on demand by the overlay layer when the user saves a unit.
OVERLAY_SYSTEMD=/var/lib/orclaw/overrides/systemd
if [ -d "$OVERLAY_SYSTEMD" ]; then
  for f in "$OVERLAY_SYSTEMD"/*.timer "$OVERLAY_SYSTEMD"/*.service; do
    [ -e "$f" ] || continue
    cp -f "$f" "/etc/systemd/system/$(basename "$f")"
    chmod 644 "/etc/systemd/system/$(basename "$f")"
    log "  re-applied overlay $(basename "$f")"
  done
fi

systemctl daemon-reload

log "restarting orclaw-* units (try-restart, gentle)"
for unit in \
  orclaw-orchestrator.timer \
  orclaw-batch-planner.timer \
  orclaw-summary.timer \
  orclaw-runner-monitor.timer \
  orclaw-events-purge.timer \
  orclaw-specialist.service \
  orclaw-dashboard.service ; do
  systemctl try-restart "$unit" 2>/dev/null && log "  restarted $unit" || log "  skipped $unit (not loaded)"
done

# --- 3. verify ---
log "running orclaw doctor"
if ! sudo -u "$ENGINE_USER" bash -c "
  set -a; source /etc/orclaw/secrets.env; set +a
  /opt/orclaw/.venv/bin/orclaw doctor
"; then
  log "DOCTOR FAILED — rolling back to $PREV_SHA"
  sudo -u "$ENGINE_USER" git reset --hard "$PREV_SHA"
  sudo -u "$ENGINE_USER" /opt/orclaw/.venv/bin/pip install --quiet -e '.[specialist]'
  cp infra/systemd/*.service /etc/systemd/system/ 2>/dev/null || true
  cp infra/systemd/*.timer /etc/systemd/system/ 2>/dev/null || true
  systemctl daemon-reload
  notify "<b>💥 Auto-deploy ROLLBACK</b> doctor failed for ${NEW_SHA:0:7}; reverted to ${PREV_SHA:0:7}"
  exit 1
fi

log "deploy OK: ${PREV_SHA:0:7} -> ${NEW_SHA:0:7}"
notify "<b>🚀 Engine auto-deployed</b> ${PREV_SHA:0:7} → ${NEW_SHA:0:7} · doctor ✓"

#!/usr/bin/env bash
# Daily SQLite backup. Run by orclaw-backup.timer.
# Requires: rclone OR b2 CLI configured under user 'engine'. Pick one.

set -euo pipefail

DB="/var/lib/orclaw/data/engine.db"
STAMP=$(date -u +%Y%m%d-%H%M%S)
TMP="/tmp/engine-backup-$STAMP.sqlite"
GZIP="$TMP.gz"

# Hot backup (uses SQLite online backup, safe while orchestrator writes)
sqlite3 "$DB" ".backup '$TMP'"
gzip -9 "$TMP"

# Upload (uncomment whichever you set up)
# b2 upload-file orclaw-backups "$GZIP" "engine-db/$(date -u +%Y/%m)/engine-backup-$STAMP.sqlite.gz"
# rclone copy "$GZIP" remote:orclaw-backups/engine-db/$(date -u +%Y/%m)/

# Retain local 7 days
find /tmp/engine-backup-*.sqlite.gz -mtime +7 -delete 2>/dev/null || true

# Healthcheck ping
[ -n "${HEALTHCHECKS_BACKUP_URL:-}" ] && curl -fsS -m 10 --retry 3 "$HEALTHCHECKS_BACKUP_URL" >/dev/null || true

echo "Backup $GZIP completed."

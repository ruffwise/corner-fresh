#!/usr/bin/env bash
# Nightly: online SQLite backup + restore test, then push backups and new
# archive files off the box (if RCLONE_REMOTE is set in secrets.env).
set -euo pipefail
cd /opt/corners/app
hc() { [ -n "${HC_BACKUP:-}" ] && curl -fsS -m 10 "${HC_BACKUP}$1" >/dev/null || true; }
hc /start
if ! python3 -m collector.backup --config /opt/corners/config.toml; then hc /fail; exit 1; fi
if [ -n "${RCLONE_REMOTE:-}" ]; then
  # --immutable: an archive file that already exists remotely must never change.
  rclone copy /opt/corners/data/backups "${RCLONE_REMOTE}/backups" --max-age 48h || { hc /fail; exit 1; }
  rclone copy /opt/corners/data/archive "${RCLONE_REMOTE}/archive" --immutable || { hc /fail; exit 1; }
fi
hc ""

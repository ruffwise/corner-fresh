#!/usr/bin/env bash
# Nightly (02:20 UTC): online SQLite backup + restore test, then copy the backups
# and the raw archive off the box when RCLONE_REMOTE is set in secrets.env.
#
# Off-box rules:
#   - copy only, never sync: nothing is ever deleted remotely by this script;
#   - --immutable: a file that already exists remotely must never change;
#   - after the copy, `rclone check --one-way` must find every local archive file
#     that existed when the copy started on the remote, same size, or the run fails
#     (upload integrity itself is checked by rclone's hash comparison during copy);
#   - the result is recorded in data/backups/last_offbox.json for the daily
#     report and the watchdog, and sent to healthchecks (HC_BACKUP) with a
#     one-line reason on failure.
set -euo pipefail
cd /opt/corners/app
CFG=/opt/corners/app/config/production.toml
export RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/corners/rclone.conf}"
export XDG_CACHE_HOME=/opt/corners/data/.cache
DATA=/opt/corners/data

hc() {   # hc <suffix> [message]: POST so the message shows in the healthchecks event log
  [ -n "${HC_BACKUP:-}" ] || return 0
  curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "${2:-}" "${HC_BACKUP}$1" || true
}
hc /start

if ! out=$(python3 -m collector.backup --config "$CFG" 2>&1); then
  echo "$out"; hc /fail "local backup or restore test failed: ${out: -800}"; exit 1
fi
echo "$out"

if [ -z "${RCLONE_REMOTE:-}" ]; then
  hc "" "local backup ok; off-box copy NOT configured (RCLONE_REMOTE empty)"
  exit 0
fi

offbox_fail() {
  echo "off-box: $1" >&2
  python3 -m collector.backup --config "$CFG" --offbox-result failed --detail "$1" || true
  hc /fail "off-box copy failed: $1"
  exit 1
}
command -v rclone >/dev/null || offbox_fail "rclone not installed"

START=$(date +%s)
rclone copy "$DATA/backups" "$RCLONE_REMOTE/backups" --include 'corners-*.db.gz' --max-age 72h \
  --immutable --stats-one-line --stats 0 -v || offbox_fail "backups copy (rclone exit $?)"
rclone copy "$DATA/archive" "$RCLONE_REMOTE/archive" --exclude '*.tmp' \
  --immutable --stats-one-line --stats 0 -v || offbox_fail "archive copy (rclone exit $?)"
age=$(( $(date +%s) - START + 1 ))
rclone check "$DATA/archive" "$RCLONE_REMOTE/archive" --one-way --size-only --exclude '*.tmp' \
  --min-age "${age}s" --log-level ERROR || offbox_fail "check: local archive files missing or different remotely"
remote_n=$(rclone lsf -R --files-only --include '*.json.gz' "$RCLONE_REMOTE/archive" | wc -l) \
  || offbox_fail "remote listing"

if ! res=$(python3 -m collector.backup --config "$CFG" --offbox-result ok --remote-archive-files "$remote_n" \
             --copy-started "$START"); then
  echo "$res"; hc /fail "off-box incomplete: $res"; exit 1
fi
echo "$res"
hc "" "local backup ok; off-box ok: $res"

#!/usr/bin/env bash
# One-time VPS setup for the corners collector. Run as root on Ubuntu 24.04.
#   sudo bash deploy/bootstrap.sh
set -euo pipefail
apt-get update
apt-get install -y python3 sqlite3 chrony rclone git curl
systemctl enable --now chrony
timedatectl set-timezone UTC

id corners >/dev/null 2>&1 || useradd --system --home /opt/corners --shell /usr/sbin/nologin corners
install -d -o corners -g corners -m 750 /opt/corners /opt/corners/data
install -d -o root -g corners -m 750 /etc/corners
if [ ! -f /etc/corners/secrets.env ]; then
  install -o root -g corners -m 640 /dev/null /etc/corners/secrets.env
  cat > /etc/corners/secrets.env <<'SECRETS'
AF_KEY=
OP_KEY=
# Optional healthchecks.io ping URLs, one per job:
HC_AF_FIXTURES=
HC_AF_ODDS=
HC_AF_RESULTS=
HC_OP_ACCOUNT=
HC_OP_FIXTURES=
HC_OP_HISTORY=
HC_WATCHDOG=
HC_BACKUP=
# Optional rclone remote for off-box backup, e.g. b2:my-bucket/corners
RCLONE_REMOTE=
SECRETS
fi

# Code: the repository is expected at /opt/corners/app (git clone done by you).
test -f /opt/corners/app/collector/__main__.py || { echo "clone the repo to /opt/corners/app first"; exit 1; }
chown -R corners:corners /opt/corners/app
[ -f /opt/corners/config.toml ] || install -o corners -g corners -m 640 /opt/corners/app/deploy/config.example.toml /opt/corners/config.toml

python3 - <<'PY'
import sqlite3, sys
v = tuple(map(int, sqlite3.sqlite_version.split(".")))
sys.exit(0 if v >= (3, 42, 0) else f"SQLite {sqlite3.sqlite_version} too old (need 3.42+)")
PY

cp /opt/corners/app/deploy/corners@.service /opt/corners/app/deploy/corners-*.timer \
   /opt/corners/app/deploy/corners-backup.service /etc/systemd/system/
systemctl daemon-reload
echo "Installed. Next: fill /etc/corners/secrets.env, then follow RUNBOOK.md step 4 (day-1 checks) BEFORE enabling timers."

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

# Code: a root-owned git clone at /opt/corners/repo; releases are built from it by
# deploy/release.sh into root-owned, read-only directories. The corners user can
# read the code it runs but never modify it.
test -d /opt/corners/repo/.git || { echo "clone the repo to /opt/corners/repo first (as root)"; exit 1; }
chown -R root:root /opt/corners/repo
install -d -o root -g root -m 755 /opt/corners/releases
chown root:corners /opt/corners && chmod 750 /opt/corners

python3 - <<'PY'
import sqlite3, sys
v = tuple(map(int, sqlite3.sqlite_version.split(".")))
sys.exit(0 if v >= (3, 42, 0) else f"SQLite {sqlite3.sqlite_version} too old (need 3.42+)")
PY

# Swap: the collector peaks well under 200 MB, but a 1 GB VM should still have swap.
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo "Installed base packages. Next: fill /etc/corners/secrets.env, then run"
echo "  sudo bash /opt/corners/repo/deploy/release.sh main"
echo "  sudo -u corners python3 -m collector --config /opt/corners/app/config/production.toml init   (fresh install only)"

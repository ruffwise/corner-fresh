#!/usr/bin/env bash
# Deploy a git commit as a new read-only release, without stopping any timer.
#
#   sudo bash /opt/corners/repo/deploy/release.sh <commit-or-tag>        # deploy
#   sudo bash /opt/corners/repo/deploy/release.sh <commit-or-tag> --rollback   # code only, no migration
#
# Layout after the first run:
#   /opt/corners/repo                 root-owned git clone (source of releases)
#   /opt/corners/releases/<sha12>/    root-owned, read-only to corners (git archive of one commit)
#   /opt/corners/app -> releases/<sha12>   (atomic symlink switch)
#   /opt/corners/data                 corners-owned: database, archive, backups, review
#
# Timers keep running throughout. The switch happens inside a quiet window
# (minutes 21-28 of the hour, no corners job running): nothing is scheduled
# between the :20 and :30 watchdog runs.
set -euo pipefail
REF="${1:?usage: release.sh <commit-or-tag> [--rollback]}"
MODE="${2:-deploy}"
BASE=/opt/corners
REPO=$BASE/repo
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

git -C "$REPO" fetch --quiet --tags origin
# A branch name resolves to the freshly fetched remote branch, never a stale local one.
SHA=$(git -C "$REPO" rev-parse --verify --quiet "origin/$REF^{commit}" || git -C "$REPO" rev-parse --verify "$REF^{commit}")
SHORT=${SHA:0:12}
REL=$BASE/releases/$SHORT
echo "== release $SHORT ($REF)"

# 1. Build the release directory (idempotent).
if [ ! -d "$REL" ]; then
  mkdir -p "$REL.tmp"
  git -C "$REPO" archive "$SHA" | tar -x -C "$REL.tmp"
  echo "$SHA" > "$REL.tmp/RELEASE_COMMIT"
  chown -R root:root "$REL.tmp"
  chmod -R u=rwX,go=rX "$REL.tmp"
  mv "$REL.tmp" "$REL"
fi

# 2. Preflight with the NEW code: same database path, database exists, nothing created.
CFG=$REL/config/production.toml
sudo -u corners bash -c "set -a; . /etc/corners/secrets.env; set +a; cd '$REL' && python3 -m collector --config '$CFG' check-config" \
  | tee /tmp/corners-preflight.json
grep -q '"db_path": "/opt/corners/data/corners.db"' /tmp/corners-preflight.json || { echo "ABORT: db_path differs"; exit 1; }
grep -q '"db_exists": true' /tmp/corners-preflight.json || { echo "ABORT: database not found at configured path"; exit 1; }
# Which scheduled jobs the new config activates (empty for code older than v0.5.2).
JOBS=$(sudo -u corners bash -c "set -a; . /etc/corners/secrets.env; set +a; cd '$REL' && python3 -m collector --config '$CFG' scheduled-jobs" 2>/dev/null || true)
if [ -n "$JOBS" ]; then
  echo "$JOBS" | sed 's/^/   job: /'
  echo "$JOBS" | grep -Eq '^af_odds active$' || { echo "ABORT: af_odds is not in active_jobs; refusing to stop odds capture"; exit 1; }
fi

# 3. Wait for the quiet window.
wait_quiet() {
  while :; do
    m=$(date -u +%M); m=$((10#$m))
    busy=$(systemctl list-units --no-legend --state=activating,active 'corners@*.service' 'corners-backup.service' | wc -l)
    if [ "$m" -ge 21 ] && [ "$m" -le 28 ] && [ "$busy" -eq 0 ]; then return; fi
    echo "   waiting for quiet window (minute $m, $busy job(s) running)"; sleep 20
  done
}
wait_quiet

# 4. Migrate (backs up and restore-tests first; no-op if already current).
if [ "$MODE" != "--rollback" ]; then
  sudo -u corners bash -c "set -a; . /etc/corners/secrets.env; set +a; cd '$REL' && python3 -m collector --config '$CFG' migrate"
fi

# 5. Switch atomically. The first run converts the old directory install into a release.
if [ -d "$BASE/app" ] && [ ! -L "$BASE/app" ]; then
  OLD=$BASE/releases/pre-git-$(date -u +%Y%m%dT%H%M%SZ)
  mv "$BASE/app" "$OLD"
  chown -R root:root "$OLD"; chmod -R u=rwX,go=rX "$OLD"
  echo "   previous directory install kept at $OLD"
fi
ln -sfn "$REL" "$BASE/app.new"
mv -T "$BASE/app.new" "$BASE/app"

# 6. Units: install if changed; timers keep their schedule across daemon-reload.
changed=0
for f in "$REL"/deploy/corners@.service "$REL"/deploy/corners-*.timer "$REL"/deploy/corners-backup.service; do
  dest=/etc/systemd/system/$(basename "$f")
  if ! cmp -s "$f" "$dest"; then install -m 644 "$f" "$dest"; changed=1; fi
done
[ "$changed" = 1 ] && systemctl daemon-reload

# 6b. Scheduled-job timers follow active_jobs in the committed config, so the planner,
#     the watchdog and systemd can never disagree. Infrastructure timers (plan,
#     watchdog, report, backup) are always on.
if [ -n "$JOBS" ]; then
while read -r job state; do
  t=corners-${job//_/-}.timer
  if [ "$state" = active ]; then
    systemctl is-enabled --quiet "$t" && systemctl is-active --quiet "$t" || { systemctl enable --now "$t"; echo "   enabled $t"; }
  else
    if systemctl is-enabled --quiet "$t" || systemctl is-active --quiet "$t"; then systemctl disable --now "$t"; echo "   disabled $t (not in active_jobs)"; fi
  fi
done <<< "$JOBS"
else
  echo "   this release has no active_jobs support (older code): scheduled-job timers left as they are"
fi
for t in corners-plan.timer corners-watchdog.timer corners-report.timer corners-backup.timer; do
  systemctl is-enabled --quiet "$t" && systemctl is-active --quiet "$t" || { systemctl enable --now "$t"; echo "   enabled $t"; }
done

# 7. Post-checks with the new code.
run() { sudo -u corners bash -c "set -a; . /etc/corners/secrets.env; set +a; cd '$BASE/app' && python3 -m collector --config '$BASE/app/config/production.toml' $*"; }
run plan                      # plan for the active jobs now; removes pending slots of inactive ones
run watchdog || true
run archive-verify | tail -5
echo "== live: $(readlink -f $BASE/app)"
systemctl list-timers 'corners*' --no-pager | head -15

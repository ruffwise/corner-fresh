#!/usr/bin/env bash
# Make the scheduled-job timers match active_jobs in a release's committed config,
# then VERIFY the result and fail loudly on any mismatch.
#
#   sudo bash /opt/corners/app/deploy/sync-timers.sh            # the live release
#   sudo bash /opt/corners/app/deploy/sync-timers.sh <release>  # called by release.sh
#
# Every systemctl call reads from /dev/null, so nothing in the loop can consume the
# job list, and the final table is computed from systemd's own state.
set -euo pipefail
REL="${1:-/opt/corners/app}"
CFG="$REL/config/production.toml"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

if [ -n "${CORNERS_JOBS:-}" ]; then           # test hook: "job state" lines
  JOBS="$CORNERS_JOBS"
else
  JOBS=$(sudo -u corners bash -c "set -a; . /etc/corners/secrets.env; set +a; cd '$REL' && python3 -m collector --config '$CFG' scheduled-jobs" </dev/null)
fi
mapfile -t LINES <<< "$JOBS"
[ "${#LINES[@]}" -ge 6 ] || { echo "sync-timers: could not read active_jobs from $CFG"; exit 1; }
printf '%s\n' "${LINES[@]}" | grep -qx 'af_odds active' || { echo "ABORT: af_odds is not in active_jobs"; exit 1; }

sc() { systemctl "$@" </dev/null; }
for line in "${LINES[@]}"; do
  job=${line%% *}; want=${line##* }; t=corners-${job//_/-}.timer
  if [ "$want" = active ]; then
    if ! { sc is-enabled --quiet "$t" && sc is-active --quiet "$t"; }; then sc enable --now "$t"; echo "   enabled $t"; fi
  else
    if sc is-enabled --quiet "$t" || sc is-active --quiet "$t"; then sc disable --now "$t"; echo "   disabled $t (not in active_jobs)"; fi
  fi
done
for t in corners-plan.timer corners-watchdog.timer corners-report.timer corners-backup.timer; do
  if ! { sc is-enabled --quiet "$t" && sc is-active --quiet "$t"; }; then sc enable --now "$t"; echo "   enabled $t"; fi
done

# Verify from systemd's state, not from what the loop believes it did.
bad=0
printf '   %-28s %-9s %-10s %-9s %s\n' timer want enabled active ok
for line in "${LINES[@]}"; do
  job=${line%% *}; want=${line##* }; t=corners-${job//_/-}.timer
  en=$(sc is-enabled "$t" 2>/dev/null || true); ac=$(sc is-active "$t" 2>/dev/null || true)
  if [ "$want" = active ]; then ok=$([ "$en" = enabled ] && [ "$ac" = active ] && echo yes || echo NO)
  else ok=$([ "$en" != enabled ] && [ "$ac" != active ] && echo yes || echo NO); fi
  printf '   %-28s %-9s %-10s %-9s %s\n' "$t" "$want" "${en:-?}" "${ac:-?}" "$ok"
  [ "$ok" = yes ] || bad=1
done
if [ "$bad" != 0 ]; then echo "TIMER STATE MISMATCH: fix it before anything else (see the table)"; exit 1; fi
echo "   timers match active_jobs"

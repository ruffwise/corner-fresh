"""deploy/sync-timers.sh against a fake systemctl (state in a file).

The fake systemctl deliberately READS STDIN, the classic way a `while read` loop
loses its remaining lines; the script must still reach every job and verify.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "sync-timers.sh"
pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="sync-timers.sh refuses to run as non-root")

FAKE = r'''#!/usr/bin/env bash
cat >/dev/null || true            # eat stdin, like a misbehaving tool would
S="$FAKE_STATE"; cmd=$1; shift; [ "${1:-}" = --quiet ] && { q=1; shift; } || q=0; [ "${1:-}" = --now ] && shift
t=$1
case "$cmd" in
  is-enabled) if grep -qx "$t" "$S"; then [ $q = 1 ] || echo enabled; exit 0; else [ $q = 1 ] || echo disabled; exit 1; fi ;;
  is-active)  if grep -qx "$t" "$S"; then [ $q = 1 ] || echo active; exit 0; else [ $q = 1 ] || echo inactive; exit 3; fi ;;
  enable)  [ -n "${FAKE_STUCK:-}" ] && [ "$t" = "$FAKE_STUCK" ] && exit 0; echo "$t" >> "$S" ;;
  disable) [ -n "${FAKE_STUCK:-}" ] && [ "$t" = "$FAKE_STUCK" ] && exit 0; grep -vx "$t" "$S" > "$S.n" || true; mv "$S.n" "$S" ;;
esac
'''
JOBS = "af_fixtures active\naf_odds active\naf_results active\nop_account inactive\nop_fixtures inactive\nop_history inactive"
INFRA = ["corners-plan.timer", "corners-watchdog.timer", "corners-report.timer", "corners-backup.timer"]


def run(tmp_path, enabled, jobs=JOBS, stuck=""):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "systemctl").write_text(FAKE)
    (b / "systemctl").chmod(0o755)
    state = tmp_path / "state"
    state.write_text("".join(t + "\n" for t in enabled))
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", FAKE_STATE=str(state), CORNERS_JOBS=jobs,
               FAKE_STUCK=stuck)
    p = subprocess.run(["bash", str(SCRIPT), str(ROOT)], env=env, capture_output=True, text=True, input="x\n" * 50)
    return p, set(state.read_text().split())


def test_removed_job_timer_is_disabled(tmp_path):                 # the 30 Oct op_history case
    on = ["corners-af-fixtures.timer", "corners-af-odds.timer", "corners-af-results.timer",
          "corners-op-history.timer", *INFRA]
    p, state = run(tmp_path, on)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "disabled corners-op-history.timer" in p.stdout and "corners-op-history.timer" not in state
    assert "timers match active_jobs" in p.stdout


def test_added_job_timer_is_enabled(tmp_path):
    jobs = JOBS.replace("op_account inactive", "op_account active")
    p, state = run(tmp_path, ["corners-af-odds.timer"], jobs=jobs)
    assert p.returncode == 0, p.stdout + p.stderr
    assert {"corners-op-account.timer", "corners-af-fixtures.timer", *INFRA} <= state


def test_mismatch_after_reconcile_fails_loudly(tmp_path):
    on = ["corners-af-fixtures.timer", "corners-af-odds.timer", "corners-af-results.timer",
          "corners-op-history.timer", *INFRA]
    p, _ = run(tmp_path, on, stuck="corners-op-history.timer")      # disable silently has no effect
    assert p.returncode == 1 and "TIMER STATE MISMATCH" in p.stdout


def test_refuses_config_without_af_odds(tmp_path):
    p, _ = run(tmp_path, [], jobs=JOBS.replace("af_odds active", "af_odds inactive"))
    assert p.returncode == 1 and "af_odds is not in active_jobs" in p.stdout

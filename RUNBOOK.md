# Runbook v0.5.1

**Part A** redeploys v0.5 over the running v0.4 install. The af-odds timer is never stopped, and no data is lost.
**Part B** is a fresh install, for reference.

Run all commands on the VM unless a step says otherwise. Replace `OWNER` with your GitHub user or organisation.

---

## Part A-H — Hotfix v0.5.1: redaction destroyed key-passes data (do this before A10)

**What went wrong.** v0.5 applied field-name redaction to every endpoint. The bare name `key` matched api-football's key-passes statistic (`passes.key`) in match-detail responses (`/fixtures?ids=`):
- The v0.5 scrub rewrote raw ids 39–41 (2,306 values) and deleted their originals.
- Capture-time redaction blanked key passes in the one match-detail response captured between the v0.5 deploy and the hotfix: raw id 387, the 11:35 UTC `af-results` run (46 values).
- Total: 4 records, 2,352 values.

Nothing else was hit: odds, fixture lists, statistics (corners) and outcomes are unaffected.

**Record, 25 Oct.** H1–H7 done. Hotfix d401bc1 deployed 12:24 UTC (backup `corners-20261025T122405Z-pre-migrate-v5.db.gz`, restore test ok). `repair-damaged` showed 4 rows (39, 40, 41, 387), 2,352 values, one pattern; all 4 re-fetched and linked. `archive-verify`: 457 checked, 0 problems, 0 unrepaired. Scrub dry run: 0 candidates. Watchdog: `missed []`. H8 remains.

**What v0.5.1 does:**
- **Field rules run on two personal endpoints only.** `/status` and `/v4/account` are a fixed list, held in a database table that only a migration can change.
- **Data endpoints are never changed by field rules.** Only a live API key found in a data response is replaced, and that fires an alert.
- **Every rule that fires is counted per response.** The watchdog alerts if anything fires on a data endpoint, and the daily report shows the counts.
- **The scrub is restricted.** It refuses data records (also enforced by the schema), needs `--expect`, and moves originals aside instead of deleting them.
- **`repair-damaged` re-fetches every damaged response** as a new raw row, linked to the damaged one. The damaged rows stay as evidence.

**What can't be undone.** The original bytes of raw ids 39–41 are gone. The re-fetch records api-football's current final statistics for the same matches.

### H1. Bring the hotfix into your clone and push (as `azureuser`)

1. Copy the new bundle to the VM (from your PC):
   ```bash
   scp corner-fresh.bundle azureuser@<vm>:~/corner-fresh-v051.bundle
   ```
2. On the VM, as `azureuser`:
   ```bash
   cd ~/corner-fresh
   git fetch ~/corner-fresh-v051.bundle main
   git merge --ff-only FETCH_HEAD
   git log --oneline | head -3        # top line should read "Hotfix v0.5.1: ..."
   git push
   ```

### H2. Deploy

```bash
sudo bash /opt/corners/repo/deploy/release.sh main
```

**Expected:**
- `migrated` is `[6]`;
- the backup path ends `-pre-migrate-v5.db.gz`;
- `backup_restore_test` is `ok`.

This deploy stops new damage.

### H3. Damage dry run

```bash
sudo /opt/corners/app/deploy/run.sh repair-damaged
```

**Expected:**
- `per_endpoint` has exactly one key, `api_football /fixtures`;
- its `patterns` has exactly one key, `$.response[*].players[*].players[*].statistics[*].passes.key`;
- `damaged_rows` is 3 (raw ids 39–41), plus one for each match-detail response captured since the v0.5 deploy.

**If any other endpoint or pattern appears, stop and send me the output.**

### H4. Re-fetch

```bash
sudo /opt/corners/app/deploy/run.sh repair-damaged --apply --expect <damaged_rows from H3>
```

**Expected:** `linked` equals `damaged_rows`. `distinct_requests` can be smaller, because several damaged rows may share one id batch.

This costs one api-football request per distinct id batch, which is trivial against 7,500 a day.

### H5. Verify

```bash
sudo /opt/corners/app/deploy/run.sh archive-verify
```

**Expected:** `problem_count` is 0 and `damaged_unrepaired` is 0.

### H6. Scrub check (no rewrite expected)

```bash
sudo /opt/corners/app/deploy/run.sh archive-scrub
```

**Expected:** `rewrite_candidates` is 0, and `refused_data_endpoint_records` is empty. Raw ids 1 and 35 were already rewritten on 25 Oct.

### H7. Watchdog

```bash
sudo /opt/corners/app/deploy/run.sh watchdog
```

**Expected:** `"missed": []`. No `redaction_on_data_endpoint` entries.

### H8. Next morning

```bash
sudo /opt/corners/app/deploy/run.sh report
```

**Expected:**
- `redaction_last_24h` lists `field:` rules only against `api_football /status` and `oddspapi /v4/account`;
- `redaction_alerts_last_24h` is 0;
- `damaged_unrepaired` is 0.

Then continue with **A10**.

---

## Part A — Redeploy over the running install

### A0. Before 02:20 UTC Monday (03:20 Lagos), if `RCLONE_REMOTE` is set

The nightly backup would copy the two archived `/status` responses off the box. They contain your api-football account name and email. If you won't reach step A6 before then, blank the remote for one night:

```bash
sudo sed -i 's/^RCLONE_REMOTE=.*/RCLONE_REMOTE=/' /etc/corners/secrets.env
```

Local backups still run. Restore the line after A6.

### A1. Create the private repository

1. On github.com, create a new **private** repository named `corner-fresh`. Leave it **empty**: no README, licence or .gitignore.
2. Create a fine-grained personal access token:
   1. Go to **Settings → Developer settings → Fine-grained tokens → Generate new token**.
   2. Repository access: **only `corner-fresh`**.
   3. Permissions: **Contents: Read and write**.
   4. Expiry: 90 days.

   This token is for your own working clone (A2). The server uses a read-only deploy key instead (A3).

### A2. Push the code from the bundle (your working clone, as `azureuser`)

1. Copy `corner-fresh.bundle` to the VM (from your PC):
   ```bash
   scp corner-fresh.bundle azureuser@<vm>:
   ```
2. On the VM, as `azureuser` (no sudo):
   ```bash
   git bundle verify ~/corner-fresh.bundle
   git clone ~/corner-fresh.bundle ~/corner-fresh
   cd ~/corner-fresh
   git remote set-url origin https://github.com/OWNER/corner-fresh.git
   git config user.name "Oluwanbe Oluwatobi"
   git config user.email "<the address you use on GitHub>"
   git push -u origin main      # username = your GitHub login, password = the token from A1
   git log --oneline | head -5
   ```

This clone is where you make the commits that steps A10–A11 need (config and team map), then push them.

### A3. Give the server a read-only clone (root-owned)

1. Create a deploy key:
   ```bash
   sudo ssh-keygen -t ed25519 -N '' -C corner-fresh-deploy -f /root/.ssh/corner_fresh_deploy
   sudo cat /root/.ssh/corner_fresh_deploy.pub
   ```
2. On GitHub: **repo → Settings → Deploy keys → Add deploy key**. Paste the key and leave **Allow write access unticked**.
3. Configure SSH for it and clone:
   ```bash
   sudo tee -a /root/.ssh/config >/dev/null <<'EOF'
   Host github-corner-fresh
     HostName github.com
     User git
     IdentityFile /root/.ssh/corner_fresh_deploy
     IdentitiesOnly yes
   EOF
   sudo ssh -o StrictHostKeyChecking=accept-new -T github-corner-fresh || true   # "successfully authenticated" is expected
   sudo git clone git@github-corner-fresh:OWNER/corner-fresh.git /opt/corners/repo
   ```

### A4. Fix ownership: code root-owned, data corners-owned

```bash
sudo chown root:corners /opt/corners && sudo chmod 750 /opt/corners
sudo chown -R root:root /opt/corners/repo
sudo install -d -o root -g root -m 755 /opt/corners/releases
ls -ld /opt/corners /opt/corners/data /opt/corners/repo   # data must still be corners:corners
```

### A5. Deploy

The release script:
1. builds a read-only release from the commit;
2. checks, with the new code, that the database path is unchanged and the database exists;
3. waits for the quiet window (minutes 21–28, no job running);
4. backs up the database and restore-tests the backup;
5. migrates the schema from v4 to v5;
6. switches the `/opt/corners/app` symlink atomically;
7. installs the updated units.

Timers are never stopped.

```bash
sudo bash /opt/corners/repo/deploy/release.sh main
```

**Expected output:**

| Item | Expected value |
|---|---|
| preflight `db_path` | `/opt/corners/data/corners.db` |
| preflight `db_exists` | `true` |
| preflight `db_user_version` | `4` |
| migrate `"migrated": [5]` | yes |
| migrate `"backup_restore_test": "ok"` | yes |
| migrate `"backup"` | a path ending `-pre-migrate-v4.db.gz` |
| watchdog | `"missed": []` |
| `archive-verify` | `"problem_count": 0` |
| `live:` | `/opt/corners/releases/<sha12>` |

The old directory install, including your VM patch, is kept at `/opt/corners/releases/pre-git-<time>`.

**Check the next jobs ran on the new code** (after :35 and the next :05):
```bash
sudo /opt/corners/app/deploy/run.sh report
journalctl -u corners@af-odds --since -2h | tail -5
```

### A6. Scrub the pre-fix archive (scrub v2 procedure)

*Done on 25 Oct with the v0.5 scrub, which also damaged three data records. The hotfix (H1–H8) repairs them. For any future scrub, use this procedure.*

1. **Dry run.** It touches only personal endpoints, and prints a per-endpoint summary:
   ```bash
   sudo /opt/corners/app/deploy/run.sh archive-scrub
   ```
   **Stop and send me the output** in any of these cases:
   - `per_endpoint` lists anything other than `api_football /status` or `oddspapi /v4/account`;
   - `refused_data_endpoint_records` is not empty;
   - `rewrite_candidates` differs from what you expected.
2. **Apply**, passing the exact count from the dry run:
   ```bash
   sudo /opt/corners/app/deploy/run.sh archive-scrub --apply --expect <rewrite_candidates>
   sudo /opt/corners/app/deploy/run.sh archive-verify
   ```
   Originals are **moved** to `/opt/corners/data/superseded/`, not deleted. That folder is outside the archive, so it is never synced off-box. `archive-verify` lists them under `superseded_originals_pending`.
3. **Check one rewritten record**, then delete the originals:
   ```bash
   sudo /opt/corners/app/deploy/run.sh archive-finalize --raw-ids <ids> --confirmed-by "<your name>"
   ```
4. Re-run the bootstrap:
   ```bash
   sudo /opt/corners/app/deploy/run.sh af-bootstrap
   ```
   This refreshes the redacted review file and fills the bookmaker catalog.
5. Confirm nothing personal is left:
   ```bash
   sudo -u corners grep -rl '@' /opt/corners/data/review/ || echo "no email addresses in review files"
   ```

If you blanked `RCLONE_REMOTE` in A0, restore it now.

### A7. healthchecks.io for every job (mandatory)

healthchecks.io is the only alarm if the VM itself goes down. Create one check per job, using **Schedule = Cron** and **Time zone = UTC**:

| Variable | Cron | Grace |
|---|---|---|
| `HC_AF_ODDS` | `5 * * * *` | 15 min |
| `HC_WATCHDOG` | `*/10 * * * *` | 10 min |
| `HC_AF_RESULTS` | `35 * * * *` | 20 min |
| `HC_AF_FIXTURES` | `15 */6 * * *` | 30 min |
| `HC_OP_ACCOUNT` | `50 0 * * *` | 1 h |
| `HC_OP_FIXTURES` | `55 0 * * *` | 1 h |
| `HC_OP_HISTORY` | `45 * * * *` | 30 min |
| `HC_BACKUP` | `20 2 * * *` | 2 h |

1. Paste each ping URL into `/etc/corners/secrets.env`. Each job reads it at its next start; no restart is needed.
2. Point the integrations at an address and a phone channel you will actually see. Telegram works.
3. **Test it:** pick a check, press **Pause**, then **Resume**. You should get the alert.

### A8. Azure settings that would otherwise stop collection

1. **Auto-shutdown must be off.**
   - Portal: **VM → Operations → Auto-shutdown → toggle Off → Save**.
   - Or CLI: `az vm auto-shutdown -g <resource-group> -n <vm-name> --off`
2. **Upgrade a free account to pay-as-you-go before day 30.** Microsoft's FAQ says that if you don't upgrade, "your account and services will be disabled". Go to **Subscriptions → your subscription → Upgrade**.
   - The 12-month free services (for example, B1s hours) continue after upgrading.
   - Their clock runs from account creation.
   - Add a budget alert in **Cost Management → Budgets** so a surprise charge is visible.
3. **Memory on a 1 GB VM.**
   1. Check for swap:
      ```bash
      swapon --show || true
      ```
   2. If it prints nothing, create a 2 GB swapfile:
      ```bash
      sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
      echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
      ```
   3. Measure real peaks after a day of runs:
      ```bash
      for j in af-odds af-fixtures af-results op-history; do
        echo -n "$j: "; systemctl show -p MemoryPeak --value corners@$j.service
      done
      journalctl -u 'corners@*' --since -1d | grep -o 'memory peak[^,]*' | sort | uniq -c | tail
      ```
      Send me the numbers.

**Memory estimate.** Parsing and close-reporting a 59,000-record history payload (7 MB of JSON) took about 26 MB in a test here. I expect the whole process to stay well under 200 MB. The unit caps it at 700 MB, so a runaway becomes a failed, visible slot rather than an OOM-killed VM.

### A9. Recheck Arsenal v Leeds under the new close rule (free)

```bash
sudo /opt/corners/app/deploy/run.sh op-check-fixture id1000001772221292 --kickoff 2026-10-10T11:30:00Z
```

Send me the output. These fields matter:

| Field | Meaning |
|---|---|
| `basis` | Which start sources were used. `op_true_start` appears if the payload carries `trueStartTime` |
| `payload_top_level_fields` | Lets me see whether v4 history carries `trueStartTime` at all |
| `cutoff_ms` | Expect 11:28:00 UTC, or earlier if the true start was earlier |
| `records_in_buffer` and `records_at_or_after_cutoff` | Excluded by rule. Last time, 33,494 records were at or after kickoff |
| `series_without_close` | Counts by reason, for example `suspended_at_cutoff` |
| `close_age_last_record_min` and `close_age_last_change_min` | Staleness by last record and by last price change |

### A10. OddsPapi day-1 steps (formerly 7, 9 and 10)

1. **Quota reading (free):**
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-account
   ```
   It now prints only `request_limit`, `request_count` and `fields_present`, never the body.

2. **Catalog (2 billable calls):**
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-catalog --sport-id 10
   sudo -u corners cat /opt/corners/data/review/day1_oddspapi_tournaments.json | head -50
   ```

3. **Config commit.** In your working clone (`~/corner-fresh`):
   1. Edit `config/production.toml`.
   2. Set `oddspapi_tournament_id` on each league, both the 2026 and the 2025 entries.
   3. Uncomment `fixtures_from = "2026-01-01T00:00:00Z"`.
   4. Commit, push and deploy:
      ```bash
      git commit -am "config: OddsPapi tournament ids; one-off listing from 2026-01-01" && git push
      sudo bash /opt/corners/repo/deploy/release.sh main
      ```

4. **List every tournament** (1 billable call per distinct tournament):
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-fixtures --force
   ```

5. **Revert the one-off setting** by re-commenting `fixtures_from`:
   ```bash
   git commit -am "config: end one-off listing window" && git push
   sudo bash /opt/corners/repo/deploy/release.sh main
   ```

6. **Month histogram.** Check that the listing reaches back to January:
   ```bash
   sudo -u corners sqlite3 /opt/corners/data/corners.db \
     "SELECT tournament_key, strftime('%Y-%m', start_ms/1000, 'unixepoch'), count(DISTINCT provider_fixture_id)
      FROM provider_fixture_candidate GROUP BY 1,2 ORDER BY 1,2;"
   ```

### A11. Team map, from review to load

1. Propose the mappings:
   ```bash
   sudo /opt/corners/app/deploy/run.sh map-propose
   sudo cat /opt/corners/data/review/team_map_proposals.csv > ~/corner-fresh/review/team_map_v1.csv
   ```
2. Review `review/team_map_v1.csv`. Delete any wrong rows and resolve any `conflicts`.
3. Commit, push, and fetch the commit into the server clone:
   ```bash
   cd ~/corner-fresh && git add review/team_map_v1.csv && git commit -m "team map v1 (reviewed)" && git push
   SHA=$(git rev-parse HEAD)
   sudo git -C /opt/corners/repo fetch --quiet origin
   ```
4. Load and apply. The loader reads the CSV from git at that commit, not from disk:
   ```bash
   sudo /opt/corners/app/deploy/run.sh map-load-teams $SHA review/team_map_v1.csv
   sudo /opt/corners/app/deploy/run.sh map-apply
   ```
5. **Backfill** history (free) and exploration-season outcomes. Repeat each until it reports nothing left:
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-backfill --max 600
   sudo /opt/corners/app/deploy/run.sh af-backfill --max 1500
   ```
6. **Enable the OddsPapi timers:**
   ```bash
   sudo systemctl enable --now corners-op-account.timer corners-op-fixtures.timer corners-op-history.timer
   ```

From now on, `op-fixtures` runs `map-apply` itself. It alerts (exit 4, healthchecks fail) when more than 10% of the next 72 hours' fixtures in a tournament are unmapped. It relists, within the quota guard, when OddsPapi hasn't listed them.

### A12. Real-payload tests (makes CI green)

1. Export the payloads:
   ```bash
   sudo -u corners bash -c 'set -a; . /etc/corners/secrets.env; set +a; cd /opt/corners/app && \
     python3 -m collector --config config/production.toml export-fixtures --out /opt/corners/data/review/real \
     --history id1000001772221292=2026-10-10T11:30:00Z'
   sudo cp -r /opt/corners/data/review/real ~/corner-fresh/tests/fixtures/ && sudo chown -R azureuser: ~/corner-fresh/tests/fixtures
   ```
2. Look before you commit:
   ```bash
   zcat ~/corner-fresh/tests/fixtures/real/*/status_*.json.gz | head -c 600
   ```
   You should see `[REDACTED]` in place of your name and email.
3. Commit and push:
   ```bash
   cd ~/corner-fresh && git add tests/fixtures/real && git commit -m "tests: real payloads for every endpoint" && git push
   ```

The export never includes a response damaged by redact-v1. It needs one match-detail response (`/fixtures?ids=`) from a finished match captured **after** the hotfix. If `missing_endpoints` lists `api_football /fixtures?ids`, wait for the next `af-results` run and export again.

Until this step is done, CI fails one test, `test_every_endpoint_has_a_real_payload`, on purpose. An absent fixture must not pass.

### A13. Rollback, if ever needed

- **Code only:**
  ```bash
  sudo bash /opt/corners/repo/deploy/release.sh <previous-commit> --rollback
  ```
  This works between releases with the same schema.
- **Back to v0.4:** v0.4 code refuses a v5 database. Rolling back means restoring the `*-pre-migrate-v4.db.gz` backup:
  1. Stop the timers.
  2. `gunzip` the backup over `corners.db`.
  3. Point `/opt/corners/app` at `releases/pre-git-*`.

  Raw archive files written since are kept. Their envelopes rebuild the index, so the archive loses nothing.

---

## Part B — Fresh install (reference)

1. Create an Ubuntu 24.04 VM. Then do A1–A3, cloning to `/opt/corners/repo`.
2. Install the base system:
   ```bash
   sudo bash /opt/corners/repo/deploy/bootstrap.sh
   ```
   It installs packages, chrony, swap and the corners user, and keeps the code root-owned.
3. Fill in `/etc/corners/secrets.env`, including every `HC_` URL (A7).
4. Deploy and initialise:
   ```bash
   sudo bash /opt/corners/repo/deploy/release.sh main
   sudo -u corners bash -c 'cd /opt/corners/app && python3 -m collector --config config/production.toml init'
   ```
   `init` is the only command that creates a database. Every other command refuses if the database is missing.
5. Run the day-1 checks:
   ```bash
   sudo /opt/corners/app/deploy/run.sh af-bootstrap
   sudo /opt/corners/app/deploy/run.sh af-fixtures
   ```
6. Enable the timers:
   ```bash
   sudo systemctl enable --now corners-plan.timer corners-watchdog.timer corners-af-fixtures.timer \
     corners-af-odds.timer corners-af-results.timer corners-report.timer corners-backup.timer
   sudo /opt/corners/app/deploy/run.sh plan
   ```
7. Then do A7–A12.

## If something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `QUOTA REFUSED` | The guard did its job: nothing billable was sent | Check `op-account`. Don't raise the budget without a written reason |
| `op-fixtures` exit 4 / healthchecks fail | Unmapped share above 10% for the next 72 h | Read `unmapped_next_72h`. `candidate_unmapped` means review the team map; `no_candidate` means a relist was attempted |
| A slot is `failed` | A request failed after retries | The raw attempts are in the archive; `report` shows which endpoint |
| A slot is `missed` | VM down, or the timer didn't fire | `systemctl status corners@<job>` and `journalctl -u corners@<job>` |
| `release.sh` stuck "waiting for quiet window" | A job is running, or it is outside :21–:28 | Wait. It never switches while a job runs |
| `ABORT: db_path differs` | The release config points somewhere else | Fix `config/production.toml` in git. The script never creates a new database |
| `archive-verify` problems | A file is missing, has a bad hash, or contains a live secret | Send me the output before doing anything else |

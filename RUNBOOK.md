# Runbook: get collection running

Collection comes first. Steps 1–6 start api-football capture and should take
about 45 minutes. The OddsPapi steps (7–10) can follow the same day.

The code uses only the Python standard library. No pip installs are needed on
the server.

---

## 1. Create the server

1. Create a small Linux VPS:
   - Ubuntu 24.04 LTS, x86 or ARM;
   - 1–2 vCPU, 2–4 GB RAM, 20–40 GB disk;
   - a personal account (not anything at work).
2. In the provider's firewall, allow inbound **SSH only**. The collector makes outbound calls only.
3. SSH in as a sudo user.

## 2. Put the code on it

Pick one of these:

- **From GitHub:**
  `sudo git clone https://github.com/<you>/<repo>.git /opt/corners/app`
- **From the zip:**
  1. `scp corners-v0.4.zip <server>:`
  2. `sudo apt-get install -y unzip`
  3. `sudo mkdir -p /opt/corners && sudo unzip corners-v0.4.zip -d /opt/corners && sudo mv /opt/corners/corners /opt/corners/app`

## 3. Install

```bash
sudo bash /opt/corners/app/deploy/bootstrap.sh
chronyc tracking            # "System time" offset should be well under 50 ms
```

## 4. Add the keys

Edit the secrets file:

```bash
sudo nano /etc/corners/secrets.env
```

1. Fill in `AF_KEY` (api-football) and `OP_KEY` (OddsPapi).
2. Optional: fill in the healthchecks.io ping URLs, one check per job, and `RCLONE_REMOTE` for off-box backup.

The file is readable only by root and the `corners` group. Keys never reach
the database, the archive, or git; a test enforces this.

## 5. Day-1 api-football checks

These cost 5–20 of your 7,500 daily requests.

1. **Pinnacle coverage (requirement 2):**
   ```bash
   sudo /opt/corners/app/deploy/run.sh af-bootstrap
   ```
   Read `pinnacle_check`:
   - **PASS:** exactly one bookmaker is named `Pinnacle`. Note its id.
   - **FAIL:** api-football has no Pinnacle. Collection still runs; the Pinnacle reference then comes from OddsPapi history alone. Tell me before step 11.
   - **REVIEW:** look at `data/review/day1_api_football.json` and tell me what's there.

2. **Load fixtures for all configured leagues and seasons:**
   ```bash
   sudo /opt/corners/app/deploy/run.sh af-fixtures
   ```

## 6. Start api-football capture now

```bash
sudo systemctl enable --now corners-plan.timer corners-watchdog.timer \
  corners-af-fixtures.timer corners-af-odds.timer corners-af-results.timer \
  corners-report.timer corners-backup.timer
sudo /opt/corners/app/deploy/run.sh plan
systemctl list-timers 'corners*'
```

From here, odds snapshots are archived every hour at :05 and results at :35.

**Check after the next :05:**
```bash
sudo /opt/corners/app/deploy/run.sh report
```
Look at `slots_last_24h` and `raw_last_24h`.

## 7. OddsPapi quota (free call)

```bash
sudo /opt/corners/app/deploy/run.sh op-account
```

1. Record `request_limit` and `request_count`.
2. If the raw response (`raw` in the output) shows a reset or renewal date, tell me.

Until that is confirmed, the guard treats 250 as possibly lifetime. It keeps
50 requests in reserve, and allows at most 20 billable calls a month by our own count.

## 8. The Arsenal v Leeds check (free)

1. Find the kickoff time from the api-football fixtures loaded in step 5:
   ```bash
   sudo -u corners sqlite3 /opt/corners/data/corners.db \
     "SELECT f.fixture_id, datetime(l.kickoff_ms/1000,'unixepoch') FROM fixture f
      JOIN v_fixture_latest l USING (fixture_id)
      JOIN team h ON h.team_id=f.home_team_id JOIN team a ON a.team_id=f.away_team_id
      WHERE h.name='Arsenal' AND a.name LIKE 'Leeds%';"
   ```
2. Pull its Pinnacle history and analyse the final hour (use the time from step 1):
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-check-fixture id1000001772221292 --kickoff 2026-10-10THH:MM:00Z
   ```
3. Send me the output. It is also saved to `data/review/check_id1000001772221292.json`. The key fields are:
   - `records_in_final_60_min` and `markets_with_a_record_in_final_60_min`;
   - `last_record_before_kickoff_min`: how many minutes before kickoff the vendor last recorded **any** Pinnacle price;
   - `largest_gap_in_window_min`.

**How to read it.** This is one fixture, so it is an illustration, not the G0.5 result; the gate is computed across all mapped fixtures (DESIGN v0.4 §6). Two warning signs:
- No record in the final 60 minutes across all markets suggests the vendor was not watching. An EPL Pinnacle board does not normally sit still for an hour before kickoff.
- A last record only seconds before kickoff, with nothing for a long time before that, suggests sparse scraping.

Note: api-football's own odds for this match are already gone. Its 7-day retention ended on 17 October.

## 9. OddsPapi catalog and fixture lists (billable: about 10 calls in total)

1. Fetch the catalog (2 billable calls):
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-catalog --sport-id 10
   ```
   `sportId=10` is the example value in OddsPapi's docs. It is not confirmed to be football. If the tournament list in `data/review/day1_oddspapi_tournaments.json` isn't football, stop and tell me.

2. Edit `/opt/corners/config.toml`:
   1. For each league, set `oddspapi_tournament_id` from that file.
   2. Set `fixtures_from = "2026-01-01T00:00:00Z"` (one-off, so history back to January is discoverable).

   Commit the config change to git: it is part of the record of what was tracked when.

3. List every configured tournament now (1 billable call per distinct tournament):
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-fixtures --force
   ```

4. Remove `fixtures_from` from the config again.

5. Check that the listing reaches back to January:
   ```bash
   sudo -u corners sqlite3 /opt/corners/data/corners.db \
     "SELECT tournament_key, strftime('%Y-%m', start_ms/1000, 'unixepoch') m, count(DISTINCT provider_fixture_id)
      FROM provider_fixture_candidate GROUP BY 1,2 ORDER BY 1,2;"
   ```
   If there are no months before August, OddsPapi's listing did not reach the previous season. Tell me before spending more calls.

## 10. Map fixtures and start history capture

1. Propose team mappings:
   ```bash
   sudo /opt/corners/app/deploy/run.sh map-propose
   ```
2. Review the proposals:
   1. Open `data/review/team_map_proposals.csv`.
   2. Delete any wrong row, and resolve any row with `conflicts`.
   3. Copy the file into the repo, commit it, and note the full commit hash.
3. Load the approved teams and apply the mappings:
   ```bash
   sudo /opt/corners/app/deploy/run.sh map-load-teams /opt/corners/app/review/team_map_v1.csv <40-char-commit>
   sudo /opt/corners/app/deploy/run.sh map-apply
   ```
4. Backfill history and outcomes. Both are free (history) or cheap (api-football); repeat until nothing is left:
   ```bash
   sudo /opt/corners/app/deploy/run.sh op-backfill --max 600   # about 1 hour per 600 at the 5 s cooldown
   sudo /opt/corners/app/deploy/run.sh af-backfill --max 1500  # outcomes for exploration seasons
   ```
5. Enable the OddsPapi timers:
   ```bash
   sudo systemctl enable --now corners-op-account.timer corners-op-fixtures.timer corners-op-history.timer
   ```

## 11. After a week

1. Run the mapping checks:
   ```bash
   sudo /opt/corners/app/deploy/run.sh map-audit     # kickoff re-check: measured mis-join rate
   sudo /opt/corners/app/deploy/run.sh map-sample    # 30 mappings to check by hand
   ```
2. Fill in the `agrees` column of `data/review/manual_mapping_sample.csv` against BBC Sport or SofaScore, and send it to me.
3. Send me the output of `run.sh report`.

## If something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `QUOTA REFUSED` | The guard did its job: nothing billable was sent | Check `op-account`. Don't raise the budget without a reason you'd write down |
| A slot is `failed` | Some request failed after retries | The raw attempts are in the archive. `report` shows which endpoint |
| A slot is `missed` | The machine was down or the timer didn't fire | Check `systemctl status corners@<job>` and `journalctl -u corners@<job>` |
| `clock_flagged_last_24h` > 0 | The server clock disagrees with the API's `Date` header | Check `chronyc tracking`. Flagged rows are kept but can't be parsed into prices |

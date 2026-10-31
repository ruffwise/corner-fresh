# Pinnacle-Close Benchmark Study — Design v0.5.7

**Status.** Draft for review. Supersedes v0.4. When accepted, commit it as part of `prereg-0`, then sign and timestamp it (§13).
**Date.** 2026-10-30.

**Companion files**

- `db/schema.sql` (v4 base), `db/migrations/005_v05.sql` (to v5) and `db/connect.py`
- `db/migrations/006_v051.sql` (to v6), `007_v054.sql` (to v7: settled `no_history` outcomes) and `008_v057.sql` (to v8: mapping by structure)
- `collector/` and `deploy/`, plus `RUNBOOK.md` (v0.5.4)
- `analysis/prereg_tests.py`: the two confirmatory tests of §1, for any number of outcomes
- `tests/`: 298 pass. One more, `test_every_endpoint_has_a_real_payload`, fails **on purpose** until real payloads are exported (RUNBOOK A12)
- `analysis/power.py` and its output `analysis/power_output.md`

Every number in this document comes from `analysis/power.py` unless a source is cited.

**Objective (rescoped).** Determine whether any automated signal produces probabilities that beat Pinnacle's de-vigged closing price on a proper scoring rule, in any market with Pinnacle coverage. Hit rate and accuracy are not success metrics.

---

## 0. Status, and what changed in v0.5 to v0.5.4

**Status (30 Oct, night).**
- v0.5.6 deployed (a7f7d4f). Team map rounds 1–3 loaded: 186 + 46 + 7 pairs, all reviewed; 0 participants missing in all 8 tournaments for this season.
- Mapped 3,248 candidates by kickoff. 920 were left unmapped despite both teams being approved, mostly future Serie A, La Liga, Bundesliga, Ligue 1 and EPL fixtures whose listed start is a provisional time. Of the next 7 days, 3 of 9 Ligue 1 fixtures on 31 Oct. Fixed by mapping by structure (C31).

**Status (30 Oct, evening).**
- v0.5.5 deployed (5697448): for the first time the release output matched the runbook. It showed the deploy-script line, the six `job:` lines, the timer table and the `plan` output.
- A11 step 1 (`map-propose`): 188 proposed pairs. Every team of the six domestic leagues, both seasons, maps correctly.
- Two placeholder participants were proposed ("Winner Match 2", "Winner Match 3"); they are now excluded (C28).
- UEFA league stage: 9 of 36 UCL and 11 of 36 UEL clubs had no proposal, because 9 fixtures share each kickoff. Opponent propagation (C27) covers them. op_history and op_fixtures stay inactive until it does.

**Status (30 Oct, afternoon).**
- v0.5.4 deployed (live 2f969b5, schema v7; backup `…-pre-migrate-v6.db.gz`, restore test ok). `sync-timers.sh` run on its own: all six timers match `active_jobs`.
- **The history window is January to now** (W4 probe, §6.1): 17 of 17 probed fixtures returned usable history, from 16 Jan to 17 Oct, plus the control; 1–4 Jan return 404. Not a rolling window.
- W3 confirmed §0.7: 1,380 failed requests for 60 fixtures dated 1–4 Jan, each 404 at attempts 1, 2 and 3.
- W7: UEFA round names checked; the qualifier rule holds (§9.1).

**Status (30 Oct, morning).**
- v0.5.3 deployed; by-hand runs verified (own slot, no healthchecks event); `raw_vs_requests` balanced.
- A8 done (auto-shutdown off, 2 GB swap). A10 done: OddsPapi tournament ids committed (EPL 17, Championship 18, UCL 7, La Liga 8, Serie A 23, Bundesliga 35, Ligue 1 34, UEL 679, the same ids for 2025); 4,193 candidates listed, none mapped yet (A11 pending); the month histogram starts at 2026-01 in every tournament.
- Quota: provider count 19 = 9 earlier manual probes + 2 catalog + 8 listings; our own count 10. The 4,140 history calls of 27–30 Oct did not move the provider count, consistent with history being free (§2 F2).
- Personal-endpoint redaction confirmed live: `field:` rules fired only on api-football `/status` and OddsPapi `/v4/account`; 0 alerts.
- **op_history is paused** (removed from `active_jobs`, timer disabled) after the 27–30 Oct incident (§0.7). It returns at RUNBOOK A11 step 6.

**Earlier status (26 Oct).** api-football capture has been running since 25 Oct on your Azure VM, now at schema v6 (hotfix v0.5.1):
- 5,628 fixtures tracked across 16 league-seasons; 76 outcomes;
- last 24 h: `af_odds` 24 of 24 slots, `af_results` 25, `af_fixtures` 4; 1,104 odds responses; 0 clock flags;
- `af-bootstrap` passed (one bookmaker named `Pinnacle`, id 4);
- v0.5.2 deployed 26 Oct: active jobs `af_fixtures`, `af_odds`, `af_results`, `op_history`; watchdog `ok`;
- off-box copy live since 26 Oct (Azure Blob, Central India): first run 1,557 local = 1,557 remote archive files; restore drill from the remote copy passed (`integrity_check: ok`, 0 FK failures, user_version 6); delete refused to the VM identity (403) as designed;
- measured growth: archive 27.96 MB per 24 h (0.84 GB per 30 days, api-football only); 25.5 GB free on the VM disk.

OddsPapi steps (A10–A11) follow the v0.5.3 deploy.

**v0.5.2 changes** (no schema change):

| # | Change | Supersedes |
|---|---|---|
| C8 | **Scheduled jobs follow `active_jobs`** in the committed config: the planner plans, the watchdog checks and `release.sh` enables only those. A timer firing for any other job does nothing and fails the watchdog. The watchdog's failure ping names each problem | v0.5 planner, which planned every job and so alerted hourly on jobs that were never enabled |
| C9 | **Off-box copy** to Azure Blob Storage through the VM's managed identity, with a no-delete role, blob versioning and soft delete; nightly copy verified by a one-way check; backup and off-box status, storage growth and free disk in the daily report | v0.4 §5 "rclone if set" |
| C10 | **Look-ahead L2 flags go to inspection**, with a pre-registered flag-rate cap and cause list (§0.3) | §0.3 L2 as an automatic failure |
| C11 | **Suspended at the cutoff:** close-v1b registered now as a sensitivity rule, with a switching rule that uses suspension counts only (§0.2) | — |
| C12 | **≥ 2 start sources** for a fixture to count toward G0.5a, with a floor on how many fixtures may lack them (§0.2) | §6 G0.5a denominator |
| C13 | **Main-line rule** for G0.5 and the look-ahead selection (§0.2) | §6, which named the main line without defining it |

**v0.5.3 changes** (no schema change):

| # | Change | Supersedes |
|---|---|---|
| C14 | **By-hand runs never touch the plan or healthchecks.** Only a systemd timer run claims a planned slot and pings. A `run.sh` run always gets its own slot noted `by hand`; it can't collide with a completed slot, satisfy a missed one, raise a false `/fail`, or hide a broken timer with a success ping. By-hand slots are reported separately and excluded from G0.1 | v0.5.2, where a by-hand run of an active job claimed the current slot and failed with "already complete" |
| C15 | **`raw_vs_requests` in the daily report:** why `raw_response` has more rows than `api_request` (extra pages, retries, requests that never produced a row), with a balance check | — |

**v0.5.4 changes** (schema v7):

| # | Change | Supersedes |
|---|---|---|
| C16 | **History only for mapped fixtures.** `history_due` requires a live mapping; unmapped candidates are never fetched | v0.5 `history_due`, which fetched every listed candidate (§0.7) |
| C17 | **404 is an answer, not a failure.** A history 404 is not retried, is recorded in `request_outcome` as `no_history` (schema-checked: only for a recorded 404 with no usable response), lets the slot complete, is asked once more after 24 h, and is terminal after the second 404 (`history_404_max = 1` makes it terminal at once) | v0.5: 3 attempts per 404, the slot failed, the same fixtures re-queued hourly |
| C18 | **429 back-off and early stop.** On 429 the request backs off 12 s then 24 s on top of the 6 s pacing; after 2 consecutive fixtures still rate-limited the run stops and the rest stay due. 429, 5xx and network failures still fail the slot | v0.5 fixed 12 s pause |
| C19 | **History health in the report and the watchdog:** usable, no_history and failed in 24 h; the watchdog fails if more than half of ≥ 10 requests for fixtures started in the last 7 days get no_history, so a vendor change can't hide behind settled 404s | — |
| C20 | **`op-history-probe`:** one finished fixture per month and tournament, plus a known-good control, to establish the real history window (RUNBOOK W4) | — |
| C21 | **`deploy/sync-timers.sh`:** reconciles timers with `active_jobs` with every `systemctl` call reading `/dev/null`, then verifies from systemd's own state and fails loudly on any mismatch. `release.sh` calls it | v0.5.2's `while read` loop in `release.sh`, which could lose the rest of the job list (§0.7) |
| C22 | **Pre-registration amendments (30 Oct, before any data):** main lines are half lines only (§0.2 item 6); explicit confirmatory families and exclusions (§9.1); UEFA qualifiers excluded (§9.1); G0.1 window (§11); integrity without branch protection (§13) | §0.2 item 6 as confirmed on 26 Oct |

**v0.5.5 changes** (no schema change):

| # | Change | Supersedes |
|---|---|---|
| C23 | **`release.sh` always runs the latest deploy script** (from `origin/main`), and says which. The copy in the server clone's working tree had gone stale: `git fetch` never updates it, so v0.5.2, v0.5.3 and v0.5.4 were all deployed by the b9c09f6 script (§0.7) | Every `release.sh` since b9c09f6 |
| C24 | **`op-backfill --max` is the real cap** (it was silently limited to the hourly job's 60) | v0.5 `op-backfill` |
| C25 | **The confirmatory tests for any K** (`analysis/prereg_tests.py`), with 1X2 scored on all three outcomes (§1) | §1's binary-only wording |
| C26 | **G0.5a treatment of settled no_history** (§6) and the measured start of history (`eligibility.history_window_start_ms`) | — |

**v0.5.6 changes** (no schema change):

| # | Change | Supersedes |
|---|---|---|
| C27 | **Opponent propagation in `map-propose`** (§5): with simultaneous kickoffs, a participant whose opponent is already **approved** is pinned to the one fixture with that team on the same side. Only approved pairs propagate, so each round rests on reviewed mappings | Kickoff-only proposals, which can't separate 9 simultaneous UEFA fixtures |
| C28 | **Placeholder participants** ("Winner Match 2" and the like) are never proposed, and the loader refuses them | Reliance on the reviewer |
| C29 | **A `decision` column**: `accept`, or `REVIEW: <why>` for undominated conflicts, a team already approved for another key, or one team proposed for two keys. The loader takes only `accept` rows; identical already-loaded rows are skipped, so a cumulative CSV can be reloaded; a changed pair is refused | Loader refusing any non-empty `conflicts` cell |
| C30 | **Root-run deploy files need your confirmation when they change** (§13): `release.sh` shows the diff of `deploy/` against the last approved tree and asks for `yes` at the terminal | C23's unconditional re-execution |

**v0.5.7 changes** (schema v8):

| # | Change | Supersedes |
|---|---|---|
| C31 | **Mapping by structure** (§5). Kickoff agreement stays the first rule. When it fails, a candidate maps if both teams are approved, exactly one api-football fixture has that **ordered** pair within 4 days of the listed start, and the pair occurs **once** in its league-season. The basis is recorded per mapping (`map_basis`), and the schema re-checks uniqueness | The 15-minute rule as the only rule. Listings refresh every 28 days, so a fixture timed or re-timed after a listing couldn't map, and re-timed fixtures are the televised ones: a non-random hole |
| C32 | **Unmapped candidates by reason** (`map-unmapped`; counts in the daily report): `team_not_allowlisted`, `orientation_swapped`, `pair_not_unique_in_season`, `kickoff_gap_over_4_days`, `no_af_fixture`, `ambiguous`, split played and future, with the played ones listed | `no_unique_af_fixture`, one undifferentiated count |
| C33 | **The 72-hour check finds a listed fixture by its team pair too.** A stale listed start no longer looks like "not listed", which would have triggered billable relisting every 48 hours | §0.4's kickoff-only candidate lookup |
| C34 | **Every listing also covers the last 35 days** (`fixtures_lookback_days`), so a post-match listing brings each fixture's final start and `trueStartTime`. That gives close-v1 a second start source and the structure mappings a real kickoff recheck. No extra calls | Listings of upcoming fixtures only |

**v0.5 changes.** Each supersedes the section named.

| # | Change | Supersedes |
|---|---|---|
| C1 | **Redaction (§0.1).** Response bodies are redacted before hashing, archiving or parsing. This is the one permitted change to a raw body. Revised to redact-v2 in v0.5.1, after v1 destroyed key-passes data | v0.4 §4: the "keys never stored" claim, which was false for response bodies |
| C2 | **Close rule close-v1 (§0.2)**, in one code path, enforced again by schema triggers | §6 `v_close` and the G0.5 per-fixture definitions |
| C3 | **Look-ahead control (§0.3)** | New gate G0.5d |
| C4 | **Reverse-direction mapping coverage (§0.4)** | Adds to §5 |
| C5 | **Unnamed bookmakers.** Catalogued as received; they can never be approved into the allowlist | Adds to §5 and the allowlists |
| C6 | **Releases.** Code is root-owned and read-only to the service user; the database is never auto-created; migrations back up first | v0.4 §5 deployment |
| C7 | **Real-payload tests** for every endpoint | Adds to the testing rule |

### 0.1 Redaction policy (`collector/redact.py`, policy `redact-v2`; v0.5.1)

**Incident, 25 Oct.** Policy `redact-v1` applied field-name rules to every endpoint. The bare name `key` matched api-football's key-passes statistic (`passes.key`) in match-detail responses:
- The v0.5 scrub rewrote 3 such records (raw ids 39–41), blanking 2,306 values, and deleted the originals.
- Capture-time redaction then blanked the same field in the one match-detail response captured between the v0.5 deploy and the hotfix: raw id 387, the 11:35 UTC results run, 46 values.
- Total: 4 records, 2,352 values. (An earlier draft of this note said 4,612 for 39–41; that figure counted the scrub's dry-run and apply listings together.)
- No other field was hit. Odds, fixture lists, statistics and outcomes were unaffected.
- **Repair:** every damaged response is identified from its own envelope or from `archive_rewrite`, and re-fetched as a new raw row linked in `raw_refetch`. The damaged rows stay as evidence. The original bytes of 39–41 are not recoverable; their re-fetches hold api-football's statistics as served on 25 Oct.
- **Repaired 25 Oct** (hotfix deployed 12:24 UTC): `repair-damaged` dry run showed 4 rows, 2,352 values, the single pattern `passes.key`; apply re-fetched and linked all 4; `archive-verify` 457 checked, 0 problems, 0 unrepaired; scrub dry run 0 candidates; watchdog `missed []`.

**Policy v2:**
- **Personal endpoints** form a fixed list in the `personal_endpoint` table, which only a migration can change: api-football `/status` and OddsPapi `/v4/account`.
  - On these only, field rules replace credential and personal fields (`api_key`, `key`, `token`, `email`, `firstname`, `lastname`, `phone`, `username` and similar), plus `name` inside an account object.
- **Data endpoints** (everything else) are never changed by a field rule. The schema refuses to record one firing there.
  - Only a live configured key found in a string, in the bytes or in a header is replaced (`value:` and `bytes:` rules).
  - Personal-looking field names (`email`, `api_key`, `password`, `firstname`, `lastname` and similar; never `key`, `token` or `name`) are **counted, not altered** (`detect:` rules).
- **Alerting:** any rule firing on a data endpoint makes the watchdog fail (healthchecks alert). The daily report shows counts per rule per endpoint (`raw_redaction`).
- **Scrub:** limited to personal endpoints, and enforced by the schema. It needs `--expect` equal to the dry-run count. It moves originals to `data/superseded/` (outside the archive and the off-box sync), verifies the only differences are the redacted fields, and deletes originals only through `archive-finalize` with a named confirmer.

**Tests:**
- A match-detail payload with `passes.key` must pass through byte-identical.
- Generic names (`key`, `token`, `name`, `phone`, `username`, `secret`, `password`) on a data endpoint must be untouched.
- Every committed real data payload must be byte-identical after redaction.
- Committed real payloads must include a match-detail response with integer key passes.

### 0.2 Close rule close-v1 (`collector/close.py`; schema `close_rule`, `close_cutoff`, `close_price`)

1. **Cutoff** = min(api-football scheduled kickoff, api-football first-half start, OddsPapi `trueStartTime`), using whichever are present, **minus 2 minutes**.
2. **No close for the fixture** when either holds:
   - the start sources that are present span more than 10 minutes. They disagree, so neither is picked;
   - the api-football status is not FT, AET or PEN.
3. **Per price series**, the close is the record **in force at the cutoff** (the last record strictly before it).
   - It must be `active`. An inactive record in force means **no close**, with no fall-back to an earlier price.
   - Same-timestamp records with different prices mean no close.
4. **Staleness is reported two ways:**
   - age of the last record;
   - age of the last **price change**. Repeated same-price records make the first measure flattering.
5. **G0.5a/b** are computed at the cutoff, not at kickoff. Book silence is measured from the cutoff to the last pre-cutoff record of any market.

**Pre-registered on 26 Oct, before any gate data** (`collector/close.py`, tests in `tests/test_close.py`):

6. **Main line** (totals and spreads families; `close.main_lines`). Among **half lines only** (x.5), the line whose two sides, de-vigged multiplicatively, are closest to 50/50 at **cutoff − 60 min**, with both sides active then. Ties go to the smaller |handicap|, then the lower market id. 1X2 and BTTS have one line. The choice is made before the look-ahead windows (cutoff − 20 and − 10 min), so late moves cannot pick the line.
   - **Amended 30 Oct, before any data:** quarter lines (x.25, x.75) and whole lines (x.0) are excluded as candidates. Their outcomes aren't binary: a quarter line can settle as half-won or half-lost, and a whole line can push, so the binary log loss in §1 isn't defined for them. On Arsenal v Leeds this moves the Asian-handicap main line from −1.25 to the nearer of −1.5 and −0.5. The goals 2.5, corners 9.5 (first half 4.5) and corner handicap −3.5 main lines are unaffected.
   - Families with several 3-way markets (European handicap) have no main line under this rule; they are excluded explicitly (§9.1), not by a silent `null`.
7. **Start sources.** A fixture counts toward G0.5a only when **at least 2** start sources are present (api-football scheduled kickoff plus api-football first-half start and/or OddsPapi `trueStartTime`). With one source, late-kickoff protection is untested. Such fixtures still get a close-v1 close; they are reported, not counted.
   - **Floor:** G0.5a fails if fewer than **80%** of finished, mapped fixtures have ≥ 2 sources, so the gate cannot pass on a small self-selected subset.
   - The G0.5 report shows the number of sources per fixture, and which ones.
8. **Suspended at the cutoff: close-v1b.** 22 of 428 series in the A9 fixture were `suspended_at_cutoff`. The A9 output did not record which markets; `close-report` (v0.5.2) now gives the per-market and, with the `/v4/markets` catalog, per-family breakdown from the archive. My expectation is that most are alternative lines withdrawn as the main line moved, which don't affect G0.5a (main lines only). If instead main 1X2 or main totals lines are routinely suspended at kickoff − 2 min, G0.5a would fail by construction. Fixing that after seeing it would be a data-driven rule change, so the alternative and the rule for adopting it are fixed now:
   - **close-v1b** (sensitivity): when the record in force at the cutoff is suspended, use the last **active** record if it lies within **5 minutes** before the cutoff. Otherwise there is no close. It never uses a record at or after the cutoff (tested by mutation), and equals v1 whenever the in-force record is active.
   - Both are computed for every fixture and reported side by side per market family in the G0.5 report, with the suspended series listed (how long suspended, how far back the last active record was).
   - **Switching rule, applied once, at the first G0.5 evaluation:** a family moves to v1b only if, on the G0.5 sample, its G0.5a usable share is below 80% under v1, at or above 80% under v1b, and main-line suspension is the reason for the shortfall (v1 would reach 80% if suspended main lines counted). Then a new close-rule version (close-v2 = v1b for those families) is registered and applied to **every** fixture of the family, exploration and confirmation alike. Otherwise v1 stays.
   - The decision uses suspension counts only: no outcomes, forecasts or prices against outcomes.
   - **A9 breakdown (26 Oct, one fixture, illustration only):** full-time 1X2 (market 101) closed on all 3 sides. Of the 22 suspended series, 20 had been suspended for 233 minutes to 27 days: withdrawn alternative lines. Only market 10496 was suspended briefly (4.5 min) and is bridged by v1b, giving 292 closes under v1b against 290 under v1. Market 10336 has 28 series, 3 of them suspended for days. This matches the expectation above, but it is one fixture. Families and main lines for 10336, 10496, 101936 and 10178 wait for the A10 catalog.
   - **Why not v1b as primary:** a suspended price is one the book had withdrawn, so it is a weaker benchmark, and a weaker benchmark makes "beating the close" easier. For any family that switches, the share of bridged closes is reported, and a confirmatory result must also hold with the bridged fixtures excluded.

**Enforcement in the schema:**
- `close_cutoff` refuses a cutoff that isn't min(sources) − buffer.
- It refuses a non-excluded cutoff when the sources disagree, or when the match didn't finish.
- `close_price` refuses any record at or after the cutoff, any inactive record, and any record that wasn't in force.

**Tests:**
- 500 random series are poisoned with extreme post-cutoff records; every close is unchanged.
- The same invariants run on every committed real history payload, including Arsenal v Leeds.

**The 25 Oct illustration** (Arsenal v Leeds) was computed under the v0.4 rule: 3,342 records in the final 60 minutes; last record 0.6 minutes before kickoff. It is **void**. Recompute it with RUNBOOK A9.

### 0.3 Look-ahead control (G0.5d)

**Set-up.** Use one pre-registered selection per fixture: the **Over** side of the main full-time goals line (main line as defined in §0.2 item 6). Take its implied probability at cutoff − 20 min, cutoff − 10 min and the close. Sign each move by the outcome: move × (2y − 1).

**I have not adopted the literal test** ("final-window move = 0 within its CI"), because it is wrong as a null:
- Late pre-match money is informative, so the signed move is **positive** even with no leak. That is why closing-line value works at all.
- Across thousands of fixtures, the literal test would stop the project on a healthy market. The test suite shows this case.

**The pre-registered gate instead:**
- **L1:** the upper 95% CI of mean(final-window signed move − previous-window signed move) is below 0.5pp. A leak adds a large final-window move that the previous window lacks.
- **L2 (revised 26 Oct, accepted by you with this change):** a fixture whose selection moves more than 10pp between cutoff − 10 min and the close is **flagged for inspection**. Flags do not stop the project or exclude the fixture by themselves.

The literal final-window mean and CI are still reported alongside.

**L2 inspection (pre-registered; `close.lookahead_gate`):**
- Each flagged fixture gets its record timeline (all series, ± 30 min around the cutoff) and its start sources reviewed by hand. The cause is recorded as one of:
  - `in_play_contamination`: a record used for the close came from after the real start, for example because a start source was wrong;
  - `late_news`: a dated public report before the cutoff explains the move (team news, a late withdrawal);
  - `data_error`: the vendor feed is wrong (an isolated record that reverts, a price inconsistent with the other sides);
  - `unexplained`.
- **G0.5d fails** if L1 fails, or **any** flag is `in_play_contamination`, or flags exceed **2%** of the fixtures in the control (whatever their causes).
- G0.5d is pending while any flag lacks a cause. `late_news`, `data_error` and `unexplained` flags are listed in the report but don't fail the gate below the 2% cap.
- **Flagged fixtures are never dropped.** If contamination is found, it is fixed by a new close-rule version applied to all fixtures, and the control is re-run.
- **Why 2%:** a 10pp move in the last 10 pre-match minutes on a main goals line needs major late news; I expect it in well under 0.5% of top-league fixtures. 2% leaves room for that while catching a systematic leak, which would flag far more. This is a judgment, fixed before any data.

**Status.** The control runs once the OddsPapi outcome allowlist exists (M6). Its function, the gate and their tests are in place now.

### 0.4 Reverse-direction mapping coverage

`mapping.unmapped_upcoming()` reports, per tournament, the api-football fixtures kicking off within 72 hours that have no live mapping. It splits them into two groups:
- `candidate_unmapped`: OddsPapi lists the fixture, but it didn't map. Usually a team isn't allowlisted yet. Relisting won't help; reviewing will.
- `no_candidate`: OddsPapi hasn't listed it.

**Pre-registered thresholds:**
- **Alert:** unmapped share > 10% in a tournament with ≥ 5 upcoming fixtures. `op-fixtures` exits 4 and sends a healthchecks fail.
- **Relist:** `no_candidate` share > 10%, and the last listing is ≥ 48 hours old. The relist goes through the quota guard and budget.

**Cost of the relist rule.** If OddsPapi genuinely lacks some fixtures, it spends up to 1 call per tournament every 2 days, until the monthly budget refuses further calls. That refusal is itself alerted. The budget protects the quota; the alert makes the cost visible.

### 0.5 Scheduled jobs and alerting (C8)

- `active_jobs` in `config/production.toml` is the one list. Today (30 Oct): `af_fixtures`, `af_odds`, `af_results`. `op_account`, `op_fixtures` and `op_history` join at RUNBOOK A11 step 6.
- `plan` creates slots only for active jobs and deletes **pending** slots of inactive ones. Pending slots have no requests, so nothing observed is lost; claimed, complete, failed and missed slots are never touched.
- Only systemd timer runs (`CORNERS_TRIGGER=timer`) claim planned slots and ping healthchecks. Every `run.sh` run gets a new slot noted `by hand`, never on a planned slot's timestamp, and sends no ping (v0.5.3).
- `watchdog` marks misses only for active jobs. It also reports a timer that fired for an inactive job.
- Every watchdog failure is POSTed to healthchecks with a one-line summary per check (job, what, when), so a real miss reads differently from noise. Job failure pings carry the error text, scrubbed of live keys.
- `release.sh` enables exactly the active jobs' timers, disables the rest, and refuses a config without `af_odds`.

### 0.6 Off-box copy (C9)

**Where:** Azure Blob Storage in your personal subscription, in a **separate resource group** with a delete lock, so deleting or losing the VM (or its resource group) can't take the copy with it.

**Access:** the VM's system-assigned managed identity, so no storage key or secret is on the VM. A custom role allows read, list and create, but **not delete**, scoped to the one container; shared-key access is disabled on the account. Blob versioning keeps any overwritten version, and soft delete (30 days) covers your own mistakes. A compromised VM can add data but can't destroy what is already there.

**What is copied, nightly after the local backup:** the backup files (new names each night) and the whole raw archive, with `rclone copy --immutable` (never `sync`; nothing is deleted remotely). Then `rclone check --one-way` must find every local archive file on the remote, or the run fails. Backups older than 90 days are expired remotely by a lifecycle rule run by Azure, not the VM; the archive is kept.

**Monitoring:** `last_backup.json` and `last_offbox.json` feed the daily report (`backup`, `storage`) and the watchdog (no backup in 26 h, off-box failed or stale). `HC_BACKUP` gets the result with a reason on failure.

**Growth and cost.** Measured on 26 Oct: 27.96 MB of compressed archive per 24 h from api-football, 0.84 GB per 30 days; the daily report tracks it (`storage`). The OddsPapi part can be bounded from the one measured payload: the Arsenal v Leeds history was about 7 MB of JSON (59k records). Assuming gzip reduces it 8–15 times, that is roughly 0.5–0.9 MB per fixture: about 0.15–0.3 GB a month forward (about 325 fixtures a month), plus about 0.8–1.5 GB once for the backfill since January. Combined: about 1.0–1.15 GB a month forward, plus the one-off backfill. The 25.5 GB free on the VM lasts about 20 months at that rate (the report's 912 days counts api-football only). Cost at Central India Hot LRS list prices ($0.02 per GB-month; writes $0.055 per 10,000): about 1,200 new files a day cost about $0.20 a month to write, and about 1.1 GB a month of growth costs about $1.7 in storage over the first year. That is roughly $4 for the first year in total.

### 0.7 Incident: op_history, 27–30 Oct (fixed in v0.5.4)

**What happened.** From the first OddsPapi listing (27 Oct) every hourly `op_history` run took about 18 minutes and failed: 22 failed slots in 24 h; 4,117 HTTP 404 and 23 HTTP 429, no 200s.
- **Why it ran at all:** `history_due` selected every listed candidate, mapped or not. The config comment "does nothing until mappings exist" was wrong: it did nothing only while there were no candidates. My error.
- **Why the same fixtures every hour:** it takes the 60 oldest due fixtures (2026-01-01 to 01-04), and a failed request left them due. 1,380 requests = 23 runs × 60. 4,140 responses = 1,380 × 3, because each 404 was retried 3 times at 6 s pacing (the 18 minutes).
- **Why every slot failed:** one failed request failed the slot, and a 404 was treated as a failure.
- **Why `release.sh` didn't disable the timer** after `op_history` left `active_jobs` (corrected 30 Oct). The timer step never ran. `release.sh` is run from the server clone's working tree (`/opt/corners/repo/deploy/release.sh`), and `release.sh` only `git fetch`es, which updates the objects but never that file. So every deploy since b9c09f6, including v0.5.2, v0.5.3 and v0.5.4, ran the b9c09f6 script. That script has no job list, no timer step, no `plan` post-check and no `sync-timers.sh` call. The evidence is the release output: no `job:` lines, no `plan` output, no timer table (28 and 30 Oct). My first explanation, the `while read` loop losing its input, described a real fragility in a loop the VM never ran; v0.5.4 removed it anyway. v0.5.5 makes `release.sh` re-execute itself from `origin/main` (C23).
- **What else the stale script skipped since 26 Oct:** the `af_odds` preflight guard, the timer reconciliation (you managed the timers by hand, and W2's standalone `sync-timers.sh` shows them correct now), and the post-deploy `plan` (the hourly plan timer did it at the next :00). Unit files were installed, because that step predates b9c09f6: `CORNERS_TRIGGER=timer` is live, as V3 showed. Nothing collected was affected.

**Effect on data: none harmful.** The failed rows stay in the raw layer as evidence. They are `usable = 0`, so nothing parses them; nothing in G0.5, the close or analysis reads them; they cost about the size of 4,140 small error bodies in the archive and its off-box copy. They didn't count against the OddsPapi quota. The one consequence is the ledger: 22 failed `op_history` slots. G0.1 counts from the v0.5.4 deploy (§11), and this incident is the explanation on record.

---

## 1. What counts as success

> **Success:** out of sample, on fixtures frozen after pre-registration, a pre-registered rule's forecasts carry information the de-vigged Pinnacle close does not. Two tests must both pass:
> 1. **Encompassing:** in the pooled forecast p_k(b) ∝ c_k^(1−b) · m_k^b over the K outcomes, b > 0 by a one-sided likelihood-ratio test (signed root of the LR statistic; p = 1 − Φ(z)).
> 2. **Scoring rule:** the mean log loss of the forecast m, −log m_y, is lower than that of the close c, −log c_y, by a one-sided paired t-test on the per-fixture differences.
>
> For K = 2, test 1 is exactly `y ~ offset(logit c) + b·(logit m − logit c)`. **1X2 is scored on all three outcomes (K = 3)**, never on one named side: picking a side after exploration would be a researcher choice. Fixed 30 Oct, before any data; code and tests in `analysis/prereg_tests.py`.
>
> Both use Holm correction at the cap of 6 rules (§9).

**Why both tests.** Encompassing detects information even when the forecast is badly scaled. The log-loss test asks whether the forecast, as issued, is better. A forecast that only passes encompassing has information but is not yet a better probability.

**Timing.** The forecast is frozen at a pre-registered time before kickoff (default: our api-football capture 60–120 minutes before kickoff). The close is the price in force at the close-v1 cutoff, 2 minutes before the earliest start source (§0.2). This is deliberately hard: the forecast competes with a price set later, with more information, including team news.

**Not success metrics:** hit rate, accuracy, AUC, calibration alone, EV at capture, and ROI at SportyBet (untested; §7).

---

## 2. Sources: facts and unknowns

| # | Item | Status | Source / how resolved |
|---|---|---|---|
| F1 | api-football pre-match odds: kept 7 days, appear 1–14 days before kickoff, refreshed every 3 hours, `/odds` returns 10 fixtures per page | Documented | api-football beginner's guide |
| F2 | OddsPapi `/v4/historical-odds` is "always free" and never increments the request count. `/v4/account` is always accessible, even after the quota runs out. `/v4/fixtures`, `/v4/tournaments` and `/v4/markets` cost 1 each | Documented | OddsPapi "Requests & Quota" |
| F3 | The same page says the free quota "resets on the 1st day of each month". Your account shows `request_limit: 250` with no renewal date | **Conflict** | Treated as possibly lifetime until confirmed (§3) |
| F4 | History is available **from at least 16 Jan 2026**; fixtures on 1–4 Jan return 404 (the boundary lies between 4 and 16 Jan). Not a rolling window: fixtures from January to October return history. The docs say "since January 2026". Records carry `createdAt`, `price`, `limit` and `active`. The call returns at most 3 bookmakers, has a 5 s cooldown, and finished fixtures return an ETag | Documented; **window measured 30 Oct** | OddsPapi "GET historical-odds"; RUNBOOK W4 probe (17 of 17 usable, 16 Jan to 17 Oct) and W3 (1,380 requests for 60 fixtures dated 1–4 Jan, all 404) |
| F5 | `/v4/fixtures` with `tournamentId` makes `from`/`to` optional. Its 2 s cooldown applies, and each fixture carries `fixtureId`, participant ids and names, `startTime`, `statusId` and external ids | Documented | OddsPapi "GET fixtures" |
| F6 | OddsPapi has a v5 historical endpoint with a `changedAt` field per record | Documented | OddsPapi v5 docs. Not used yet, because you specified v4. Worth one look on day 1 if v4's `createdAt` turns out to be a poll time |
| F7 | api-football lists exactly one bookmaker named `Pinnacle`, id 4 (was U1) | **Resolved 25 Oct** | `af-bootstrap` PASS on your VM |
| U2 | Is `sportId=10` football in OddsPapi? | Unknown (10 is only the docs' example) | Day 1: `op-catalog` output |
| U3 | Does a `tournamentId` listing reach back to January (the previous season)? | Unknown | Day 1: month histogram (RUNBOOK step 9.5) |
| U4 | Does `createdAt` mean the price changed, or that the scraper saw it? | Unknown | G0.5c cross-check against our own api-football Pinnacle snapshots |
| A3 | api-football `score.fulltime` is the 90-minute score, with extra time reported separately | Assumption | Checked on 20 cup ties against a second source before any extra-time-possible fixture is labelled |

---

## 3. OddsPapi quota policy (requirement 9)

**Which calls cost anything.**
- Free: history, plus `/v4/account`, which is checked before every billable call.
- Billable: only the fixture listing (to find OddsPapi fixture IDs) and a one-off catalog.

**How fixtures are discovered.** One `/v4/fixtures?tournamentId=X` call returns that tournament's fixtures. Billable use:

| When | Calls |
|---|---|
| Day 1 | 2 (catalog) + 8 (one per tournament) = **10** |
| Refresh every 28 days | 8 per refresh, ≈ **9/month** |
| History pulls | Free; their delay costs nothing, because history is retained |

**What the guard enforces in code** (with tests):
1. Fetch `/v4/account` (free). If it fails, or lacks integer `request_limit` and `request_count` fields, **refuse**. An absent reading never means "unlimited".
2. Refuse if the **provider's** count, as `/v4/account` reports it, would come within 50 of the limit. That count includes calls made outside this system (your 9 manual probes), so the guard can't be fooled by our own bookkeeping.
3. Refuse if our own billable calls this month would exceed 20.
4. Never retry a billable call automatically.

**Monthly or lifetime: the 1 Nov test.** The docs say the quota resets on the 1st; the account shows no renewal date (§2 F3). On 1 Nov after 00:00 UTC, `op-account` settles it: a provider count below 19 means monthly; 19 means lifetime (RUNBOOK W6). Until then it is treated as lifetime.

**How long 250 lasts if it is lifetime:** about 20 months. The 50 reserve leaves 200 usable requests: 10 on day 1, then about 9 a month.

**What would change this.** Each extra tournament adds about 1 call per month. Widening to lower leagues (§8) is therefore a quota decision as well as a power decision.

---

## 4. What was removed (requirement 1)

**Removed:**
- all `sb_*` tables and their triggers (sessions, sampling, attempts, screenshots, double entry, reconciliation);
- the audit arm;
- the `venue` bookmaker role, and the NG-jurisdiction trigger on the provider-bookmaker map;
- `evaluation.venue_quote_id`.

A test asserts that no `sb_` table exists, and that a bookmaker cannot be given the `venue` role.

**Kept, with tests:**
- raw-layer acceptance and quality flags;
- pagination completeness;
- the slot ledger;
- exact allowlists;
- per-(book, market) quarantine;
- coverage ledger;
- look-ahead guards;
- the 6-rule cap;
- frozen picks;
- singles-only;
- ex-ante extra-time exclusion for corners;
- append-only tables.

---

## 5. Cross-provider fixture mapping (requirement 3)

### Schema

- `provider_team_map` is an exact allowlist from OddsPapi participant **id** to api-football team id. It is one-to-one in both directions, and each row records the commit that approved it. The provider's team name is kept as evidence; it is never used for matching.
- `provider_fixture_map` is one-to-one in both directions: the OddsPapi id is the primary key, and a unique index allows one *live* mapping per api-football fixture. On insert, a trigger requires:
  - **home team:** the OddsPapi home participant maps, through the allowlist, to exactly the fixture's home team;
  - **away team:** the same for the away side. Swapped orientation is refused;
  - **kickoff:** the provider's start time is within 15 minutes of the **latest known** api-football kickoff;
  - **existence:** the OddsPapi fixture was actually listed (a candidate row exists);
  - **evidence:** the evidence text is non-empty.
- Mappings are append-only, except for revocation. Revoking a mapping frees the fixture for a reissued OddsPapi id.

### Workflow (`collector/mapping.py`)

1. `map-propose` writes proposed participant → team pairs to a CSV, from two kinds of evidence in the same configured league:
   - **kickoff:** exactly one api-football fixture kicks off within 15 minutes;
   - **opponent** (v0.5.6): several do, because kickoffs are simultaneous (a UEFA matchday has 9), but one participant is already **approved**, and exactly one of those fixtures has that team on the same side. The other participant is proposed as the other side.

   Only approved pairs propagate, never pairs proposed in the same run, so every step rests on a reviewed mapping. Each round is load, then propose again. Names are evidence for you; nothing is matched by name. Placeholder participants ("Winner Match 2", "Loser …", "TBD") are never proposed.
2. Each row gets a `decision`:
   - `accept`: no conflicting team, or support at least 5 times all conflicting support;
   - `REVIEW: <why>`: weaker conflicts, a team already approved for another key, or one team proposed for two keys.

   You delete wrong rows, resolve REVIEW rows, and commit the CSV.
3. `map-load-teams` loads the CSV exactly as committed in git:
   - all or nothing, and only `accept` rows;
   - placeholders are refused;
   - a row identical to an approved pair is skipped, so a cumulative file can be loaded again;
   - a row that would change an approved pair is refused.
4. `map-apply` maps only fixtures whose two participants are both allowlisted, on one of two recorded bases (`map_basis`, v0.5.7):
   - **kickoff:** exactly one api-football fixture with those teams on the same sides kicks off within 15 minutes. This is unique even at simultaneous kickoffs.
   - **structure:** no kickoff match, but exactly one api-football fixture with that **ordered** pair lies within 4 days of the listed start, and the pair occurs **once** in its league-season, as in every double round robin and in a UEFA league stage. Cup legs and play-offs repeat a pair, so they keep the kickoff rule. The schema refuses a structure mapping of a repeated pair.
   - **Why structure is safe:** identity comes from the reviewed team map plus a pair that can occur only once. The 4-day window only pins the season. The listed start isn't used for the close: close-v1 takes its start sources from api-football and from OddsPapi's `trueStartTime`, and every listing now re-covers the last 35 days, so `trueStartTime` arrives after the match.
   - **Never mapped:** reversed orientation (`orientation_swapped`) is refused and reported. So are repeated pairs, kickoff gaps over 4 days, and missing fixtures (C32).
   - **Kickoff recheck** (mis-join measurement) runs for a structure mapping only against a listing made after the kickoff. Before that, the listed start is the provisional one the mapping was made despite.

### Measured mis-join rate (`v_misjoin_rate`)

| Method | What it checks | Coverage |
|---|---|---|
| `kickoff_recheck` | After full time, final api-football kickoff vs latest OddsPapi start | Automatic, every mapped fixture |
| `price_agreement` | On forward fixtures, our api-football Pinnacle snapshot at time t vs the OddsPapi price in force at t. A mis-joined fixture disagrees almost everywhere | Automatic, every forward fixture with both |
| `final_score` | Used if the OddsPapi payload carries scores (checked on day 1) | All fixtures, if available |
| `manual` | A fixed-seed random sample of 30, checked by you | Sample |

**Gate G0.7.** A rate above 1% on any automatic method fails the gate; so does any manual disagreement. A failure revokes the affected mappings and **stops** the analysis until the cause is found.

**The manual sample is weak on its own.** 0 disagreements out of 30 still leaves an upper 95% bound of 11.6%; 0 out of 150 would give 2.4%. The automatic methods carry the real weight.

---

## 6. Using the Pinnacle history correctly (requirement 4)

**History since January is exploration only.**
- It is stored in `price_history`, separate from our own captures (`quote`).
- Backtests on it are analysis outputs. They are never written to `evaluation`.
- `evaluation` rows are forecasts frozen at **database clock time**: a trigger refuses any `evaluated_at_ms` more than 2 minutes from the database's own clock. A backtest therefore cannot be inserted as if it had been frozen at the time.

**Confirmation uses forward fixtures only**, frozen after `effective_from` of the rule's pre-registration (enforced by trigger).

**Model features are strictly pre-kickoff:**
- `evaluation.feature_cutoff_ms` must not be later than `evaluated_at_ms`, which must be before the latest known kickoff.
- An evaluation is refused if an outcome for that fixture already exists, or if its reference quote was captured after the evaluation.
- For forward forecasts, inputs come only from **our own captures**, never from vendor history, which is fetched after the match.
- A runtime guard (R14, two tests required) asserts that every training example's kickoff precedes the feature cutoff.

**The closing price:** rule close-v1 (§0.2). `v_close` was removed in schema v5. Exploration on history and forward confirmation call the same function (`collector/close.py`), and the schema re-checks every stored close.

### 6.1 The history window (measured 30 Oct)

**Result.** The W4 probe asked one finished fixture per month, January to October 2026, in the EPL and La Liga (UCL qualifiers for July; nothing to ask in June), plus a known-good control. All 17 returned usable history, from 16 Jan to 17 Oct. The 27–30 Oct failures were all for fixtures dated 1–4 Jan (W3). History is therefore available from at least 16 Jan, the boundary lies between 4 and 16 Jan, and the window doesn't roll.

**Consequence: the January case applies.** Exploration runs on history from the first available day, about 1,700 fixtures, revealing effects of √r·τ ≥ 4.2pp (§8). The shorter-window alternatives considered before the probe are dropped. Nothing in confirmation changes.

**Rules that follow** (fixed before any G0.5 or exploration data):
- The first available history day is **measured**, not assumed: the earliest fixture start with a usable history response (`eligibility.history_window_start_ms`).
- Exploration fixtures before that day are excluded from exploration and from G0.5a, and reported as a count.
- `op-backfill` asks for every mapped fixture from 1 Jan; the 1–4 Jan fixtures settle as `no_history` (one recheck after 24 h, then terminal). That costs about 120 free calls.

### G0.5 for the OddsPapi close (pre-registered)

**Per fixture and market family, the close is usable when all three hold:**
- (a) book silence at the **cutoff** ≤ 15 minutes, which shows the vendor was observing at the end;
- (b) every side of the main line (§0.2 item 6) has a close under close-v1 (an active record in force at the cutoff);
- (c) the mapping is live and unaudited-or-agreeing;
- (d) the fixture has at least 2 start sources (§0.2 item 7). Fixtures with fewer are reported and not counted, subject to the 80% floor.

**Settled `no_history` in the G0.5a denominator** (`eligibility.g05a_history_status`, fixed 30 Oct, before any G0.5 data):
- a **forward** fixture, or any fixture on or after the first available history day, with a settled `no_history` stays **in** the denominator as **unusable**, so vendor gaps can't hide behind settled 404s;
- a fixture **before** the first available history day is excluded from G0.5a and reported;
- a fixture not yet asked or answered is pending: not counted until settled.

**The source passes G0.5 when all three hold:**
- (a) **G0.5a:** ≥ 80% of mapped fixtures with ≥ 2 start sources are usable, per family, under close-v1 (or close-v2 for a family switched by the rule in §0.2 item 8); and ≥ 80% of finished mapped fixtures have ≥ 2 start sources;
- (b) **G0.5b:** median book silence ≤ 15 minutes;
- (c) **G0.5c:** on ≥ 200 forward comparison points, the OddsPapi in-force price equals our api-football Pinnacle snapshot to within one tick (|Δ| ≤ 0.01 below odds 2.00, ≤ 0.02 above) at ≥ 95% of points.

G0.5c is the test of `createdAt`'s meaning (U4), and it doubles as the `price_agreement` mis-join check.

**If G0.5 fails, stop:** there is no usable closing benchmark from these sources, and the objective cannot be tested.

**G0.5d (look-ahead control):** §0.3. **If it fails, stop:** the close may contain in-play information. Contamination is fixed by a new close-rule version for all fixtures, then G0.5 is re-run; flagged fixtures are never dropped.

**The G0.5 report shows, per market family:** usable share under v1 and v1b; the main line's suspended sides; the suspended-series list; start sources per fixture; book silence; and the L2 flags with their recorded causes.

**The Arsenal v Leeds check** (RUNBOOK A9) prints the close-v1 report: the cutoff and its basis, records excluded in the buffer and after the cutoff, closes by reason, and staleness by last record and by last change. It is one fixture: an illustration, not the gate.

---

## 7. The honest ceiling (requirement 6)

A positive result yields three things:
- **fair probabilities** for the markets and rules that passed;
- **minimum acceptable prices.** The minimum is 1/p̂ for zero expected value, or 1/p̂ × (1 + required margin) to clear a pre-stated edge. These are recorded per slip (`bet_slip.min_acceptable_price`);
- **a statement of scope:** the result is valid against Pinnacle's close in the tested leagues and market families, over the tested period.

**It does not yield evidence of profit at SportyBet.** That is untested:
- SportyBet prices are not observed.
- Whether SportyBet ever offers a price above the minimum, how often, and at what stake is unknown.
- Account restriction is unknown too. Kaunitz et al. (2017) documented it against exactly this kind of bettor.

**The only execution data** is the `bet_slip` record for each bet actually placed: ticket, time, market, line, odds taken, stake, the minimum acceptable price at the time, and a screenshot hash. It is singles-only, and it must be placed before kickoff and after the linked pick was frozen. Settlement is recorded from SportyBet's own result. These records describe **only the bets placed**: they say nothing about prices you did not take or bets SportyBet would have refused.

---

## 8. Power and timeline (requirement 5)

From `power_output.md` §13.

**The model.** τ is the close's genuine error, the RMS gap between the close and the true probability. A forecast can beat the close only by capturing a share r of that error, from information the close did not price.

**Constraints on the test:**
- one main line per fixture per family, so no pooling of correlated markets;
- Holm correction at the cap of 6, which multiplies the required sample by 1.54.

**Throughput is not yet measured.** It is an assumption until Phase 0 measures it: about 75 fixtures a week in season across the 8 configured competitions, a usable-close share of 0.6–0.9, and about 38 playing weeks per season.

| Scenario | Fixtures needed | Usable fixtures/week | Seasons |
|---|---|---|---|
| τ 3pp, r 0.5 | 6,728 | 68 / 45 | 2.6 / 3.9 |
| τ 2pp, r 0.5 | 15,137 | 68 / 45 | 5.9 / 8.9 |
| τ 2pp, r 0.25 | 30,274 | 68 / 45 | 11.8 / 17.7 |
| τ 1pp, r 0.5 | 60,548 | 68 / 45 | 23.6 / 35.4 |

**Smallest effect detectable at 80% power** (as √r·τ, the RMS mispricing the model captures):

| Sample | Detectable √r·τ |
|---|---|
| Exploration history, about 1,700 fixtures | 4.2pp |
| One season, 2,600 fixtures | 3.4pp |
| Two seasons, 5,200 fixtures | 2.4pp |

**Monte Carlo check** (one season, n = 2,600, α = 0.05/6):

| τ | r | Model's own noise | P(beats close on log loss) | P(encompassing b > 0) |
|---|---|---|---|---|
| 3pp | 0.5 | 1pp | 0.03 | 0.20 |
| 2pp | 0.5 | 0 | 0.04 | 0.10 |
| 0 (null) | — | — | 0.00 | 0.01 (correct size) |

### Plain statement

**At realistic error levels for Pinnacle's close in top leagues (1–2pp, my prior), a confirmed positive answer is out of reach: it would take 6 to 35 seasons.** It is reachable within one or two seasons only if the close is mispriced by about 3pp RMS or more in a way the signal captures. I consider that unlikely for top-league main markets.

**What one season can deliver** is a bounded negative: either "no signal captures more than about 3.4pp RMS of the close's error", or, if an effect that large exists, its detection.

**Levers that change this:**
- **More fixtures.** Pinnacle covers lower leagues, where the close is plausibly less efficient (larger τ). Each extra tournament costs about 1 OddsPapi call a month (§3), so this is a quota decision.
- **More market families per fixture.** Each family adds a separate test, priced inside the cap of 6. It does not add sample to a single test.
- **Exploration as a filter.** It can reveal only effects ≥ 4.2pp. Anything smaller "found" there is presumed noise.

---

## 9. Multiple-comparison budget (fixed in `prereg-0`, before any data)

**Exploration.** Unlimited descriptive analysis on history since January, labelled exploratory. No claims.

**Confirmation.** At most **6 rules** for the life of the project, enforced by the schema. Each rule fixes all of the following before any forward data:
- market family and main-line rule;
- forecast freeze time;
- the forecast function (frozen code commit);
- de-vig method (multiplicative primary, power as a sensitivity; a result must hold under both);
- the stopping n from §8;
- the interim futility look (§10).

**Tests.** Both tests in §1, one-sided, Holm–Bonferroni at family-wise α = 0.05 with m fixed at 6.

**Persistence.** A positive point estimate in at least 3 of the first 4 four-week blocks (Winkelmann et al. 2024).

**BTTS overlap.** Your separate BTTS project is not independent confirmation, and neither is this one for it (v0.3 §4).

### 9.1 Confirmatory families and eligible fixtures (pre-registered 30 Oct, before any data)

**Eligible families** (`close.CONFIRMATORY_FAMILIES`). Full time only; main line per §0.2 item 6:

| Family | Markets | Main line |
|---|---|---|
| `1x2:fulltime` | 101 | the one market (3 sides) |
| `bothteamsscore:fulltime` | 104 | the one market |
| `totals:fulltime` (goals) | 106, 108, 1010, … | half line nearest 50/50 at cutoff − 60 min |
| `spreads:fulltime` (Asian handicap) | 1050–1074 | half line nearest 50/50 |
| `totals-corners:fulltime` | 10787–10809 | half line nearest 50/50 |
| `spreads-corners:fulltime` | 10859–10881 | half line nearest 50/50 |

Each confirmatory rule (at most 6 for the project) picks one of these families at `prereg-1`. **1X2 is scored on all three outcomes** with the K = 3 form of both tests (§1), fixed now rather than at `prereg-1`. Corners are in because their outcomes are collected and their Pinnacle limits are lower, which is where an inefficient close is most plausible.

**Asian handicap and 1X2 overlap.** When the Asian-handicap main half line is ±0.5, it is a function of the 1X2 outcome: home −0.5 wins exactly when the home side wins, and home +0.5 wins on a home win or a draw. In those fixtures the two families aren't independent tests. Holm stays valid under any dependence, so the error rate is protected. But a rule on each family is then not two pieces of evidence, and results are reported with the share of fixtures whose AH main line was ±0.5.

**Excluded families, with reasons** (`close.family_eligibility`; any family not listed is excluded by default):

| Excluded | Reason |
|---|---|
| All first-half and other part-match markets (`:p1`) | Same fixtures, no new sample; excluded to keep the family count, and so the search space, small |
| Correct score, exact score (match and team), winning margin, half-time/full-time | Many outcomes: withdrawn longshots make "every side closed" fail by construction (A9: 3 of 28 and 1 of 9 suspended at the cutoff). A rule for withdrawn outcomes would be one more researcher choice. Excluded rather than patched |
| European handicap (`spreads-european`) | 3-way handicap: the main-line rule is defined for 2-way lines; redundant with the Asian handicap |
| Bookings markets | No bookings outcomes are collected |
| Anytime goalscorer, first goal | Need player or goal-event data that is not collected |
| Team totals (goals and corners) | Redundant with match totals and handicaps |
| Double chance, draw no bet, win to nil, to score, odd/even | Derived from or redundant with 1X2, BTTS or totals; odd/even carries no plausible information |

**UEFA qualifiers don't count** (`collector/eligibility.py`). For the Champions League (2) and Europa League (3), a fixture counts only if it kicks off on or after the first kickoff of a round named `League Stage…` in that league-season. The rule keys on the main competition's start rather than on qualifying-round names, so every later round counts whatever it is called, and August's `Play-offs` don't. Reasons: qualifiers bring many teams outside the team map (mis-join risk), thin Pinnacle markets, and a different competitive context. **Checked on the VM 30 Oct (W7):** both leagues, 2025 and 2026, use `1st/2nd/3rd Qualifying Round`, `Play-offs`, `League Stage - 1` to `- 8`, then `Round of 32` (17 and 19 Feb 2026), `Round of 16`, `Quarter-finals`, `Semi-finals` and `Final`. The test uses these names.

---

## 10. Over-correction check (requirement 7)

### What is different from the previous project

| Previous (V6.2) | This design |
|---|---|
| Success judged by hit rate and calibration | Success is information beyond the **sharp close**, on a **proper scoring rule** (§1) |
| Measured against prices it couldn't verify, at a book it didn't use | Measured against Pinnacle's de-vigged close, validated by G0.5 including a cross-provider price-agreement check |
| Picks chosen as the maximum-probability selection out of ~30 markets | One main line per fixture per family. No within-fixture search |
| In-sample metrics | Exploration on history; confirmation **only** on forward forecasts frozen at database clock time after a signed, timestamped pre-registration (schema-enforced) |
| Rules and thresholds changed as results came in | At most 6 rules for the project's life, with Holm priced at 6; rules frozen on insert |
| Pipeline bugs found weeks later | Raw-first archive, allowlists, pagination and slot-ledger triggers, quarantine, mis-join measurement. Every guard has bad-case and absent-case tests |
| 28-leg accumulators | Singles only, enforced on `bet_slip` |

### Where this is still close to the old question, and the risk that creates

The old project's real question was "do my forecasts beat the market?" This is the same question with a better benchmark.

**The risk is the reverse of the old one.** The test is now strict enough that, at realistic τ, it will almost certainly return "no detectable effect", whatever the truth. That is acceptable only if the project **stops** when the answer is clear, rather than running indefinitely or loosening the benchmark to find something.

### Stop rules (pre-registered)

| # | Stop when | Report |
|---|---|---|
| X1 | G0.5 fails | "No usable closing benchmark from these sources; objective untestable as scoped." |
| X2 | G0.7 mis-join gate fails and the cause isn't found within 2 weeks | Stop analysis until it is fixed |
| X3 | Exploration: no candidate rule shows an encompassing estimate with a point estimate ≥ 2pp RMS-equivalent, and an exploration p < 0.05 before correction | "Nothing in exploration is worth a confirmatory slot." Forward collection continues only if you choose to keep the data |
| X4 | Interim futility at 50% of a rule's planned n: conditional power under the planned effect < 10% | Stop that rule and report it negative. Its slot is not reused |
| X5 | Any proposal to change the benchmark, scoring rule, freeze time or a registered rule after seeing forward data | Stop. A change needs a new pre-registration and a new forward sample, and the old rule is reported as it stands |
| X6 | OddsPapi history becomes unavailable, or the quota guard projects exhaustion before the stopping n | Stop forward confirmation and report what was reached |

**Futility cut-off.** At the end of one full season with no rule past X4, the project reports a bounded negative (§8) and stops. Continuing past one season would need your explicit, written decision, recorded in a pre-registration.

---

## 11. Gates

| Gate | Pass condition |
|---|---|
| G0.0 | `af-bootstrap` finds exactly one bookmaker named `Pinnacle`. If not, Pinnacle snapshots, price agreement and capture-time features are unavailable, and G0.5c cannot run. Report, then decide |
| G0.1 Ledger | Per job: ≥ 99.0% of planned slots complete over the first 14 days the job is active under v0.5.4 or later (by-hand slots excluded), no 2 consecutive misses, every miss explained. Earlier slots are reported, not counted; the 27–30 Oct `op_history` failures are explained in §0.7 |
| G0.3 Off-box | Nightly off-box copy `ok` for 7 consecutive nights, and one restore drill from the remote copy passes `--check-file` (RUNBOOK U5–U6) |
| G0.2 Clock | `clock_ok = 0` on ≤ 1% of 200 responses; chrony p99 offset < 50 ms |
| G0.4 | api-football odds `update` granularity resolved |
| G0.5a–c | §6 (computed at the close-v1 cutoff) |
| G0.5d | Look-ahead control: L1, and L2 inspection with the 2% flag cap (§0.3) |
| G0.9 | Redaction: `archive-verify` reports zero live secrets and `damaged_unrepaired = 0`; real data payloads are byte-identical after redaction; no redaction alert on a data endpoint (§0.1) |
| G0.6 | api-football Pinnacle snapshot coverage ≥ 70% of tracked fixtures per league, with the fixture table as denominator |
| G0.7 | Mis-join (§5) |
| G0.8 | api-football 90-minute score agrees with a second source on 20 of 20 cup ties (A3) |
| S1 | De-vig matches penaltyblog's MULTIPLICATIVE and POWER methods to 1e−9 on a fixed test vector (R13) |
| G2.1 | §1 tests pass, Holm-adjusted, at the stopping n, under both de-vig methods |
| G2.5 | Persistence (§9) |

---

## 12. Predictions (recorded before data)

P0–P12 from v0.2 and v0.3 stand. Those about SportyBet (P1, P7–P12) are void, because SportyBet is no longer measured. P0 now applies to api-football as a closing source only.

| # | Prediction | Falsified if |
|---|---|---|
| P13 | api-football lists exactly one bookmaker named `Pinnacle`. Weak prior | It doesn't. **Resolved 25 Oct: held** (id 4) |
| P14 | OddsPapi Pinnacle history passes G0.5a and G0.5b for EPL fixtures | Either gate fails for EPL |
| P15 | `createdAt` is a change-capture time, and G0.5c passes | G0.5c < 95% |
| P16 | A `tournamentId` listing without `from` does **not** reach back to January | Months before August appear. **Not tested (30 Oct):** the 27 Oct listing passed the one-off `from = 2026-01-01`, so its reaching January says nothing about a listing without `from`. The question is moot: the one-off `from` listing found the history candidates. Closed as untested, not as held or falsified |
| P17 | Exploration produces no candidate meeting X3 for top-league 1X2 or goals O/U | One does |
| P18 | No confirmatory rule passes G2.1 within one season | One does |

---

## 13. Pre-registration mechanics

1. **Sign** the `prereg-*` tag with `git tag -s`, on your PC, with a key that never touches the VM.
2. **Stamp** the tagged commit with OpenTimestamps; keep the `.ots` proof in the repository and off-box, and run `ots upgrade` then `ots verify` once the Bitcoin attestation exists (a few hours).
3. **Insert** the `preregistration` row with the commit hash; the database refuses evaluations frozen before its `effective_from`.

**Without branch protection (free private GitHub repos don't offer it).** Branch protection would only stop history being rewritten on GitHub; it isn't the evidence. The evidence is the signed tag (who fixed the content), the OpenTimestamps proof (the commit existed by time T), and the database's `effective_from` (no forecast frozen earlier counts). A rewritten `main` can't change a timestamped commit hash, and the write-enabled deploy key on the VM can push commits but can't forge your signature or backdate a proof. That is sufficient on two conditions: the signing key stays off the VM, and the `.ots` proofs are kept and verified.

**What root runs at a release (v0.5.6).** Everything under `deploy/` runs as root: `release.sh`, the unit files it installs (a unit's `ExecStart` runs as root unless it names a user), `run.sh` and `sync-timers.sh`. Since C23, `release.sh` comes from `origin/main`, so whoever controls GitHub `main` would control what root runs at your next release. `azureuser` already has sudo, so the new exposure is the GitHub account.
- **Controls:**
  1. 2FA on the GitHub account (you're enabling it).
  2. The server clone's checked-out commit is the last `deploy/` tree you approved. When the script to run, or the target release's `deploy/`, differs from it, `release.sh` prints the diff and the commits, and runs only after you type `yes` at the terminal. Without a terminal it refuses. The approved commit becomes the new reference.
  3. The server clone keeps its separate read-only deploy key.
- **What this does not cover:** a compromised GitHub account changing `collector/` code. That code runs as the unprivileged `corners` user and can damage the data, not the machine. It is visible in `git log` before each release, and the off-box copy can't be deleted from the VM.

**`prereg-0` contents:**
- this document;
- G0.5's definitions, including §0.2 items 6–8 (main line, start sources, close-v1b and its switching rule) and §0.3 (L1, L2 inspection, 2% cap, causes);
- the stop rules;
- the 6-rule budget;
- P13–P18;
- the league list;
- the team allowlist commit.

**`prereg-1`:** the confirmatory rules, chosen from exploration, each with its frozen forecast code commit.

---

## 14. Build status and order

| Step | Status |
|---|---|
| Schema v4, connection contract, schema tests | **Done**; deployed 25 Oct |
| Raw collector, quota guard, slot ledger and watchdog, backup with restore test, systemd units | **Done**; api-football part running since 25 Oct |
| v0.5: redaction and archive scrub; close-v1 and its schema; look-ahead control; null-name catalog; reverse-unmapped alert; schema v5 migration; read-only releases; real-payload tests | **Done**; deployed 25 Oct |
| v0.5.1 hotfix: endpoint-scoped redaction, damage repair (schema v6) | **Done**; deployed and repaired 25 Oct |
| v0.5.2: active jobs, named watchdog failures, timer drift guard; off-box copy and backup status; close-v1b, main-line rule, start-source rule, L2 inspection gate; `close-report` | **Done**; deployed 26 Oct, off-box copy and restore drill verified |
| v0.5.3: by-hand runs get their own slots and never ping; `raw_vs_requests` in the report | **Done**; deployed 27 Oct |
| v0.5.4: mapped-only history, settled 404s, 429 back-off, history health alert, probe, verified timer sync, family eligibility, UEFA stage rule (schema v7) | **Done**; deployed 30 Oct |
| v0.5.5: `release.sh` runs the latest script; `op-backfill --max`; K-outcome confirmatory tests; G0.5a no_history rule | **Done**; deployed 30 Oct |
| v0.5.6: opponent propagation, placeholder exclusion, `decision` column and idempotent loader; confirmation of root-run deploy changes | **Done**; deployed 30 Oct |
| v0.5.7: mapping by structure (schema v8), unmapped reasons, pair-aware 72-hour check, 35-day listing lookback | **Done.** Deploy with RUNBOOK Part Z |
| Mapping tooling (propose, load from git, apply, kickoff audit, manual sample) | **Done** |
| M6: parsers into `quote` and `price_history` with allowlists from the day-1 dumps; `price_agreement` audit | Next. Needs the day-1 dumps |
| M7: G0.5 report, coverage ledger job, de-vig oracle test (R13), training-cutoff guard (R14) | After M6 |
| M8: exploration notebook (history since January) | After M7 |
| `prereg-1`: confirmatory rules | After exploration |

---

## 15. Decisions for you

**Decided on 25 Oct:**
- League breadth: 8 competitions.
- The ceiling (§7) and the stop rules (§10), including the hard stop after one season unless you decide in writing to continue.

**Decided on 26 Oct:**
- Look-ahead gate L1/L2 accepted, with L2 flags going to inspection, never dropped (§0.3).
- G0.5a needs ≥ 2 start sources per counted fixture (§0.2 item 7).

- Confirmed for `prereg-0`: the 2% L2 flag cap and the four causes (§0.3); close-v1b and its switching rule (§0.2 item 8); the 80% floor on fixtures with ≥ 2 start sources (item 7); the main-line rule at cutoff − 60 min, with Over as the look-ahead selection (item 6).

**Decided on 30 Oct:**
- Main lines are half lines only (§0.2 item 6), with the AH ±0.5 / 1X2 overlap stated (§9.1).
- The six confirmatory families and the exclusion list (§9.1); 1X2 scored on all three outcomes with the K = 3 tests (§1).
- UEFA qualifiers excluded by the League Stage date rule (§9.1).
- G0.1 counted per job from the v0.5.4 deploy (§11).
- A history 404 is re-asked once after 24 h, then terminal (§0 C17).
- History window: January to now; the January case applies (§6.1).
- Settled `no_history` in G0.5a: unusable in the denominator from the first available history day; excluded and reported before it (§6).

**Open:** none in the design. UEFA stays in the study: opponent propagation (§5) maps the league stage; qualifiers were already excluded (§9.1). Still to come from you: W6 (the quota reading after 00:00 UTC 1 Nov) and the A8 memory peaks once op_history runs again.

---

**Sources**

- [api-football beginner's guide](https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide)
- [OddsPapi: Requests & Quota](https://oddspapi.io/en/docs/requests-and-quota)
- [OddsPapi: GET historical-odds](https://oddspapi.io/en/docs/get-historical-odds)
- [OddsPapi: GET fixtures](https://oddspapi.io/en/docs/get-fixtures)
- [OddsPapi: GET tournaments](https://oddspapi.io/en/docs/get-tournaments)
- [OddsPapi v5 historical odds docs](https://docs.oddspapi.io/api-reference/fixturesodds/%E5%8E%86%E5%8F%B2%E8%B5%94%E7%8E%87.md)
- [Kaunitz, Zhong & Kreiner (2017)](https://arxiv.org/abs/1710.02824)
- [Winkelmann et al. (2024)](https://ideas.repec.org/a/sae/jospec/v25y2024i1p54-97.html)
- [Azure: Auto-shutdown a VM](https://learn.microsoft.com/en-us/azure/virtual-machines/auto-shutdown-vm)
- [Azure CLI: az vm auto-shutdown](https://learn.microsoft.com/en-us/cli/azure/vm?view=azure-cli-latest)
- [Azure free account FAQ](https://azure.microsoft.com/en-us/free/free-account-faq/)
- [penaltyblog implied-probability methods](https://penaltyblog.readthedocs.io/en/latest/implied/implied.html)
- [OddsPapi API concepts: market and outcome ids](https://docs.oddspapi.io/api-reference/concepts.md)
- [OddsPapi GET historical-odds (history since January 2026; 5,000 ms cooldown; only 200 and 304 documented)](https://oddspapi.io/en/docs/get-historical-odds)
- [Azure retail prices API (Hot LRS, Central India)](https://prices.azure.com/api/retail/prices)
- [healthchecks integrations page template (Test! button)](https://github.com/healthchecks/healthchecks/blob/master/templates/front/channels.html)
- [Azure Blob time-based retention and immutability](https://learn.microsoft.com/en-us/azure/storage/blobs/immutable-time-based-retention-policy-overview)
- [Authorize blob access with Microsoft Entra ID](https://learn.microsoft.com/en-us/azure/storage/blobs/authorize-access-azure-active-directory)
- [rclone Azure Blob backend](https://rclone.org/azureblob/), [downloads](https://rclone.org/downloads/), [release signing](https://rclone.org/release_signing/)
- [Ubuntu noble rclone package (1.60.1)](https://packages.ubuntu.com/noble/rclone)
- [Backblaze B2 pricing](https://www.backblaze.com/cloud-storage/pricing)
- [Healthchecks pinging API (POST body stored with the ping, up to 100 kB)](https://healthchecks.io/docs/http_api/)

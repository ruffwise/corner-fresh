# Pinnacle-Close Benchmark Study — Design v0.5.3

**Status.** Draft for review. Supersedes v0.4. When accepted, commit it as part of `prereg-0`, then sign and timestamp it (§13).
**Date.** 2026-10-26.

**Companion files**

- `db/schema.sql` (v4 base), `db/migrations/005_v05.sql` (to v5) and `db/connect.py`
- `db/migrations/006_v051.sql` (to v6; v0.5.2 adds no migration)
- `collector/` and `deploy/`, plus `RUNBOOK.md` (v0.5.3)
- `tests/`: 234 pass. One more, `test_every_endpoint_has_a_real_payload`, fails **on purpose** until real payloads are exported (RUNBOOK A12)
- `analysis/power.py` and its output `analysis/power_output.md`

Every number in this document comes from `analysis/power.py` unless a source is cited.

**Objective (rescoped).** Determine whether any automated signal produces probabilities that beat Pinnacle's de-vigged closing price on a proper scoring rule, in any market with Pinnacle coverage. Hit rate and accuracy are not success metrics.

---

## 0. Status, and what changed in v0.5 and v0.5.2

**Collection status (26 Oct).** api-football capture has been running since 25 Oct on your Azure VM, now at schema v6 (hotfix v0.5.1):
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

6. **Main line** (totals and spreads families; `close.main_lines`). The line whose two sides, de-vigged multiplicatively, are closest to 50/50 at **cutoff − 60 min**, with both sides active then. Ties go to the smaller |handicap|, then the lower market id. 1X2 and BTTS have one line. The choice is made before the look-ahead windows (cutoff − 20 and − 10 min), so late moves cannot pick the line.
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

- `active_jobs` in `config/production.toml` is the one list. Today: `af_fixtures`, `af_odds`, `af_results`, `op_history`. `op_account` and `op_fixtures` join at RUNBOOK A11.
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

---

## 1. What counts as success

> **Success:** out of sample, on fixtures frozen after pre-registration, a pre-registered rule's forecasts carry information the de-vigged Pinnacle close does not. Two tests must both pass:
> 1. **Encompassing:** in `y ~ offset(logit c) + b·(logit m − logit c)`, b > 0 by a one-sided likelihood-ratio test.
> 2. **Scoring rule:** the mean log loss of the forecast m is lower than that of the close c, by a one-sided paired test.
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
| F4 | History is available since January 2026. Records carry `createdAt`, `price`, `limit` and `active`. The call returns at most 3 bookmakers, has a 5 s cooldown, and finished fixtures return an ETag | Documented | OddsPapi "GET historical-odds" |
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
2. Refuse if the provider count would come within 50 of the limit.
3. Refuse if our own billable calls this month would exceed 20.
4. Never retry a billable call automatically.

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

1. `map-propose` writes proposed participant → team pairs, supported by same-league fixtures with agreeing kickoffs, to a CSV. Conflicts are listed.
2. You review the CSV and commit it.
3. `map-load-teams` loads the approved pairs. It refuses any row that still lists conflicts.
4. `map-apply` maps only fixtures whose two participants are both allowlisted and match exactly one api-football fixture.

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

### G0.5 for the OddsPapi close (pre-registered)

**Per fixture and market family, the close is usable when all three hold:**
- (a) book silence at the **cutoff** ≤ 15 minutes, which shows the vendor was observing at the end;
- (b) every side of the main line (§0.2 item 6) has a close under close-v1 (an active record in force at the cutoff);
- (c) the mapping is live and unaudited-or-agreeing;
- (d) the fixture has at least 2 start sources (§0.2 item 7). Fixtures with fewer are reported and not counted, subject to the 80% floor.

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
| G0.1 Ledger | ≥ 99.0% of planned slots of **active** jobs complete over 14 days (by-hand slots excluded), no 2 consecutive misses, every miss explained |
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
| P16 | A `tournamentId` listing without `from` does **not** reach back to January | Months before August appear |
| P17 | Exploration produces no candidate meeting X3 for top-league 1X2 or goals O/U | One does |
| P18 | No confirmatory rule passes G2.1 within one season | One does |

---

## 13. Pre-registration mechanics

Unchanged:
1. Protect `main`.
2. Sign tags with `git tag -s`.
3. Stamp with OpenTimestamps.
4. Insert the `preregistration` row.

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
| v0.5.3: by-hand runs get their own slots and never ping; `raw_vs_requests` in the report | **Done.** Deploy with RUNBOOK Part V |
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

**Open:** none in the design. Next is the OddsPapi day-1 work (RUNBOOK A10–A11), then M6.

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
- [Azure retail prices API (Hot LRS, Central India)](https://prices.azure.com/api/retail/prices)
- [healthchecks integrations page template (Test! button)](https://github.com/healthchecks/healthchecks/blob/master/templates/front/channels.html)
- [Azure Blob time-based retention and immutability](https://learn.microsoft.com/en-us/azure/storage/blobs/immutable-time-based-retention-policy-overview)
- [Authorize blob access with Microsoft Entra ID](https://learn.microsoft.com/en-us/azure/storage/blobs/authorize-access-azure-active-directory)
- [rclone Azure Blob backend](https://rclone.org/azureblob/), [downloads](https://rclone.org/downloads/), [release signing](https://rclone.org/release_signing/)
- [Ubuntu noble rclone package (1.60.1)](https://packages.ubuntu.com/noble/rclone)
- [Backblaze B2 pricing](https://www.backblaze.com/cloud-storage/pricing)
- [Healthchecks pinging API (POST body stored with the ping, up to 100 kB)](https://healthchecks.io/docs/http_api/)

# Pinnacle-Close Benchmark Study — Design v0.4

**Status.** Draft for review. Supersedes v0.3. When accepted, commit it as part of `prereg-0`, then sign and timestamp it (§13).
**Date.** 2026-10-23.

**Companion files**

- `db/schema.sql` (v0.4) and `db/connect.py`
- `collector/` and `deploy/`, plus `RUNBOOK.md`
- `tests/` (128 tests, all passing)
- `analysis/power.py` and its output `analysis/power_output.md`

Every number in this document comes from `analysis/power.py` unless a source is cited.

**Objective (rescoped).** Determine whether any automated signal produces probabilities that beat Pinnacle's de-vigged closing price on a proper scoring rule, in any market with Pinnacle coverage. Hit rate and accuracy are not success metrics.

---

## 0. Collection status

**I could not start collection from this workspace.** Its network policy blocks both `v3.football.api-sports.io` and `api.oddspapi.io` (the proxy refuses the connection), and no keys are present here.

So the collector is built, tested offline against a fake transport, and packaged with a step-by-step runbook:

- `RUNBOOK.md` steps 1–6 start api-football capture in about 45 minutes on a fresh VPS.
- Steps 7–10 add the OddsPapi Pinnacle history pull.

**Every day before step 6 runs is permanent loss** of api-football odds snapshots, because that feed keeps odds for 7 days only. The odds for Arsenal v Leeds (10 Oct) are already gone. Its OddsPapi history is not lost: history is retained from January 2026.

---

## 1. What counts as success

> **Success:** out of sample, on fixtures frozen after pre-registration, a pre-registered rule's forecasts carry information the de-vigged Pinnacle close does not. Two tests must both pass:
> 1. **Encompassing:** in `y ~ offset(logit c) + b·(logit m − logit c)`, b > 0 by a one-sided likelihood-ratio test.
> 2. **Scoring rule:** the mean log loss of the forecast m is lower than that of the close c, by a one-sided paired test.
>
> Both use Holm correction at the cap of 6 rules (§9).

**Why both tests.** Encompassing detects information even when the forecast is badly scaled. The log-loss test asks whether the forecast, as issued, is better. A forecast that only passes encompassing has information but is not yet a better probability.

**Timing.** The forecast is frozen at a pre-registered time before kickoff (default: our api-football capture 60–120 minutes before kickoff). The close is the price in force at kickoff. This is deliberately hard: the forecast competes with a price set later, with more information, including team news.

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
| U1 | Does api-football list Pinnacle? | **Unknown** | Day 1: `af-bootstrap` asserts exactly one bookmaker named `Pinnacle` (requirement 2) |
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

**The closing price (`v_close`):**
- It is the last **active** Pinnacle record strictly before the final known kickoff.
- Suspended and in-play records are excluded.
- It exposes `market_age_at_ko_ms` (time since that market last changed) and `book_silence_at_ko_ms` (time since Pinnacle last recorded anything for the fixture).
- With no pre-kickoff record, there is no close; one is never invented (tested).

### G0.5 for the OddsPapi close (pre-registered)

**Per fixture and market family, the close is usable when all three hold:**
- (a) `book_silence_at_ko` ≤ 15 minutes, which shows the vendor was observing at the end;
- (b) an active pre-kickoff record exists for every side of the main line;
- (c) the mapping is live and unaudited-or-agreeing.

**The source passes G0.5 when all three hold:**
- (a) **G0.5a:** ≥ 80% of mapped fixtures are usable, per family;
- (b) **G0.5b:** median book silence ≤ 15 minutes;
- (c) **G0.5c:** on ≥ 200 forward comparison points, the OddsPapi in-force price equals our api-football Pinnacle snapshot to within one tick (|Δ| ≤ 0.01 below odds 2.00, ≤ 0.02 above) at ≥ 95% of points.

G0.5c is the test of `createdAt`'s meaning (U4), and it doubles as the `price_agreement` mis-join check.

**If G0.5 fails, stop:** there is no usable closing benchmark from these sources, and the objective cannot be tested.

**The Arsenal v Leeds check** (RUNBOOK step 8) prints records in the final 60 minutes, the last pre-kickoff record, and the largest gap. It is one fixture: an illustration, not the gate.

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
| G0.1 Ledger | ≥ 99.0% of slots complete over 14 days, no 2 consecutive misses, every miss explained |
| G0.2 Clock | `clock_ok = 0` on ≤ 1% of 200 responses; chrony p99 offset < 50 ms |
| G0.4 | api-football odds `update` granularity resolved |
| G0.5a–c | §6 |
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
| P13 | api-football lists exactly one bookmaker named `Pinnacle`. Weak prior | It doesn't |
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
- G0.5's definitions;
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
| Schema v0.4, connection contract, schema tests | **Done** |
| Raw collector (api-football fixtures/odds/results; OddsPapi account/fixtures/history), quota guard, slot ledger and watchdog, backup with restore test, systemd units, runbook | **Done.** Waiting on your deployment |
| Mapping tooling (propose, load, apply, kickoff audit, manual sample) | **Done** |
| Day-1 diagnostics (`af-bootstrap` Pinnacle check, `op-check-fixture` final-hour report) | **Done** |
| M6: parsers into `quote` and `price_history` with allowlists from the day-1 dumps; `price_agreement` audit | Next. Needs the day-1 dumps |
| M7: G0.5 report, coverage ledger job, de-vig oracle test (R13), training-cutoff guard (R14) | After M6 |
| M8: exploration notebook (history since January) | After M7 |
| `prereg-1`: confirmatory rules | After exploration |

---

## 15. Decisions for you

1. **Deploy now** (RUNBOOK steps 1–6). Nothing else unlocks collection.
2. **League breadth.** The 8 competitions in `config.example.toml` (top 5, Championship, UCL, UEL) are broader than the EPL, Championship and UCL you named. Breadth is the main power lever, and each tournament costs about 1 OddsPapi call a month. Adding lower leagues raises both power and quota use.
3. **Accept the ceiling** (§7) and **the stop rules** (§10), including the one-season futility cut-off.

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
- [penaltyblog implied-probability methods](https://penaltyblog.readthedocs.io/en/latest/implied/implied.html)

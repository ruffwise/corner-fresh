
## 1. Realised ROI at SportyBet, singles

SD per unit-stake bet = odds * sqrt(p(1-p)), p = (1+edge)/odds.

| odds | true edge | SD/bet | bets for 80% power | years at 2 bets/wk | years at 5 bets/wk |
|---|---|---|---|---|---|
| 1.50 | 2% | 0.700 | 9,607 | 92 | 37 |
| 1.50 | 3% | 0.696 | 4,222 | 41 | 16 |
| 1.50 | 5% | 0.687 | 1,483 | 14 | 6 |
| 1.85 | 2% | 0.920 | 16,612 | 160 | 64 |
| 1.85 | 3% | 0.919 | 7,366 | 71 | 28 |
| 1.85 | 5% | 0.917 | 2,637 | 25 | 10 |
| 2.20 | 2% | 1.097 | 23,617 | 227 | 91 |
| 2.20 | 3% | 1.098 | 10,510 | 101 | 40 |
| 2.20 | 5% | 1.099 | 3,791 | 36 | 15 |

## 2. CLV against the feed's last pre-kickoff snapshot

Variance is not the problem; bias is. If only a fraction `a` of the
informative closing movement happens before the last captured snapshot,
the observed mean CLV is a*delta. n scales as 1/a^2, and `a` cannot be
estimated from inside the feed when timestamps are per-fixture.

ASSUMPTION: per-bet SD of log-CLV in {3%, 5%, 8%}; true mean CLV delta = 2%.

| SD/bet | a = 1.0 | a = 0.7 | a = 0.5 |
|---|---|---|---|
| 3% | 18 | 36 | 71 |
| 5% | 49 | 100 | 196 |
| 8% | 126 | 256 | 502 |

Staleness of the last pre-kickoff snapshot if kickoffs are uniform
relative to a 180-min refresh phase: Uniform(0,180) min ->
median 90 min, P(staleness > 60 min) = 67%.

## 3. Forecast encompassing: model vs market at prediction time

True p = q + d, d ~ N(0, tau); a model capturing share r of var(d).
Expected log-loss gain per obs ~ r*tau^2 / (2 q(1-q)), q ~ 0.5.
LRT noncentrality ~ 2 n * gain.

| tau (pp) | share captured r | gain (nats/obs) | n for 80% power |
|---|---|---|---|
| 2 | 1.0 | 0.00080 | 4,906 |
| 2 | 0.5 | 0.00040 | 9,811 |
| 3 | 1.0 | 0.00180 | 2,180 |
| 3 | 0.5 | 0.00090 | 4,360 |
| 5 | 1.0 | 0.00500 | 785 |
| 5 | 0.5 | 0.00250 | 1,570 |

Monte Carlo check (tau=3pp, r=1, n=2,180, 400 reps): empirical power = 0.79 (target 0.80).

## 4. Lag test on the manual sample

SB_fair = beta*ref_now + (1-beta)*ref_prev + e, fitted only on fixtures
where the reference moved >= 2pp between the two refreshes.
SE(beta) = sigma_e / sqrt(n_moved * E[move^2]); RMS move = 3pp (ASSUMPTION).
Target: detect a lag share (1-beta) of 0.30 vs 0.

| captured fixtures | share with move | sigma_e (pp) | n_moved | SE(beta) | power for lag share 0.30 |
|---|---|---|---|---|---|
| 300 | 10% | 1.0 | 30 | 0.061 | 1.00 |
| 300 | 10% | 1.5 | 30 | 0.091 | 0.91 |
| 300 | 10% | 2.5 | 30 | 0.152 | 0.50 |
| 300 | 20% | 1.0 | 60 | 0.043 | 1.00 |
| 300 | 20% | 1.5 | 60 | 0.065 | 1.00 |
| 300 | 20% | 2.5 | 60 | 0.108 | 0.80 |
| 300 | 30% | 1.0 | 90 | 0.035 | 1.00 |
| 300 | 30% | 1.5 | 90 | 0.053 | 1.00 |
| 300 | 30% | 2.5 | 90 | 0.088 | 0.93 |

## 5. Frequency of SportyBet prices with EV >= +2% vs reference

Per-fixture indicator, n = 300 captured fixtures, Clopper-Pearson 95% CI.

| true share pi | expected hits | 95% CI (exact) |
|---|---|---|
| 2% | 6 | [0.7%, 4.3%] |
| 5% | 15 | [2.8%, 8.1%] |
| 10% | 30 | [6.8%, 14.0%] |

## 6. Reference (de-vigged) calibration: mean bias vs outcomes

- Detect a 2pp mean bias, 80% power: n = 4,906 fixtures
- Detect a 3pp mean bias, 80% power: n = 2,180 fixtures
- 95% CI half-width of 2pp on the mean residual: n = 2,401 fixtures

## 7. Anchor validation (relative proper scoring, paired by fixture)

If the sharp close is truth and another book's close = truth + e,
mean paired log-loss gain = E[e^2]/(2q(1-q)), SD of the paired
difference ~ RMS(e)/sqrt(q(1-q)). q ~ 0.5.

| RMS disagreement (pp) | mean gain (nats) | SD of paired diff | fixtures for 80% power |
|---|---|---|---|
| 1.0 | 0.00020 | 0.020 | 78,489 |
| 1.5 | 0.00045 | 0.030 | 34,884 |
| 3.0 | 0.00180 | 0.060 | 8,721 |

## 8. Timeline in weeks under throughput scenarios

ASSUMPTION: manual SportyBet captures/week in {15, 25, 40};
automated fixtures/week with an eligible reference corner line in {100, 200, 400}.
Phase 0 measures both; these rows are re-run then.

| question | n | low | mid | high |
|---|---|---|---|---|
| SB price study + lag test (n=300 captured) | 300 | 20 | 12 | 8 |
| Reference calibration, +/-2pp CI (n=2,401) | 2,401 | 24 | 12 | 6 |
| Encompassing, tau=3pp, r=0.5 | 4,360 | 44 | 22 | 11 |
| Anchor validation, RMS disagreement 1.5pp | 34,884 | 349 | 174 | 87 |
| Realised ROI at SB, 3% edge, odds 1.85, 2 bets/wk | 7,366 | 3,683 | 3,683 | 3,683 |

## 9. De-vig method sensitivity (favourite side fair probability)

| prices (fav, dog) | overround | multiplicative | power | difference (pp) |
|---|---|---|---|---|
| 1.85 / 1.95 | 5.34% | 0.5132 | 0.5142 | 0.11 |
| 1.70 / 2.10 | 6.44% | 0.5526 | 0.5578 | 0.52 |
| 1.40 / 2.80 | 7.14% | 0.6667 | 0.6853 | 1.87 |
| 1.25 / 3.75 | 6.67% | 0.7500 | 0.7765 | 2.65 |

## 10. Null false-hit rate (no true edge anywhere)

Model (ASSUMPTIONS, replaced by Phase 0 measurements):
- SportyBet prices = TRUE probability loaded with a proportional margin M
  (so every selection has true EV = 1/(1+M) - 1 < 0: no edge exists);
- reference fair probability = truth + noise in logit space, SD sigma
  (quoted in pp at p = 0.5), shared fixture component rho = 0.5;
- K lines per fixture spread over p in [0.2, 0.8], both sides searched;
- a fixture is a 'hit' if ANY selection shows EV_obs >= +2%;
- dual condition: hit must hold against two references whose noises
  correlate r (consensus copies Pinnacle, so r is not 0).

| sigma (pp) | lines K | margin M | single reference | dual, r = 0.5 | dual, r = 0.8 |
|---|---|---|---|---|---|
| 1 | 1 | 5% | 0.1% | 0.0% | 0.0% |
| 1 | 5 | 5% | 3.5% | 0.5% | 1.6% |
| 1 | 20 | 5% | 8.3% | 1.2% | 3.3% |
| 1 | 60 | 5% | 16.9% | 3.1% | 7.5% |
| 2 | 1 | 5% | 7.7% | 1.7% | 3.6% |
| 2 | 5 | 5% | 40.7% | 15.4% | 24.8% |
| 2 | 20 | 5% | 74.5% | 35.0% | 51.8% |
| 2 | 60 | 5% | 95.3% | 59.6% | 78.9% |
| 3 | 1 | 5% | 23.8% | 8.1% | 13.6% |
| 3 | 5 | 5% | 69.5% | 35.7% | 49.8% |
| 3 | 20 | 5% | 97.2% | 70.1% | 85.5% |
| 3 | 60 | 5% | 100.0% | 92.6% | 98.5% |
| 1 | 1 | 8% | 0.0% | 0.0% | 0.0% |
| 1 | 5 | 8% | 0.2% | 0.0% | 0.1% |
| 1 | 20 | 8% | 0.4% | 0.0% | 0.1% |
| 1 | 60 | 8% | 1.1% | 0.1% | 0.3% |
| 2 | 1 | 8% | 1.2% | 0.1% | 0.4% |
| 2 | 5 | 8% | 16.6% | 4.4% | 8.5% |
| 2 | 20 | 8% | 35.9% | 10.4% | 19.7% |
| 2 | 60 | 8% | 60.1% | 21.5% | 37.0% |
| 3 | 1 | 8% | 9.2% | 2.1% | 4.4% |
| 3 | 5 | 8% | 45.0% | 17.8% | 28.0% |
| 3 | 20 | 8% | 79.3% | 39.7% | 57.2% |
| 3 | 60 | 8% | 97.3% | 65.5% | 83.8% |

## 11. Timeline with automated SportyBet capture (vendor route)

ASSUMPTION: fixtures/week with SportyBet prices AND an eligible reference
in {150, 400, 800}; rule-trigger rate pi per fixture in {2%, 5%}.
Paper picks at captured SportyBet prices count toward outcome-based ROI;
real placements are needed only for execution realism (slippage, limits).

| fixtures/wk | pi | survey n=300 (wk) | 40 rule picks (wk) | 100 rule picks (wk) | ROI 3% edge (wk) | ROI 5% edge (wk) |
|---|---|---|---|---|---|---|
| 150 | 2% | 2.0 | 13.3 | 33.3 | 2,455 | 879 |
| 150 | 5% | 2.0 | 5.3 | 13.3 | 982 | 352 |
| 400 | 2% | 0.8 | 5.0 | 12.5 | 921 | 330 |
| 400 | 5% | 0.8 | 2.0 | 5.0 | 368 | 132 |
| 800 | 2% | 0.4 | 2.5 | 6.2 | 460 | 165 |
| 800 | 5% | 0.4 | 1.0 | 2.5 | 184 | 66 |

(ROI sample sizes at odds 1.85: 3% edge n = 7,366; 5% edge n = 2,637.)

## 12. CLV confirmation sample size (only if G0.5 does not kill CLV)

Selection uses the capture-time reference; evaluation uses the close, so
selection noise and evaluation noise are separate measurements.

| per-pick SD of log-CLV | true mean CLV | picks for 80% power |
|---|---|---|
| 3% | 1% | 71 |
| 3% | 2% | 18 |
| 5% | 1% | 196 |
| 5% | 2% | 49 |
| 8% | 1% | 502 |
| 8% | 2% | 126 |

## 13. v0.4 — encompassing the Pinnacle close (primary metric)

Truth p = c + d, d ~ N(0, tau) in probability, c = de-vigged close.
tau is the close's genuine error; a model can only beat the close by
capturing part r of var(d) from information the close did not price.
Test: y ~ offset(logit c) + b (logit m - logit c); H0 b = 0 (LRT, 1 df).
One main line per fixture per market family (no within-fixture pooling).

Holm at the cap of 6 rules (strongest rule tested at alpha 0.05/6): sample-size factor 1.54

| tau (pp) | r | n (alpha .05) | n (alpha .05/6) |
|---|---|---|---|
| 1 | 0.25 | 78,489 | 121,096 |
| 1 | 0.50 | 39,244 | 60,548 |
| 1 | 1.00 | 19,622 | 30,274 |
| 2 | 0.25 | 19,622 | 30,274 |
| 2 | 0.50 | 9,811 | 15,137 |
| 2 | 1.00 | 4,906 | 7,568 |
| 3 | 0.25 | 8,721 | 13,455 |
| 3 | 0.50 | 4,360 | 6,728 |
| 3 | 1.00 | 2,180 | 3,364 |

Smallest detectable effect at a fixed sample (80% power, alpha .05/6):
effective RMS mispricing captured = sqrt(r) * tau.

| fixtures with usable close | detectable sqrt(r)*tau (pp) |
|---|---|
| 1,700 | 4.22 |
| 2,600 | 3.41 |
| 5,200 | 2.41 |
| 10,400 | 1.71 |

Throughput (ASSUMPTION until Phase 0 measures it): top-5 leagues +
Championship + UCL + UEL ~ 75 fixtures/week in season; usable-close share
in {0.6, 0.9}; ~38 playing weeks per season.

| scenario | usable fixtures/week | n needed | weeks | seasons |
|---|---|---|---|---|
| tau 3pp, r 0.5 | 68 | 6,728 | 100 | 2.6 |
| tau 3pp, r 0.5 | 45 | 6,728 | 150 | 3.9 |
| tau 2pp, r 0.5 | 68 | 15,137 | 224 | 5.9 |
| tau 2pp, r 0.5 | 45 | 15,137 | 336 | 8.9 |
| tau 2pp, r 0.25 | 68 | 30,274 | 449 | 11.8 |
| tau 2pp, r 0.25 | 45 | 30,274 | 673 | 17.7 |
| tau 1pp, r 0.5 | 68 | 60,548 | 897 | 23.6 |
| tau 1pp, r 0.5 | 45 | 60,548 | 1,346 | 35.4 |

Monte Carlo, one season (n = 2,600), alpha .05/6, model = close + (known part of the close error) + own noise:

| tau (pp) | r | model's own noise (pp) | P(beats close on log loss) | P(encompassing b > 0) |
|---|---|---|---|---|
| 2 | 0.5 | 0 | 0.04 | 0.10 |
| 2 | 0.5 | 1 | 0.01 | 0.05 |
| 3 | 0.5 | 1 | 0.03 | 0.20 |
| 2 | 0.25 | 1 | 0.00 | 0.01 |
| 0 | 0.0 | 1 | 0.00 | 0.01 |

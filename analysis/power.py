"""Power and timeline calculations for the corners edge study.

Every number quoted in docs/DESIGN.md section 9 comes from this script.
Run:  python analysis/power.py  > analysis/power_output.md

All inputs marked ASSUMPTION are priors, not measurements. Phase 0/1 data
replaces them; re-run the script and re-check the gates when that happens.
Conventions: alpha = 0.05 two-sided, power = 0.80, unless stated.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats

Z_A = stats.norm.ppf(0.975)   # 1.95996
Z_B = stats.norm.ppf(0.80)    # 0.84162
ZZ = Z_A + Z_B                # 2.80158
CHI2_CRIT_1DF = stats.chi2.ppf(0.95, 1)  # 3.8415


def n_for_mean(effect: float, sd: float, deff: float = 1.0) -> float:
    """Sample size to detect a mean shift `effect` with per-unit SD `sd`."""
    return deff * (ZZ * sd / effect) ** 2


def lrt_power(noncentrality: float) -> float:
    """Power of a 1-df likelihood-ratio test with the given noncentrality."""
    return 1 - stats.ncx2.cdf(CHI2_CRIT_1DF, 1, noncentrality)


def n_for_lrt(delta_nats: float) -> float:
    """n so that a 1-df LRT has 80% power when the alternative improves
    expected log loss by `delta_nats` per observation (noncentrality ~ 2*n*delta)."""
    lam = 7.8489  # noncentrality giving 80% power at alpha=.05, 1 df
    return lam / (2 * delta_nats)


def section(title: str) -> None:
    print(f"\n## {title}\n")


# ---------------------------------------------------------------------------
# 1. Realised ROI at SportyBet (singles)
# ---------------------------------------------------------------------------
section("1. Realised ROI at SportyBet, singles")
print("SD per unit-stake bet = odds * sqrt(p(1-p)), p = (1+edge)/odds.\n")
print("| odds | true edge | SD/bet | bets for 80% power | years at 2 bets/wk | years at 5 bets/wk |")
print("|---|---|---|---|---|---|")
for odds in (1.50, 1.85, 2.20):
    for edge in (0.02, 0.03, 0.05):
        p = (1 + edge) / odds
        sd = odds * math.sqrt(p * (1 - p))
        n = n_for_mean(edge, sd)
        print(f"| {odds:.2f} | {edge:.0%} | {sd:.3f} | {n:,.0f} | {n/2/52:,.0f} | {n/5/52:,.0f} |")

# ---------------------------------------------------------------------------
# 2. CLV with a ~3-hour refresh
# ---------------------------------------------------------------------------
section("2. CLV against the feed's last pre-kickoff snapshot")
print("Variance is not the problem; bias is. If only a fraction `a` of the\n"
      "informative closing movement happens before the last captured snapshot,\n"
      "the observed mean CLV is a*delta. n scales as 1/a^2, and `a` cannot be\n"
      "estimated from inside the feed when timestamps are per-fixture.\n")
print("ASSUMPTION: per-bet SD of log-CLV in {3%, 5%, 8%}; true mean CLV delta = 2%.\n")
print("| SD/bet | a = 1.0 | a = 0.7 | a = 0.5 |")
print("|---|---|---|---|")
for sd in (0.03, 0.05, 0.08):
    row = [f"{n_for_mean(0.02 * a, sd):,.0f}" for a in (1.0, 0.7, 0.5)]
    print(f"| {sd:.0%} | " + " | ".join(row) + " |")
print("\nStaleness of the last pre-kickoff snapshot if kickoffs are uniform\n"
      "relative to a 180-min refresh phase: Uniform(0,180) min ->\n"
      f"median {180/2:.0f} min, P(staleness > 60 min) = {(180-60)/180:.0%}.")

# ---------------------------------------------------------------------------
# 3. Forecast-encompassing test (diagnostic only)
# ---------------------------------------------------------------------------
section("3. Forecast encompassing: model vs market at prediction time")
print("True p = q + d, d ~ N(0, tau); a model capturing share r of var(d).\n"
      "Expected log-loss gain per obs ~ r*tau^2 / (2 q(1-q)), q ~ 0.5.\n"
      "LRT noncentrality ~ 2 n * gain.\n")
print("| tau (pp) | share captured r | gain (nats/obs) | n for 80% power |")
print("|---|---|---|---|")
enc_rows = {}
for tau in (0.02, 0.03, 0.05):
    for r in (1.0, 0.5):
        gain = r * tau ** 2 / (2 * 0.25)
        n = n_for_lrt(gain)
        enc_rows[(tau, r)] = n
        print(f"| {tau*100:.0f} | {r:.1f} | {gain:.5f} | {n:,.0f} |")


def simulate_encompassing(n: int, tau: float, reps: int, seed: int = 7) -> float:
    """Monte Carlo check of the analytic n: offset logistic regression
    y ~ offset(logit q) + b * (logit m - logit q), LRT on b."""
    rng = np.random.default_rng(seed)
    rejections = 0
    for _ in range(reps):
        q = rng.uniform(0.40, 0.60, n)
        d = rng.normal(0, tau, n)
        p = np.clip(q + d, 0.01, 0.99)
        y = (rng.uniform(size=n) < p).astype(float)
        off = np.log(q / (1 - q))
        z = np.log(p / (1 - p)) - off
        b = 0.0
        for _ in range(25):  # Newton
            eta = off + b * z
            mu = 1 / (1 + np.exp(-eta))
            grad = np.sum((y - mu) * z)
            hess = np.sum(mu * (1 - mu) * z * z)
            step = grad / hess
            b += step
            if abs(step) < 1e-10:
                break

        def ll(bb: float) -> float:
            eta = off + bb * z
            return float(np.sum(y * eta - np.log1p(np.exp(eta))))

        if 2 * (ll(b) - ll(0.0)) > CHI2_CRIT_1DF:
            rejections += 1
    return rejections / reps


n_check = int(round(enc_rows[(0.03, 1.0)]))
sim_power = simulate_encompassing(n_check, 0.03, reps=400)
print(f"\nMonte Carlo check (tau=3pp, r=1, n={n_check:,}, 400 reps): "
      f"empirical power = {sim_power:.2f} (target 0.80).")

# ---------------------------------------------------------------------------
# 4. Lag test: does SportyBet track the current or the previous sharp price?
# ---------------------------------------------------------------------------
section("4. Lag test on the manual sample")
print("SB_fair = beta*ref_now + (1-beta)*ref_prev + e, fitted only on fixtures\n"
      "where the reference moved >= 2pp between the two refreshes.\n"
      "SE(beta) = sigma_e / sqrt(n_moved * E[move^2]); RMS move = 3pp (ASSUMPTION).\n"
      "Target: detect a lag share (1-beta) of 0.30 vs 0.\n")
print("| captured fixtures | share with move | sigma_e (pp) | n_moved | SE(beta) | power for lag share 0.30 |")
print("|---|---|---|---|---|---|")
for n_fix in (300,):
    for f_move in (0.10, 0.20, 0.30):
        for sig in (1.0, 1.5, 2.5):
            n_moved = n_fix * f_move
            se = (sig / 100) / math.sqrt(n_moved * 0.03 ** 2)
            power = 1 - stats.norm.cdf(Z_A - 0.30 / se) + stats.norm.cdf(-Z_A - 0.30 / se)
            print(f"| {n_fix} | {f_move:.0%} | {sig} | {n_moved:.0f} | {se:.3f} | {power:.2f} |")

# ---------------------------------------------------------------------------
# 5. Opportunity frequency (price-only, no outcomes needed)
# ---------------------------------------------------------------------------
section("5. Frequency of SportyBet prices with EV >= +2% vs reference")
print("Per-fixture indicator, n = 300 captured fixtures, Clopper-Pearson 95% CI.\n")
print("| true share pi | expected hits | 95% CI (exact) |")
print("|---|---|---|")
for pi in (0.02, 0.05, 0.10):
    k = round(300 * pi)
    lo = stats.beta.ppf(0.025, k, 300 - k + 1) if k > 0 else 0.0
    hi = stats.beta.ppf(0.975, k + 1, 300 - k)
    print(f"| {pi:.0%} | {k} | [{lo:.1%}, {hi:.1%}] |")

# ---------------------------------------------------------------------------
# 6. Reference calibration on the automated sample
# ---------------------------------------------------------------------------
section("6. Reference (de-vigged) calibration: mean bias vs outcomes")
sd_bin = 0.5
for b in (0.02, 0.03):
    print(f"- Detect a {b*100:.0f}pp mean bias, 80% power: n = {n_for_mean(b, sd_bin):,.0f} fixtures")
print(f"- 95% CI half-width of 2pp on the mean residual: n = {(Z_A*sd_bin/0.02)**2:,.0f} fixtures")

# ---------------------------------------------------------------------------
# 7. Anchor validation: sharp close vs other books' closes, paired log loss
# ---------------------------------------------------------------------------
section("7. Anchor validation (relative proper scoring, paired by fixture)")
print("If the sharp close is truth and another book's close = truth + e,\n"
      "mean paired log-loss gain = E[e^2]/(2q(1-q)), SD of the paired\n"
      "difference ~ RMS(e)/sqrt(q(1-q)). q ~ 0.5.\n")
print("| RMS disagreement (pp) | mean gain (nats) | SD of paired diff | fixtures for 80% power |")
print("|---|---|---|---|")
for e in (0.01, 0.015, 0.03):
    gain = e ** 2 / (2 * 0.25)
    sd = e / math.sqrt(0.25)
    print(f"| {e*100:.1f} | {gain:.5f} | {sd:.3f} | {n_for_mean(gain, sd):,.0f} |")

# ---------------------------------------------------------------------------
# 8. Timeline
# ---------------------------------------------------------------------------
section("8. Timeline in weeks under throughput scenarios")
print("ASSUMPTION: manual SportyBet captures/week in {15, 25, 40};\n"
      "automated fixtures/week with an eligible reference corner line in {100, 200, 400}.\n"
      "Phase 0 measures both; these rows are re-run then.\n")
manual = (15, 25, 40)
auto = (100, 200, 400)
need = {
    "SB price study + lag test (n=300 captured)": (300, manual),
    "Reference calibration, +/-2pp CI (n=2,401)": (2401, auto),
    "Encompassing, tau=3pp, r=0.5": (enc_rows[(0.03, 0.5)], auto),
    "Anchor validation, RMS disagreement 1.5pp": (n_for_mean(0.015**2/0.5, 0.015/0.5), auto),
}
print("| question | n | low | mid | high |")
print("|---|---|---|---|---|")
for name, (n, rates) in need.items():
    cells = [f"{n / r:,.0f}" for r in rates]
    print(f"| {name} | {n:,.0f} | " + " | ".join(cells) + " |")
p = (1.03) / 1.85
n_roi = n_for_mean(0.03, 1.85 * math.sqrt(p * (1 - p)))
print(f"| Realised ROI at SB, 3% edge, odds 1.85, 2 bets/wk | {n_roi:,.0f} | "
      f"{n_roi/2:,.0f} | {n_roi/2:,.0f} | {n_roi/2:,.0f} |")

# ---------------------------------------------------------------------------
# 9. De-vig sensitivity: multiplicative vs power method, two-way markets
# ---------------------------------------------------------------------------
section("9. De-vig method sensitivity (favourite side fair probability)")
from scipy.optimize import brentq  # noqa: E402

print("| prices (fav, dog) | overround | multiplicative | power | difference (pp) |")
print("|---|---|---|---|---|")
for o in ((1.85, 1.95), (1.70, 2.10), (1.40, 2.80), (1.25, 3.75)):
    imp = [1 / x for x in o]
    s = sum(imp)
    mult = imp[0] / s
    k = brentq(lambda kk: sum(x ** kk for x in imp) - 1, 0.5, 3)
    pw = imp[0] ** k
    print(f"| {o[0]:.2f} / {o[1]:.2f} | {s-1:.2%} | {mult:.4f} | {pw:.4f} | {(pw-mult)*100:.2f} |")

# ---------------------------------------------------------------------------
# 10. Winner's curse: null false-hit rate of an "any selection clears +2%" rule
# ---------------------------------------------------------------------------
section("10. Null false-hit rate (no true edge anywhere)")
print("Model (ASSUMPTIONS, replaced by Phase 0 measurements):\n"
      "- SportyBet prices = TRUE probability loaded with a proportional margin M\n"
      "  (so every selection has true EV = 1/(1+M) - 1 < 0: no edge exists);\n"
      "- reference fair probability = truth + noise in logit space, SD sigma\n"
      "  (quoted in pp at p = 0.5), shared fixture component rho = 0.5;\n"
      "- K lines per fixture spread over p in [0.2, 0.8], both sides searched;\n"
      "- a fixture is a 'hit' if ANY selection shows EV_obs >= +2%;\n"
      "- dual condition: hit must hold against two references whose noises\n"
      "  correlate r (consensus copies Pinnacle, so r is not 0).\n")


def null_hit_rate(sigma_pp, k, margin, rho=0.5, dual_r=None, n=20000, seed=11):
    rng = np.random.default_rng(seed)
    p = np.full(1, 0.5) if k == 1 else np.linspace(0.2, 0.8, k)
    s = sigma_pp * 4 / 100  # logit SD giving sigma_pp at p = 0.5

    def ref_noise():
        zf = rng.standard_normal((n, 1))
        zk = rng.standard_normal((n, k))
        return s * (np.sqrt(rho) * zf + np.sqrt(1 - rho) * zk)

    e1 = ref_noise()
    logit = np.log(p / (1 - p))

    def hits(eps):
        pr_over = 1 / (1 + np.exp(-(logit + eps)))
        ev_over = pr_over / (p * (1 + margin)) - 1
        ev_under = (1 - pr_over) / ((1 - p) * (1 + margin)) - 1
        return ev_over >= 0.02, ev_under >= 0.02

    h_o, h_u = hits(e1)
    if dual_r is not None:
        e_ind = ref_noise()
        e2 = dual_r * e1 + np.sqrt(1 - dual_r ** 2) * e_ind
        g_o, g_u = hits(e2)
        h_o, h_u = h_o & g_o, h_u & g_u
    return float(np.mean((h_o | h_u).any(axis=1)))


print("| sigma (pp) | lines K | margin M | single reference | dual, r = 0.5 | dual, r = 0.8 |")
print("|---|---|---|---|---|---|")
for margin in (0.05, 0.08):
    for sig in (1.0, 2.0, 3.0):
        for k in (1, 5, 20, 60):
            a = null_hit_rate(sig, k, margin)
            b = null_hit_rate(sig, k, margin, dual_r=0.5)
            c = null_hit_rate(sig, k, margin, dual_r=0.8)
            print(f"| {sig:.0f} | {k} | {margin:.0%} | {a:.1%} | {b:.1%} | {c:.1%} |")

# ---------------------------------------------------------------------------
# 11. Timeline if a vendor supplies SportyBet prices automatically
# ---------------------------------------------------------------------------
section("11. Timeline with automated SportyBet capture (vendor route)")
print("ASSUMPTION: fixtures/week with SportyBet prices AND an eligible reference\n"
      "in {150, 400, 800}; rule-trigger rate pi per fixture in {2%, 5%}.\n"
      "Paper picks at captured SportyBet prices count toward outcome-based ROI;\n"
      "real placements are needed only for execution realism (slippage, limits).\n")
p185 = 1.03 / 1.85
roi3 = n_for_mean(0.03, 1.85 * math.sqrt(p185 * (1 - p185)))
p185b = 1.05 / 1.85
roi5 = n_for_mean(0.05, 1.85 * math.sqrt(p185b * (1 - p185b)))
print("| fixtures/wk | pi | survey n=300 (wk) | 40 rule picks (wk) | 100 rule picks (wk) | ROI 3% edge (wk) | ROI 5% edge (wk) |")
print("|---|---|---|---|---|---|---|")
for f in (150, 400, 800):
    for pi in (0.02, 0.05):
        picks = f * pi
        print(f"| {f} | {pi:.0%} | {300/f:.1f} | {40/picks:.1f} | {100/picks:.1f} | "
              f"{roi3/picks:,.0f} | {roi5/picks:,.0f} |")
print(f"\n(ROI sample sizes at odds 1.85: 3% edge n = {roi3:,.0f}; 5% edge n = {roi5:,.0f}.)")

# ---------------------------------------------------------------------------
# 12. Confirmation via CLV against a fine-grained sharp close (vendor route)
# ---------------------------------------------------------------------------
section("12. CLV confirmation sample size (only if G0.5 does not kill CLV)")
print("Selection uses the capture-time reference; evaluation uses the close, so\n"
      "selection noise and evaluation noise are separate measurements.\n")
print("| per-pick SD of log-CLV | true mean CLV | picks for 80% power |")
print("|---|---|---|")
for sd in (0.03, 0.05, 0.08):
    for d in (0.01, 0.02):
        print(f"| {sd:.0%} | {d:.0%} | {n_for_mean(d, sd):,.0f} |")

# ---------------------------------------------------------------------------
# 13. v0.4: beating the de-vigged Pinnacle CLOSE on a proper scoring rule
# ---------------------------------------------------------------------------
section("13. v0.4 — encompassing the Pinnacle close (primary metric)")
print("Truth p = c + d, d ~ N(0, tau) in probability, c = de-vigged close.\n"
      "tau is the close's genuine error; a model can only beat the close by\n"
      "capturing part r of var(d) from information the close did not price.\n"
      "Test: y ~ offset(logit c) + b (logit m - logit c); H0 b = 0 (LRT, 1 df).\n"
      "One main line per fixture per market family (no within-fixture pooling).\n")
z_holm = stats.norm.ppf(1 - 0.05 / 6 / 2)
holm_factor = ((z_holm + Z_B) / ZZ) ** 2
print(f"Holm at the cap of 6 rules (strongest rule tested at alpha 0.05/6): sample-size factor {holm_factor:.2f}\n")
print("| tau (pp) | r | n (alpha .05) | n (alpha .05/6) |")
print("|---|---|---|---|")
v04 = {}
for tau in (0.01, 0.02, 0.03):
    for r in (0.25, 0.5, 1.0):
        n = n_for_lrt(r * tau ** 2 / (2 * 0.25))
        v04[(tau, r)] = n
        print(f"| {tau*100:.0f} | {r:.2f} | {n:,.0f} | {n*holm_factor:,.0f} |")

print("\nSmallest detectable effect at a fixed sample (80% power, alpha .05/6):\n"
      "effective RMS mispricing captured = sqrt(r) * tau.\n")
print("| fixtures with usable close | detectable sqrt(r)*tau (pp) |")
print("|---|---|")
for n in (1_700, 2_600, 5_200, 10_400):
    eff = math.sqrt(7.8489 * holm_factor * 0.25 / n)
    print(f"| {n:,} | {eff*100:.2f} |")

print("\nThroughput (ASSUMPTION until Phase 0 measures it): top-5 leagues +\n"
      "Championship + UCL + UEL ~ 75 fixtures/week in season; usable-close share\n"
      "in {0.6, 0.9}; ~38 playing weeks per season.\n")
print("| scenario | usable fixtures/week | n needed | weeks | seasons |")
print("|---|---|---|---|---|")
for (tau, r) in ((0.03, 0.5), (0.02, 0.5), (0.02, 0.25), (0.01, 0.5)):
    n = v04[(tau, r)] * holm_factor
    for share in (0.9, 0.6):
        wk = 75 * share
        print(f"| tau {tau*100:.0f}pp, r {r} | {wk:.0f} | {n:,.0f} | {n/wk:,.0f} | {n/wk/38:.1f} |")


def sim_logloss(n, tau, r, model_noise_pp, reps=300, seed=13):
    """Paired log-loss test (model vs close), one-sided at alpha .05/6,
    plus the encompassing LRT, under the same data."""
    rng = np.random.default_rng(seed)
    z_crit = stats.norm.ppf(1 - 0.05 / 6)
    chi_crit = stats.chi2.ppf(1 - 0.05 / 6, 1)
    win_ll = win_enc = 0
    for _ in range(reps):
        c = rng.uniform(0.35, 0.65, n)
        d1 = rng.normal(0, math.sqrt(r) * tau, n)        # the part of the close's error the model knows
        d2 = rng.normal(0, math.sqrt(1 - r) * tau, n)    # the part it does not
        p = np.clip(c + d1 + d2, 0.02, 0.98)
        y = (rng.uniform(size=n) < p).astype(float)
        m = np.clip(c + d1 + rng.normal(0, model_noise_pp / 100, n), 0.02, 0.98)
        ll = lambda q: -(y * np.log(q) + (1 - y) * np.log(1 - q))  # noqa: E731
        diff = ll(c) - ll(m)
        if diff.mean() / (diff.std(ddof=1) / math.sqrt(n)) > z_crit:
            win_ll += 1
        off = np.log(c / (1 - c))
        zz = np.log(m / (1 - m)) - off
        b = 0.0
        for _ in range(30):
            mu = 1 / (1 + np.exp(-(off + b * zz)))
            g, h = np.sum((y - mu) * zz), np.sum(mu * (1 - mu) * zz * zz)
            b += g / h
            if abs(g / h) < 1e-10:
                break
        lik = lambda bb: float(np.sum(y * (off + bb * zz) - np.log1p(np.exp(off + bb * zz))))  # noqa: E731
        if 2 * (lik(b) - lik(0.0)) > chi_crit and b > 0:
            win_enc += 1
    return win_ll / reps, win_enc / reps


print("\nMonte Carlo, one season (n = 2,600), alpha .05/6, model = close + (known part of the close error) + own noise:\n")
print("| tau (pp) | r | model's own noise (pp) | P(beats close on log loss) | P(encompassing b > 0) |")
print("|---|---|---|---|---|")
for tau, r, mn in ((0.02, 0.5, 0.0), (0.02, 0.5, 1.0), (0.03, 0.5, 1.0), (0.02, 0.25, 1.0), (0.0, 0.0, 1.0)):
    a, b_ = sim_logloss(2600, tau, r, mn, reps=1000 if tau == 0 else 300)
    print(f"| {tau*100:.0f} | {r} | {mn:.0f} | {a:.2f} | {b_:.2f} |")

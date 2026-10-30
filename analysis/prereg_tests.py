"""The two confirmatory tests of DESIGN §1, for any number of outcomes K.

Pre-registered 30 Oct (before any data). Binary families (BTTS, goals totals,
Asian handicap, corner totals and handicap) have K = 2; 1X2 has K = 3 and is scored
on all three outcomes (multinomial), never on one named side.

c: the de-vigged Pinnacle close, shape (n, K), rows summing to 1
m: the forecast, frozen before the close, shape (n, K), rows summing to 1
y: the observed outcome index per fixture, shape (n,)

1. Encompassing. The pooled forecast p_k(b) is proportional to c_k^(1-b) * m_k^b.
   b = 0 is the close alone; b > 0 means m carries information c does not price.
   For K = 2 this is exactly logit p = logit c + b (logit m - logit c), the form in §1.
   Test: likelihood ratio for b = 0 against the MLE, one-sided through the signed
   root z = sign(b_hat) * sqrt(2 (l(b_hat) - l(0))); p = 1 - Phi(z).
2. Scoring rule. d_i = -log m[i, y_i] + log c[i, y_i] (log loss of m minus that of
   c, in nats). Test: one-sided paired t-test of mean(d) < 0.
Both are one-sided at the Holm-adjusted level for m = 6 rules (§9).
"""
from __future__ import annotations

import numpy as np
from scipy import optimize, special, stats

B_BOUNDS = (-5.0, 5.0)


def _check(c: np.ndarray, m: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    c, m, y = np.asarray(c, float), np.asarray(m, float), np.asarray(y, int)
    if c.shape != m.shape or c.ndim != 2 or c.shape[1] < 2 or y.shape != (c.shape[0],):
        raise ValueError("c and m must be (n, K) with K >= 2, and y must be (n,)")
    for name, a in (("c", c), ("m", m)):
        if (a <= 0).any() or not np.allclose(a.sum(axis=1), 1.0, atol=1e-9):
            raise ValueError(f"{name}: every row must be strictly positive and sum to 1")
    if (y < 0).any() or (y >= c.shape[1]).any():
        raise ValueError("y: outcome index out of range")
    return c, m, y


def pooled_loglik(c: np.ndarray, m: np.ndarray, y: np.ndarray, b: float) -> float:
    z = (1 - b) * np.log(c) + b * np.log(m)
    return float(np.sum(z[np.arange(len(y)), y] - special.logsumexp(z, axis=1)))


def encompassing(c, m, y) -> dict:
    c, m, y = _check(c, m, y)
    res = optimize.minimize_scalar(lambda b: -pooled_loglik(c, m, y, b), bounds=B_BOUNDS, method="bounded",
                                   options={"xatol": 1e-8})
    b_hat = float(res.x)
    lr = max(0.0, 2 * (pooled_loglik(c, m, y, b_hat) - pooled_loglik(c, m, y, 0.0)))
    z = float(np.sign(b_hat) * np.sqrt(lr))
    return {"n": len(y), "K": c.shape[1], "b_hat": b_hat, "lr": lr, "z": z, "p_one_sided": float(stats.norm.sf(z))}


def logloss_test(c, m, y) -> dict:
    c, m, y = _check(c, m, y)
    i = np.arange(len(y))
    d = -np.log(m[i, y]) + np.log(c[i, y])
    n = len(d)
    mean, se = float(d.mean()), float(d.std(ddof=1) / np.sqrt(n))
    t = mean / se if se > 0 else float("-inf") if mean < 0 else float("inf")
    return {"n": n, "K": c.shape[1], "mean_diff_nats": mean, "se": se, "t": t,
            "p_one_sided": float(stats.t.cdf(t, n - 1))}

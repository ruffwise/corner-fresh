"""The confirmatory tests: the K = 2 case equals §1's binary form, the tests hold
their size under the null, and they detect a forecast that knows more than the close."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis import prereg_tests as pt  # noqa: E402


def dirichlet_world(n, K, rng, conc=4.0):
    truth = rng.dirichlet([conc] * K, size=n)
    y = np.array([rng.choice(K, p=p) for p in truth])
    return truth, y


def noisy(p, sd, rng):
    z = np.log(p) + rng.normal(0, sd, p.shape)
    z -= z.max(axis=1, keepdims=True)
    q = np.exp(z)
    return q / q.sum(axis=1, keepdims=True)


def test_binary_case_is_the_logit_offset_form_in_section_1():
    rng = np.random.default_rng(1)
    c = rng.uniform(0.1, 0.9, 50)
    m = rng.uniform(0.1, 0.9, 50)
    y = (rng.uniform(size=50) < c).astype(int)
    C, M = np.c_[1 - c, c], np.c_[1 - m, m]
    for b in (-0.7, 0.0, 0.4, 1.3):
        lp = np.log(c / (1 - c)) + b * (np.log(m / (1 - m)) - np.log(c / (1 - c)))
        p = 1 / (1 + np.exp(-lp))
        direct = np.sum(y * np.log(p) + (1 - y) * np.log(1 - p))
        assert pt.pooled_loglik(C, M, y, b) == pytest.approx(direct, abs=1e-9)


@pytest.mark.parametrize("K", [2, 3])
def test_null_size_when_the_forecast_adds_nothing(K):
    rng = np.random.default_rng(20 + K)
    rej_e = rej_l = 0
    reps = 300
    for _ in range(reps):
        truth, y = dirichlet_world(600, K, rng)
        c = truth                                   # the close is the truth
        m = noisy(truth, 0.3, rng)                  # the forecast is truth plus noise: no extra information
        rej_e += pt.encompassing(c, m, y)["p_one_sided"] < 0.05
        rej_l += pt.logloss_test(c, m, y)["p_one_sided"] < 0.05
    assert rej_e / reps < 0.09                      # nominal 5%, allowing Monte Carlo error
    assert rej_l / reps < 0.02                      # a noisier forecast loses on log loss


@pytest.mark.parametrize("K", [2, 3])
def test_detects_a_forecast_that_knows_more_than_the_close(K):
    rng = np.random.default_rng(40 + K)
    truth, y = dirichlet_world(3000, K, rng)
    c = noisy(truth, 0.35, rng)                     # the close is mispriced
    m = truth                                       # the forecast knows the truth
    e, l = pt.encompassing(c, m, y), pt.logloss_test(c, m, y)
    assert e["b_hat"] > 0.5 and e["p_one_sided"] < 1e-6
    assert l["mean_diff_nats"] < 0 and l["p_one_sided"] < 1e-6


def test_inputs_are_validated():
    good = np.array([[0.2, 0.3, 0.5]])
    with pytest.raises(ValueError, match="sum to 1"):
        pt.encompassing(np.array([[0.2, 0.3, 0.4]]), good, np.array([0]))
    with pytest.raises(ValueError, match="out of range"):
        pt.logloss_test(good, good, np.array([3]))

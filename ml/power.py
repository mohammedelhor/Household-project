"""How many samples do we need before a comparison can be trusted?

THE QUESTION
------------
"I want to detect that model B is better than model A by at least δ days of
mean absolute error. How many samples is that?"

THE METHOD (do it by hand if you like)
--------------------------------------
For a PAIRED comparison (both models predict the same samples) look at the
per-sample difference d = |error of A| - |error of B| (positive = B better).
Let σ be the standard deviation of d. To detect a true mean difference of δ
with a two-sided test at significance α = 0.05 and power 80 %:

    n = ((z_{1-α/2} + z_{power}) * σ / δ)^2        z = 1.96 and 0.84, so (2.80)^2 ≈ 7.85

So n = 7.85 * (σ/δ)^2. Worked examples (independent samples):
    σ/δ = 2  ->  32 samples      σ/δ = 3  ->  71      σ/δ = 4  ->  126
A fixed number such as "64 samples" is therefore enough only when σ/δ is about
2.9 or less; with σ/δ = 4, 64 samples give about 52 % power, a coin flip.
No sample size is "enough" in itself: σ/δ must be measured on the data.

SAMPLES ARE NOT INDEPENDENT
---------------------------
Consecutive samples of one (scope, product) share history, and samples of one
household share habits. Counting them as independent makes the test look more
certain than it is. With m samples per cluster (a scope) and an intra-cluster
correlation ρ, the sample size is multiplied by the DESIGN EFFECT

    deff = 1 + (m - 1) * ρ

and the number of independent clusters (scopes) needed is n_total / m. Even a
modest ρ = 0.2 with 30 samples per household multiplies the requirement by
6.8. Hence the protocol compares by scope (cluster bootstrap, ml/compare.py).

ρ and σ are properties of the data. Until real data exists they are unknown:
use the function with a RANGE of values and quote the range.
"""

from __future__ import annotations

import math
from statistics import NormalDist

ALPHA = 0.05
POWER = 0.80


def z(p: float) -> float:
    return NormalDist().inv_cdf(p)


def design_effect(cluster_size: float, icc: float) -> float:
    """deff = 1 + (m - 1) * rho. m = samples per cluster, rho in [0, 1]."""
    if cluster_size < 1:
        raise ValueError("cluster_size must be at least 1")
    if not 0 <= icc <= 1:
        raise ValueError("icc must be between 0 and 1")
    return 1 + (cluster_size - 1) * icc


def required_samples(sigma_d: float, delta: float, *, alpha: float = ALPHA,
                     power: float = POWER, cluster_size: float = 1, icc: float = 0.0) -> int:
    """Samples needed to detect a mean paired difference `delta` (days).

    sigma_d: standard deviation of the per-sample paired difference
    delta:   the smallest improvement worth detecting
    """
    if sigma_d <= 0 or delta <= 0:
        raise ValueError("sigma_d and delta must be positive")
    base = ((z(1 - alpha / 2) + z(power)) * sigma_d / delta) ** 2
    return math.ceil(base * design_effect(cluster_size, icc))


def required_clusters(sigma_d: float, delta: float, *, cluster_size: float, icc: float,
                      alpha: float = ALPHA, power: float = POWER) -> int:
    """Independent clusters (scopes) needed for the same detection."""
    samples = required_samples(sigma_d, delta, alpha=alpha, power=power,
                               cluster_size=cluster_size, icc=icc)
    return math.ceil(samples / cluster_size)


def power_for(n: int, sigma_d: float, delta: float, *, alpha: float = ALPHA,
              cluster_size: float = 1, icc: float = 0.0) -> float:
    """Probability of detecting `delta` with n samples (inverse of the above)."""
    effective = n / design_effect(cluster_size, icc)
    shift = delta * math.sqrt(effective) / sigma_d
    return NormalDist().cdf(shift - z(1 - alpha / 2))

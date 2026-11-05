"""Comparing two predictors honestly: paired differences, bootstrap by scope.

WHY NOT JUST COMPARE TWO MAE NUMBERS
------------------------------------
"Model B has MAE 1.8, A has 2.1" says nothing about whether the gap is real:
with few samples it can be luck, and the samples are not independent
(consecutive samples of one series share history; samples of one household
share habits). So:

1. PAIRED: both predictors answer the same samples, and we look at
   d = |error A| - |error B| per sample (positive = B better).
2. CLUSTER BOOTSTRAP BY SCOPE: we resample whole scopes (households or
   users) with replacement, not single rows. A scope is the unit that is
   independent of the others, so the confidence interval reflects how much
   the result changes from one household to another.
3. PRE-SPECIFIED δ: before looking at results, decide the smallest
   improvement worth having (`delta`). The decision rule is fixed in advance:
       adopt B      lower end of the 95 % interval > delta
       keep A       upper end of the 95 % interval < delta  (B not worth it)
       inconclusive otherwise  (more data needed: see ml/power.py)
   Never move δ after seeing the numbers.
4. UNTOUCHED FINAL PERIOD: the last stretch of time is never used to choose
   or tune anything; it is looked at once, at the end (ml/PROTOCOL.md).

With a single scope there is nothing to resample: the result is reported as
inconclusive on purpose, whatever the numbers say.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass

from ml.evaluate import _round_day
from ml.target import Sample

DEFAULT_RESAMPLES = 2000


@dataclass(frozen=True)
class Comparison:
    n_samples: int
    n_scopes: int
    mean_difference: float          # mean of |err A| - |err B|; positive = B better
    ci_low: float
    ci_high: float
    delta: float
    decision: str                   # adopt_b | keep_a | inconclusive
    reason: str


def paired_differences(samples: list[Sample], a, b) -> list[tuple[str, float]]:
    """[(scope key, |error A| - |error B|)] for samples both predictors answer."""
    rows = []
    for s in samples:
        pa, pb = a.predict(s.history_days), b.predict(s.history_days)
        if pa is None or pb is None:
            continue
        rows.append((s.scope.key,
                     abs(_round_day(pa) - s.target_days) - abs(_round_day(pb) - s.target_days)))
    return rows


def cluster_bootstrap_ci(rows: list[tuple[str, float]], *, resamples: int = DEFAULT_RESAMPLES,
                         seed: int = 0, level: float = 0.95) -> tuple[float, float]:
    """Percentile interval of the mean difference, resampling whole scopes."""
    by_scope: dict[str, list[float]] = defaultdict(list)
    for key, d in rows:
        by_scope[key].append(d)
    clusters = list(by_scope.values())
    if not clusters:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        picked = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        values = [d for cluster in picked for d in cluster]
        means.append(statistics.fmean(values))
    means.sort()
    low = means[int((1 - level) / 2 * resamples)]
    high = means[min(resamples - 1, int((1 + level) / 2 * resamples))]
    return low, high


def compare(samples: list[Sample], a, b, *, delta: float,
            resamples: int = DEFAULT_RESAMPLES, seed: int = 0) -> Comparison:
    """B against A with the pre-specified decision rule."""
    if delta <= 0:
        raise ValueError("delta must be positive and fixed BEFORE looking at results")
    rows = paired_differences(samples, a, b)
    scopes = {k for k, _ in rows}
    if not rows:
        return Comparison(0, 0, math.nan, math.nan, math.nan, delta, "inconclusive",
                          "no sample was answered by both predictors")
    mean = statistics.fmean(d for _, d in rows)
    if len(scopes) < 2:
        return Comparison(len(rows), len(scopes), mean, math.nan, math.nan, delta,
                          "inconclusive",
                          "a single scope: nothing to resample, the result cannot generalise")
    low, high = cluster_bootstrap_ci(rows, resamples=resamples, seed=seed)
    if low > delta:
        decision, reason = "adopt_b", "the whole interval is above delta"
    elif high < delta:
        decision, reason = "keep_a", "the whole interval is below delta"
    else:
        decision, reason = "inconclusive", "the interval contains delta: more data needed"
    return Comparison(len(rows), len(scopes), mean, low, high, delta, decision, reason)

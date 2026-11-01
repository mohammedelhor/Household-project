"""Measuring a predictor: how many days off is it, and in which direction?

METRICS (all in days, so a person can judge them)
-------------------------------------------------
  MAE        mean absolute error: "on average I am this many days off"
  median AE  the same with the median: not distorted by one huge miss
  bias       mean of (prediction - truth): positive = predicts too late
  within 1d  share of predictions at most one day off

Predictions are rounded to whole days first, because purchases are dated by
day.

COLD START
----------
A predictor needs some history. Samples with fewer than `min_intervals` known
gaps are left out of EVERY predictor's score (same samples for everyone, so
the comparison is fair) and are counted as "skipped".

THE REFERENCE LINE ("best constant")
------------------------------------
`best_constant_reference` predicts, for each product, the median of ALL its
real gaps. It uses the future, so no real system can do this; it is the best
any rule of the form "one fixed number per product" could ever score. A real
model is worth building only if it can get below this line.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from ml.target import Sample

DEFAULT_MIN_INTERVALS = 2


@dataclass(frozen=True)
class Metrics:
    n: int
    mae: float
    median_ae: float
    bias: float
    within_1_day: float


def _round_day(value: float) -> int:
    return math.floor(value + 0.5)        # .5 rounds up, not to the nearest even number


def _metrics(errors: list[int]) -> Metrics:
    if not errors:
        return Metrics(0, math.nan, math.nan, math.nan, math.nan)
    absolute = [abs(e) for e in errors]
    return Metrics(
        n=len(errors),
        mae=statistics.fmean(absolute),
        median_ae=float(statistics.median(absolute)),
        bias=statistics.fmean(errors),
        within_1_day=sum(a <= 1 for a in absolute) / len(absolute),
    )


def usable(samples: list[Sample], min_intervals: int) -> list[Sample]:
    return [s for s in samples if len(s.history_days) >= min_intervals]


def evaluate(
    samples: list[Sample],
    predictor,
    *,
    min_intervals: int = DEFAULT_MIN_INTERVALS,
) -> tuple[Metrics, dict[int, Metrics]]:
    """Overall metrics and the same per product id."""
    errors: list[int] = []
    per_product: dict[int, list[int]] = defaultdict(list)
    for sample in usable(samples, min_intervals):
        prediction = predictor.predict(sample.history_days)
        if prediction is None:
            continue
        error = _round_day(prediction) - sample.target_days
        errors.append(error)
        per_product[sample.product_id].append(error)
    return _metrics(errors), {p: _metrics(e) for p, e in per_product.items()}


def best_constant_reference(
    samples: list[Sample],
    *,
    min_intervals: int = DEFAULT_MIN_INTERVALS,
) -> tuple[Metrics, dict[int, Metrics]]:
    """The floor for 'one fixed number per product' rules (uses the future)."""
    chosen = usable(samples, min_intervals)
    gaps: dict[int, list[int]] = defaultdict(list)
    for sample in chosen:
        gaps[sample.product_id].append(sample.target_days)
    constant = {p: _round_day(statistics.median(g)) for p, g in gaps.items()}

    errors = [constant[s.product_id] - s.target_days for s in chosen]
    per_product: dict[int, list[int]] = defaultdict(list)
    for sample, error in zip(chosen, errors):
        per_product[sample.product_id].append(error)
    return _metrics(errors), {p: _metrics(e) for p, e in per_product.items()}
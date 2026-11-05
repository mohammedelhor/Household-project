"""Measuring a predictor: how many days off is it, and in which direction?

METRICS (all in days, so a person can judge them)
-------------------------------------------------
  MAE        mean absolute error: "on average I am this many days off"
  median AE  the same with the median: not distorted by one huge miss
  bias       mean of (prediction - truth): positive = predicts too late
  within 1d  share of predictions at most one day off

Predictions are rounded to whole days first, because purchases are dated by
day.

UNITS
-----
Everything is grouped by the series (scope, product): the same product in two
households is two units (see ml/target.py).

COLD START AND EVIDENCE
-----------------------
The HEADLINE score uses samples with at least `min_intervals` known gaps
(default ml.evidence.MIN_GAPS_FOR_HEADLINE = 2), the same samples for every
predictor so the comparison is fair; the rest are counted as "skipped".

But the suggestions are shown from ONE past gap (ml.evidence.MIN_GAPS_TO_SUGGEST),
so the error of those must be measured too, or users would see numbers whose
accuracy nobody checked. `evaluate_by_tier` therefore scores samples with
1, 2, 3-6 and 7+ known gaps separately, from the same thresholds the
suggestions use (ml/evidence.py), and marks a tier "unstable" when it has
fewer than ml.evidence.UNSTABLE_BELOW samples.

THE RETROSPECTIVE ORACLE REFERENCE
----------------------------------
`retrospective_oracle_reference` predicts, for each (scope, product), the
median of ALL its real gaps, including gaps that happen AFTER the moment being
predicted. It is computed in hindsight and uses the future, so no real system
can do this. It is a reference line for "one fixed number per series", not a
floor: a real predictor can beat it, because it may react to recent changes
that a single constant cannot. The line shows how much of the error comes from
the product's own irregularity; a model close to it has captured the
product's typical rhythm, and a model far above it has not.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from ml.evidence import MIN_GAPS_FOR_HEADLINE, TIERS, UNSTABLE_BELOW, tier_for
from ml.target import Sample

DEFAULT_MIN_INTERVALS = MIN_GAPS_FOR_HEADLINE


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
) -> tuple[Metrics, dict]:
    """Overall metrics and the same per unit (scope, product)."""
    errors: list[int] = []
    per_unit: dict = defaultdict(list)
    for sample in usable(samples, min_intervals):
        prediction = predictor.predict(sample.history_days)
        if prediction is None:
            continue
        error = _round_day(prediction) - sample.target_days
        errors.append(error)
        per_unit[sample.unit].append(error)
    return _metrics(errors), {u: _metrics(e) for u, e in per_unit.items()}


def _oracle_constants(chosen: list[Sample]) -> dict:
    gaps: dict = defaultdict(list)
    for sample in chosen:
        gaps[sample.unit].append(sample.target_days)
    return {u: _round_day(statistics.median(g)) for u, g in gaps.items()}


def retrospective_oracle_reference(
    samples: list[Sample],
    *,
    min_intervals: int = DEFAULT_MIN_INTERVALS,
) -> tuple[Metrics, dict]:
    """The best single number per (scope, product), chosen in hindsight."""
    chosen = usable(samples, min_intervals)
    constant = _oracle_constants(chosen)

    errors = [constant[s.unit] - s.target_days for s in chosen]
    per_unit: dict = defaultdict(list)
    for sample, error in zip(chosen, errors):
        per_unit[sample.unit].append(error)
    return _metrics(errors), {u: _metrics(e) for u, e in per_unit.items()}


def tier_labels() -> list[str]:
    return [label for label, _, _ in TIERS]


def evaluate_by_tier(samples: list[Sample], predictor) -> dict[str, dict]:
    """Score by how much history the sample had: {tier: {"metrics", "unstable"}}.

    Includes samples with a single known gap (the least the suggestions need).
    """
    errors: dict[str, list[int]] = {label: [] for label in tier_labels()}
    for sample in samples:
        tier = tier_for(len(sample.history_days))
        prediction = predictor.predict(sample.history_days) if tier else None
        if prediction is None:
            continue
        errors[tier].append(_round_day(prediction) - sample.target_days)
    return {label: _tier_entry(e) for label, e in errors.items()}


def oracle_by_tier(samples: list[Sample]) -> dict[str, dict]:
    """The oracle reference per tier (constants from all samples with >= 1 gap)."""
    chosen = usable(samples, 1)
    constant = _oracle_constants(chosen)
    errors: dict[str, list[int]] = {label: [] for label in tier_labels()}
    for sample in chosen:
        errors[tier_for(len(sample.history_days))].append(constant[sample.unit] - sample.target_days)
    return {label: _tier_entry(e) for label, e in errors.items()}


def _tier_entry(errors: list[int]) -> dict:
    metrics = _metrics(errors)
    return {"metrics": metrics, "unstable": metrics.n < UNSTABLE_BELOW}

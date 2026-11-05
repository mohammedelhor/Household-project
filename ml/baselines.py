"""Baselines: the simple rules any real model has to beat.

A model that cannot beat these is not worth its complexity. They are also a
check on the whole setup: if a trivial rule scores badly, the problem is in
the data or the target, not in the algorithm.

Each predictor answers one question: given the days between this product's
previous purchases (oldest first), in how many days will it be bought again?
They return None when they have nothing to go on.

Nothing here is trained: every prediction uses only the history passed in,
which is already limited to what was known at the cutoff. So these baselines
cannot leak the future by construction. (A trained model will need a
chronological train/test split; that comes with the first such model.)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class LastInterval:
    """Next gap = the last gap. Reacts fast, but copies every bit of noise."""
    name: str = "last interval"

    def predict(self, history: tuple[int, ...]) -> float | None:
        return float(history[-1]) if history else None


@dataclass(frozen=True)
class MeanInterval:
    """Next gap = average of all past gaps. Smooth, but pulled by outliers."""
    name: str = "mean of intervals"

    def predict(self, history: tuple[int, ...]) -> float | None:
        return statistics.fmean(history) if history else None


@dataclass(frozen=True)
class MedianInterval:
    """Next gap = median of all past gaps. Ignores outliers.

    This is the "Historical Median Baseline": the simple rule every later model
    has to beat. (The label keeps the old name so records stay comparable.)
    """
    name: str = "median of intervals"

    def predict(self, history: tuple[int, ...]) -> float | None:
        return float(statistics.median(history)) if history else None


@dataclass(frozen=True)
class FixedDays:
    """The same answer for every product (for example 14 days). It only exists
    to show how much the per-product rules above are actually worth."""
    days: int = 14

    @property
    def name(self) -> str:
        return f"always {self.days} days"

    def predict(self, history: tuple[int, ...]) -> float | None:
        return float(self.days)


ALL_BASELINES = (LastInterval(), MeanInterval(), MedianInterval(), FixedDays(14))

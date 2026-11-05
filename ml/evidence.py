"""How much evidence stands behind a number? ONE place defines the thresholds.

The evaluation (ml/evaluate.py, ml/report.py) and the suggestions
(ml/suggest.py) must agree on what counts as "enough history". When they
disagreed (the suggestions worked from one past interval, the headline score
only counted samples with two), the system could show a user something whose
accuracy had never been measured. Now both read these constants, and a test
checks that every level the suggestions can emit has a row in the report.

WORDS
-----
history_strength says how much history was available. It is NOT a probability
that the prediction is right, and the interface must not present it as one:
two products can both have a long history while one is bought like clockwork
and the other at random. (A separate `prediction_uncertainty`, based on how
spread out the past intervals are, is planned but must first be shown to
predict the real error.)

    none      no past interval: no prediction is made
    short     1-2 past intervals
    moderate  3-6
    long      7 or more

TIERS (finer than the levels, for the report)
---------------------------------------------
The report measures the error separately for 1, 2, 3-6 and 7+ past intervals,
so each level is backed by a measured number, not a guess. The level
boundaries below are tier boundaries, so a level is always a union of tiers.
"""

from __future__ import annotations

MIN_GAPS_TO_SUGGEST = 1        # the suggestions work from one past interval ...
MIN_GAPS_FOR_HEADLINE = 2      # ... but the headline score only counts samples with two
UNSTABLE_BELOW = 30            # fewer samples than this in a tier: the number is noise

# (label, first gap count, last gap count or None = no upper limit)
TIERS: tuple[tuple[str, int, int | None], ...] = (
    ("1 interval", 1, 1),
    ("2 intervals", 2, 2),
    ("3-6 intervals", 3, 6),
    ("7+ intervals", 7, None),
)

# (level, first gap count, last gap count or None)
STRENGTH_LEVELS: tuple[tuple[str, int, int | None], ...] = (
    ("short", 1, 2),
    ("moderate", 3, 6),
    ("long", 7, None),
)
STRENGTHS = ("none",) + tuple(level for level, _, _ in STRENGTH_LEVELS)


def history_strength_for(gaps: int) -> str:
    if gaps < MIN_GAPS_TO_SUGGEST:
        return "none"
    for level, low, high in STRENGTH_LEVELS:
        if gaps >= low and (high is None or gaps <= high):
            return level
    raise AssertionError("unreachable: the levels cover every positive count")


def tier_for(gaps: int) -> str | None:
    """The report row a sample with `gaps` past intervals belongs to."""
    for label, low, high in TIERS:
        if gaps >= low and (high is None or gaps <= high):
            return label
    return None

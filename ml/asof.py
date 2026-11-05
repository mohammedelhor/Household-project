"""As-of reconstruction: what did we know at a given moment?

THE PROBLEM THIS SOLVES (time leakage)
--------------------------------------
purchases.csv shows the FINAL state of every item. Take this history:

    Oct 1   added
    Oct 10  purchased
    Oct 12  reverted      (a mistaken tap, undone)
    Oct 15  purchased

The final table contains ONE purchase, on Oct 15. Now imagine we train a
model to predict, on Oct 11, "when will this product be bought next?". On Oct 11
the truth was different from the final table: there WAS a purchase on Oct 10
(not yet reverted), and the Oct 15 purchase had not happened. A feature
computed from the final table would use the future (Oct 15) and would miss
what was actually known (Oct 10). The model would look better in testing than
it can ever be in real life, and we would not notice.

THE RULE
--------
A prediction made at time T may use only events with occurred_at <= T.
(Events exactly at T count as known; a prediction is made after the instant.)
Everything is rebuilt from the EVENTS with the same cleaning rules as the
pipeline, so the state machine (purchase / revert / archive) is not
reimplemented here.

WHAT THIS DOES NOT SOLVE (yet)
------------------------------
occurred_at is when the person TAPPED, not necessarily when the purchase
really happened (someone may tap on Sunday for a Friday purchase), and the
database has no separate "recorded at" time. Our synthetic data has no such
gap. Real data may; that would need a backend change (an optional purchase
date) and is deliberately postponed.

Also, an item whose history looks inconsistent is rejected using ONLY the
events known at T, never later ones (using a later event to reject an earlier
state would be leakage in the other direction).
"""

from __future__ import annotations

from datetime import datetime

from data_engineering.scopes import Scope
from data_engineering.transform import (
    Event,
    Purchase,
    Quarantined,
    build_purchases,
    compute_intervals,
)


def events_as_of(events: list[Event], cutoff: datetime) -> list[Event]:
    """The events a predictor standing at `cutoff` could have seen."""
    if cutoff.tzinfo is None:
        # A naive datetime has no zone, so "before or after" is ambiguous.
        raise ValueError("cutoff must be timezone-aware")
    return [event for event in events if event.occurred_at <= cutoff]


def purchases_as_of(
    events: list[Event],
    cutoff: datetime,
    list_scopes: dict[int, Scope],
) -> tuple[list[Purchase], list[Quarantined]]:
    """The purchases that existed at `cutoff`, using the pipeline's own rules.

    A purchase that was reverted AFTER `cutoff` is still a purchase here: at
    that moment nobody knew it would be reverted.

    `list_scopes` (list id -> scope) gives every purchase its owner. The
    mapping comes from the lists snapshot: a list's owner never changes (the
    quality gate fails if it does), so the newest snapshot is valid for every
    cutoff, and a list created after the cutoff has no events before it.
    """
    return build_purchases(events_as_of(events, cutoff), None, list_scopes)


def intervals_as_of(
    events: list[Event],
    cutoff: datetime,
    names: dict[int, tuple[str, str]],
    list_scopes: dict[int, Scope],
) -> list[dict]:
    """Days between consecutive purchases (per scope and product), from the
    purchases known at `cutoff`."""
    purchases, _ = purchases_as_of(events, cutoff, list_scopes)
    return compute_intervals(purchases, names)

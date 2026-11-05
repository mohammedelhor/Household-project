"""The prediction task and how samples are built from it.

THE QUESTION
------------
"This household (or user) just bought product P. When will THEY buy it again?"

The unit of prediction is (scope, product): a scope is the owner of the
shopping list (a household or a single user, see data_engineering/scopes.py).
Two households that both buy milk are two separate series; their gaps are
never mixed, because a family of five and a single person do not shop alike.
The scope id is used ONLY to keep the series apart. It is never a feature
(a model must not learn "household 3 is slow"): `Sample` has no field a model
could take it from by accident, and a test guards that.

One SAMPLE = one moment right after a purchase of one product in one scope:
    scope             whose series this is
    cutoff            the moment of that purchase (the prediction is made here)
    history_days      the days between consecutive purchases of P in that scope
                      that were KNOWN at the cutoff (all a predictor may see)
    last_purchase_date  the last purchase of P known at the cutoff
    target_days       what really happened: days from that purchase to the
                      NEXT purchase of P in the same scope

SAMPLE DEFINITION: "settled_purchases_v1"
-----------------------------------------
A sample starts at every purchase that STANDS in the final, settled state
(a purchase that was reverted and never re-bought is not a sample start).
Why settled and not every tap: at serving time the user sees the state as it
is NOW, and a mistaken tap that was undone a minute later is not a moment the
system would have been asked about. The history, on the other hand, is rebuilt
as it was known at the cutoff (ml/asof.py), so it still includes taps that
were reverted LATER: at that moment nobody knew. The two choices differ on
purpose and the difference is recorded in every experiment as
`sample_definition`, so numbers from different definitions are never
compared by accident. (Alternative "every_purchase_event_v1", starting a
sample at every tap, is not implemented; it would also measure how the system
copes with taps that are later undone.)

WHY THE TARGET IS "THE NEXT PURCHASE MARK"
------------------------------------------
Our data says when an item was MARKED as purchased, not when it was used up
(see DATA_DICTIONARY.md). So the target is "days until the next time I mark
this product as purchased". It is a purchase rhythm, not a consumption
measurement, and any output built on it is a suggestion to add the product to
the list, never a claim that it ran out.

TIME RULES (no leakage)
-----------------------
* The history is rebuilt AS OF the cutoff (ml/asof.py).
* The target looks at the FINAL, settled state: the next purchase that really
  stood. The future is allowed to define the answer, never the question.
* The last purchase of a product in a scope has no "next" and produces no
  sample.
* The statistical unit is the INTERVAL, not the row: consecutive samples of
  one (scope, product) share history and are not independent (see
  ml/PROTOCOL.md).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from data_engineering.scopes import Scope
from data_engineering.transform import Event, build_purchases
from ml.asof import purchases_as_of

SAMPLE_DEFINITION = "settled_purchases_v1"


@dataclass(frozen=True)
class Sample:
    scope: Scope
    product_id: int
    cutoff: datetime
    last_purchase_date: date
    history_days: tuple[int, ...]    # oldest first; known at the cutoff
    target_days: int                 # the answer: days until the next purchase

    @property
    def unit(self) -> tuple[Scope, int]:
        """The series this sample belongs to: (scope, product)."""
        return (self.scope, self.product_id)


def build_samples(events: list[Event], list_scopes: dict[int, Scope]) -> list[Sample]:
    """All samples, oldest cutoff first."""
    # The final state: used for the answers and for choosing the sample starts.
    settled, _ = build_purchases(events, None, list_scopes)
    by_unit: dict[tuple[Scope, int], list] = defaultdict(list)
    for purchase in settled:
        if purchase.product_id is not None and purchase.scope is not None:
            by_unit[(purchase.scope, purchase.product_id)].append(purchase)

    samples: list[Sample] = []
    for (scope, product_id), purchases in by_unit.items():
        purchases.sort(key=lambda p: (p.purchased_at, p.item_id))
        for current, following in zip(purchases, purchases[1:]):
            cutoff = current.purchased_at

            # What a predictor standing at the cutoff could have known.
            known = [
                p for p in purchases_as_of(events, cutoff, list_scopes)[0]
                if p.product_id == product_id and p.scope == scope
            ]
            known.sort(key=lambda p: (p.purchased_at, p.item_id))
            if not known:
                continue
            dates = [p.purchase_date for p in known]
            history = tuple((b - a).days for a, b in zip(dates, dates[1:]))

            samples.append(Sample(
                scope=scope,
                product_id=product_id,
                cutoff=cutoff,
                last_purchase_date=dates[-1],
                history_days=history,
                target_days=(following.purchase_date - dates[-1]).days,
            ))

    samples.sort(key=lambda s: (s.cutoff, s.scope, s.product_id))
    return samples

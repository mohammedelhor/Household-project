"""The prediction task and how samples are built from it.

THE QUESTION
------------
"I just bought product P. When will I buy it again?"

One SAMPLE = one moment right after a purchase of one product:
    cutoff            the moment of that purchase (the prediction is made here)
    history_days      the days between consecutive purchases of P that were
                      KNOWN at the cutoff (this is all a predictor may look at)
    last_purchase_date  the last purchase of P known at the cutoff
    target_days       what really happened: days from that purchase to the
                      NEXT purchase of P

WHY THE TARGET IS "THE NEXT PURCHASE MARK"
------------------------------------------
Our data says when an item was MARKED as purchased, not when it was used up
(see DATA_DICTIONARY.md). So the target is "days until the next time I mark
this product as purchased". It is a purchase rhythm, not a consumption
measurement, and any output built on it is a suggestion to add the product to
the list, never a claim that it ran out.

TIME RULES (no leakage)
-----------------------
* The history is rebuilt AS OF the cutoff (ml/asof.py): a purchase that was
  reverted later is still a purchase in the history, exactly as it was known.
* The target looks at the FINAL, settled state: the next purchase that really
  stood. The future is allowed to define the answer, never the question.
* The last purchase of the product has no "next" and produces no sample.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from data_engineering.transform import Event, build_purchases
from ml.asof import purchases_as_of


@dataclass(frozen=True)
class Sample:
    product_id: int
    cutoff: datetime
    last_purchase_date: date
    history_days: tuple[int, ...]    # oldest first; known at the cutoff
    target_days: int                 # the answer: days until the next purchase


def build_samples(events: list[Event]) -> list[Sample]:
    """All samples, oldest cutoff first."""
    settled, _ = build_purchases(events)          # the final state: used for the answers only
    by_product: dict[int, list] = defaultdict(list)
    for purchase in settled:
        if purchase.product_id is not None:       # free-text items have no product to predict
            by_product[purchase.product_id].append(purchase)

    samples: list[Sample] = []
    for product_id, purchases in by_product.items():
        purchases.sort(key=lambda p: (p.purchased_at, p.item_id))
        for current, following in zip(purchases, purchases[1:]):
            cutoff = current.purchased_at

            # What a predictor standing at the cutoff could have known.
            known = [p for p in purchases_as_of(events, cutoff)[0] if p.product_id == product_id]
            known.sort(key=lambda p: (p.purchased_at, p.item_id))
            if not known:
                continue
            dates = [p.purchase_date for p in known]
            history = tuple((b - a).days for a, b in zip(dates, dates[1:]))

            samples.append(Sample(
                product_id=product_id,
                cutoff=cutoff,
                last_purchase_date=dates[-1],
                history_days=history,
                target_days=(following.purchase_date - dates[-1]).days,
            ))

    samples.sort(key=lambda s: (s.cutoff, s.product_id))
    return samples
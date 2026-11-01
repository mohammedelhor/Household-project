"""Suggestions: "which products will I probably need soon?"

    suggest(events, names, as_of=now)  ->  one Suggestion per product

A PURE function: no database, no files, no clock (the moment is a parameter).
That is what makes it testable with exact numbers, and it means the API layer
later is only a thin wrapper that loads events and calls this.

THE RULE (the baseline that won in ml/report.py)
------------------------------------------------
    expected date = last purchase date + median of the past gaps

Everything is computed AS OF `as_of` (ml/asof.py): only what was known then.
Pass events already cleaned of rejected items (ml.report.drop_rejected_items),
as the cleaning step does.

STATUS (relative to today = the UTC date of `as_of`)
----------------------------------------------------
  overdue   the expected date has passed                    (days_until < 0)
  due_soon  expected within SOON_DAYS days, or today          (0 <= days_until <= SOON_DAYS)
  not_yet   expected later
  unknown   fewer than one past gap: no rhythm to go on

  already_on_list = True when an item of this product is currently on a list
  waiting to be bought. The caller should not suggest what is already there.

CONFIDENCE (provisional; from the number of past gaps the rule used)
-------------------------------------------------------------------
  none: 0 gaps   low: 1-2   medium: 3-6   high: 7 or more
These cut-offs are a first guess. ml/report.py shows how stable the error is
per product (Olive oil with 3 samples scored 2.0 days against 1.0 for the
reference): revisit them when real data is long enough (see the report notes).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from data_engineering.transform import Event
from ml.asof import events_as_of, purchases_as_of

SOON_DAYS = 2


@dataclass(frozen=True)
class Suggestion:
    product_id: int
    product_name: str
    last_purchase_date: date | None
    typical_gap_days: int | None        # the median gap, in whole days
    expected_date: date | None
    days_until: int | None              # negative = overdue by that many days
    status: str                         # overdue | due_soon | not_yet | unknown
    confidence: str                     # none | low | medium | high
    gaps_used: int
    already_on_list: bool


def confidence_for(gaps: int) -> str:
    if gaps == 0:
        return "none"
    if gaps <= 2:
        return "low"
    if gaps <= 6:
        return "medium"
    return "high"


def products_waiting_on_a_list(events: list[Event]) -> set[int]:
    """Products with at least one item that is neither bought nor archived.

    Replays each item the same way the cleaning step does: a purchase puts the
    item in the 'bought' state, a revert takes it back, archiving ends it.
    """
    by_item: dict[int, list[Event]] = defaultdict(list)
    for event in events:
        by_item[event.item_id].append(event)

    waiting: set[int] = set()
    for item_events in by_item.values():
        item_events.sort(key=lambda e: (e.occurred_at, e.id))
        bought = archived = False
        for event in item_events:
            if event.event_type == "purchased":
                bought = True
            elif event.event_type == "reverted":
                bought = False
            elif event.event_type == "archived":
                archived = True
        product = item_events[0].product_id
        if product is not None and not bought and not archived:
            waiting.add(product)
    return waiting


def status_for(days_until: int) -> str:
    if days_until < 0:
        return "overdue"
    return "due_soon" if days_until <= SOON_DAYS else "not_yet"


def suggest(events: list[Event], names: dict[int, str], *, as_of: datetime) -> list[Suggestion]:
    """One suggestion per known product, most urgent first."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    today = as_of.astimezone(timezone.utc).date()

    known = events_as_of(events, as_of)                   # what was known, nothing else
    purchases, _ = purchases_as_of(events, as_of)
    waiting = products_waiting_on_a_list(known)

    dates_by_product: dict[int, list[date]] = defaultdict(list)
    for purchase in purchases:                            # already in time order
        if purchase.product_id is not None:
            dates_by_product[purchase.product_id].append(purchase.purchase_date)

    suggestions: list[Suggestion] = []
    for product_id, name in names.items():
        dates = dates_by_product.get(product_id, [])
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]

        if not gaps:
            suggestions.append(Suggestion(
                product_id, name, dates[-1] if dates else None, None, None, None,
                "unknown", "none", 0, product_id in waiting))
            continue

        gap = int(statistics.median(gaps) + 0.5)          # .5 rounds up, like the evaluation
        expected = dates[-1] + timedelta(days=gap)
        days_until = (expected - today).days
        suggestions.append(Suggestion(
            product_id, name, dates[-1], gap, expected, days_until,
            status_for(days_until), confidence_for(len(gaps)), len(gaps),
            product_id in waiting))

    # Most urgent first (most overdue = smallest days_until); unknown last.
    suggestions.sort(key=lambda s: (s.days_until is None, s.days_until or 0, s.product_name))
    return suggestions


def main() -> int:
    """Print today's suggestions from the latest published data.

        uv run python -m ml.suggest
        uv run python -m ml.suggest --as-of 2026-10-20     # what it would have said then
    """
    import argparse
    from pathlib import Path

    from data_engineering.extract import DEFAULT_OUT_DIR
    from data_engineering.transform import DEFAULT_CLEAN_DIR
    from ml.report import StaleInputError, load_published_dataset

    parser = argparse.ArgumentParser(description="Which products will be needed soon?")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--clean-dir", type=Path, default=DEFAULT_CLEAN_DIR)
    parser.add_argument("--as-of", type=date.fromisoformat, help="UTC date; default: now")
    args = parser.parse_args()

    try:
        events, manifest, names = load_published_dataset(args.raw_dir, args.clean_dir)
    except (FileNotFoundError, StaleInputError) as exc:
        print(f"CANNOT SUGGEST: {exc}")
        return 1

    moment = (datetime.combine(args.as_of, datetime.max.time(), tzinfo=timezone.utc)
              if args.as_of else datetime.now(timezone.utc))
    print(f"dataset run: {manifest['run_id']}   as of: {moment.date()}\n")
    print(f"{'product':12} {'status':9} {'days':>5} {'expected':11} {'gap':>4} {'conf':7} note")
    for s in suggest(events, names, as_of=moment):
        note = "already on a list" if s.already_on_list else ""
        print(f"{s.product_name:12} {s.status:9} "
              f"{'-' if s.days_until is None else format(s.days_until, '+d'):>5} "
              f"{str(s.expected_date or '-'):11} {s.typical_gap_days or '-':>4} "
              f"{s.confidence:7} {note}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
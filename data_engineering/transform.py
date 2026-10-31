"""Cleaning step: raw event CSVs -> trustworthy purchases and intervals.

This is the "T" of ETL. It reads the files written by extract.py and answers
the real question: WHEN was each product actually bought, and how many days
passed between purchases?

THE HARD PART: events are not purchases
---------------------------------------
The raw table holds events: added, purchased, reverted, archived. A person
can tap "purchased" by mistake, tap "reverted", then "purchased" again. If we
counted every 'purchased' event we would invent purchases that never happened.
So each item is replayed as a small state machine (see `evaluate_item`), and
only an item that is STILL bought at the end of its history is a purchase.

WE DO NOT TRUST THE SOURCE
--------------------------
The database already has constraints, but this pipeline will run for years
and the source may change. So every rule is checked again here. Bad data is
never silently dropped and never silently used: it goes to a QUARANTINE file
with the reason, so a person can look at it.

An item with an inconsistent history is quarantined AS A WHOLE. If one of its
events is wrong we cannot know which purchase time to believe.

KNOWN SIMPLIFICATION
--------------------
"Day" means the UTC date. The household lives in another time zone, so a
purchase at 23:30 local time could land on the next UTC day. Our synthetic
data avoids that boundary; real data may not. Decision postponed on purpose.

USAGE (from the project root)
-----------------------------
    uv run python -m data_engineering.transform
    uv run python -m data_engineering.transform --truth data_engineering/synthetic_ground_truth.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backend.app.core.units import SUPPORTED_UNITS
from data_engineering.extract import DEFAULT_OUT_DIR as DEFAULT_RAW_DIR
from data_engineering.extract import write_csv_atomic

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLEAN_DIR = PROJECT_ROOT / "data" / "clean"

EVENT_TYPES = frozenset({"added", "purchased", "reverted", "archived"})


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    id: int
    item_id: int
    product_id: int | None
    list_id: int
    event_type: str
    quantity: Decimal
    unit: str
    actor_user_id: int | None
    occurred_at: datetime


@dataclass(frozen=True)
class Purchase:
    item_id: int
    product_id: int | None
    list_id: int
    quantity: Decimal
    unit: str
    purchased_at: datetime

    @property
    def purchase_date(self) -> date:
        return self.purchased_at.date()  # UTC date: see KNOWN SIMPLIFICATION


@dataclass(frozen=True)
class Quarantined:
    event_id: int | None
    item_id: int | None
    reason: str


# ---------------------------------------------------------------------------
# Reading the raw files
# ---------------------------------------------------------------------------

def _optional_int(text: str) -> int | None:
    """CSV cannot tell NULL from empty text, so empty means None (known
    weakness of CSV: this is a guess we are forced to make)."""
    return int(text) if text != "" else None


def parse_event(row: dict[str, str]) -> Event:
    """Turn one CSV row (all text) into a typed Event, or raise ValueError."""
    try:
        occurred_at = datetime.fromisoformat(row["occurred_at"])
        if occurred_at.tzinfo is None:
            raise ValueError("timestamp has no time zone")
        return Event(
            id=int(row["id"]),
            item_id=int(row["item_id"]),
            product_id=_optional_int(row["product_id"]),
            list_id=int(row["list_id"]),
            event_type=row["event_type"],
            quantity=Decimal(row["quantity"]),
            unit=row["unit"],
            actor_user_id=_optional_int(row["actor_user_id"]),
            occurred_at=occurred_at,
        )
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise ValueError(str(exc) or exc.__class__.__name__) from exc


def validate_event(event: Event) -> str | None:
    """Rules about ONE event on its own. Returns the reason, or None if fine."""
    if event.event_type not in EVENT_TYPES:
        return f"unknown event_type '{event.event_type}'"
    if not event.quantity.is_finite() or event.quantity <= 0:
        return f"quantity not positive: {event.quantity}"
    if event.unit not in SUPPORTED_UNITS:
        return f"unsupported unit '{event.unit}'"
    return None


def load_events(raw_dir: Path) -> tuple[list[Event], list[Quarantined], dict]:
    """Read every batch file. Returns (valid-looking events, rows that could
    not even be parsed, statistics)."""
    events: dict[int, Event] = {}
    unparsed: list[Quarantined] = []
    stats = {"files": 0, "rows_read": 0, "duplicates": 0}

    for path in sorted((raw_dir / "item_events").glob("item_events_*.csv")):
        stats["files"] += 1
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                stats["rows_read"] += 1
                try:
                    event = parse_event(row)
                except ValueError as exc:
                    unparsed.append(Quarantined(
                        _safe_int(row.get("id")), _safe_int(row.get("item_id")),
                        f"unparseable row: {exc}",
                    ))
                    continue
                if event.id in events:
                    # The same id in two files (e.g. a re-run). Keep the first;
                    # the source is append-only so both copies are identical.
                    stats["duplicates"] += 1
                    continue
                events[event.id] = event

    return list(events.values()), unparsed, stats


def _safe_int(text: str | None) -> int | None:
    try:
        return int(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def load_product_names(raw_dir: Path) -> dict[int, tuple[str, str]]:
    """product id -> (name, normalized_name), from the NEWEST snapshot."""
    snapshots = sorted((raw_dir / "products").glob("products_*.csv"))
    if not snapshots:
        return {}
    with open(snapshots[-1], newline="", encoding="utf-8") as handle:
        return {
            int(r["id"]): (r["name"], r["normalized_name"])
            for r in csv.DictReader(handle)
        }


# ---------------------------------------------------------------------------
# Replaying each item
# ---------------------------------------------------------------------------

def evaluate_item(events: list[Event]) -> tuple[Purchase | None, str | None]:
    """Replay one item's history (already in time order).

    Returns (purchase, None) for a consistent history (purchase is None if the
    item was never bought, or the purchase was reverted), or (None, reason)
    for an inconsistent one.

    Rules of a valid history:
      - it starts with 'added', and 'added' appears only once
      - 'purchased' only while the item is not currently bought
      - 'reverted' only while it is bought (it cancels that purchase)
      - 'archived' is the end: nothing may follow it
    """
    bought: Event | None = None
    archived = False

    for position, event in enumerate(events):
        kind = event.event_type

        if position == 0:
            if kind != "added":
                return None, f"history starts with '{kind}', not 'added'"
            continue
        if archived:
            return None, f"'{kind}' after 'archived'"
        if kind == "added":
            return None, "'added' appears twice"
        if kind == "purchased":
            if bought is not None:
                return None, "'purchased' while already purchased"
            bought = event
        elif kind == "reverted":
            if bought is None:
                return None, "'reverted' while not purchased"
            bought = None
        elif kind == "archived":
            archived = True

    if bought is None:
        return None, None
    return Purchase(
        item_id=bought.item_id,
        product_id=bought.product_id,
        list_id=bought.list_id,
        quantity=bought.quantity,
        unit=bought.unit,
        purchased_at=bought.occurred_at,
    ), None


def build_purchases(
    events: list[Event],
    tainted_items: dict[int, str] | None = None,
) -> tuple[list[Purchase], list[Quarantined]]:
    """Group events by item, replay each, collect purchases and rejects."""
    tainted = dict(tainted_items or {})
    quarantined: list[Quarantined] = []

    # Rule 1: single-event checks. One bad event taints its whole item.
    for event in events:
        reason = validate_event(event)
        if reason and event.item_id not in tainted:
            tainted[event.item_id] = reason

    by_item: dict[int, list[Event]] = defaultdict(list)
    for event in events:
        by_item[event.item_id].append(event)

    purchases: list[Purchase] = []
    for item_id, item_events in by_item.items():
        # Time order; ties broken by id (the order the database recorded them).
        item_events.sort(key=lambda e: (e.occurred_at, e.id))

        reason = tainted.get(item_id)
        purchase = None
        if reason is None:
            purchase, reason = evaluate_item(item_events)

        if reason is not None:
            quarantined.extend(
                Quarantined(e.id, item_id, reason) for e in item_events
            )
        elif purchase is not None:
            purchases.append(purchase)

    purchases.sort(key=lambda p: (p.purchased_at, p.item_id))
    return purchases, quarantined


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------

def compute_intervals(
    purchases: list[Purchase],
    names: dict[int, tuple[str, str]],
) -> list[dict]:
    """Days between consecutive purchases of the same product."""
    by_product: dict[int, list[Purchase]] = defaultdict(list)
    for purchase in purchases:
        if purchase.product_id is not None:  # custom items have no product
            by_product[purchase.product_id].append(purchase)

    rows = []
    for product_id, items in by_product.items():
        items.sort(key=lambda p: p.purchased_at)
        label = names.get(product_id, (f"product {product_id}", ""))[0]
        for previous, current in zip(items, items[1:]):
            rows.append({
                "product_id": product_id,
                "product_name": label,
                "previous_purchase_date": previous.purchase_date,
                "purchase_date": current.purchase_date,
                "days_since_previous": (current.purchase_date - previous.purchase_date).days,
            })
    return rows


def summarise_by_product(
    purchases: list[Purchase],
    intervals: list[dict],
    names: dict[int, tuple[str, str]],
) -> dict[str, dict]:
    """normalized name -> {name, purchases, intervals_days}, for reporting
    and for the comparison with the answer key."""
    summary: dict[str, dict] = {}
    for product_id, (name, normalized) in names.items():
        summary[normalized] = {
            "name": name,
            "purchases": sum(1 for p in purchases if p.product_id == product_id),
            "intervals_days": [
                r["days_since_previous"] for r in intervals if r["product_id"] == product_id
            ],
        }
    return summary


def compare_with_truth(summary: dict[str, dict], truth: dict) -> list[dict]:
    """Compare what the pipeline found with the generator's answer key."""
    results = []
    for name, expected in truth["products"].items():
        found = summary.get(name.strip().casefold())
        if found is None:
            results.append({"product": name, "ok": False, "detail": "product not found"})
            continue
        same_count = found["purchases"] == expected["purchases"]
        same_intervals = found["intervals_days"] == expected["intervals_days"]
        results.append({
            "product": name,
            "ok": same_count and same_intervals,
            "detail": (
                f"purchases {found['purchases']}/{expected['purchases']}, "
                f"intervals {'identical' if same_intervals else 'DIFFER'}"
            ),
        })
    return results


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def run(raw_dir: Path, out_dir: Path, truth_path: Path | None = None) -> int:
    """Clean the raw files and report. Returns 0 on success, 1 on a problem
    (no input, or a mismatch with the answer key)."""
    events, unparsed, stats = load_events(raw_dir)
    if stats["files"] == 0:
        print(f"No raw files in {raw_dir}. Run the extraction first.")
        return 1

    # An unparseable event makes its item untrustworthy too.
    tainted = {q.item_id: q.reason for q in unparsed if q.item_id is not None}
    purchases, quarantined = build_purchases(events, tainted)
    quarantined = unparsed + [q for q in quarantined if q not in unparsed]

    names = load_product_names(raw_dir)
    intervals = compute_intervals(purchases, names)
    summary = summarise_by_product(purchases, intervals, names)

    write_csv_atomic(
        out_dir / "purchases.csv",
        ("item_id", "product_id", "product_name", "list_id", "quantity",
         "unit", "purchased_at", "purchase_date"),
        [
            {**vars(p), "purchase_date": p.purchase_date,
             "product_name": names.get(p.product_id, ("", ""))[0]}
            for p in purchases
        ],
    )
    write_csv_atomic(
        out_dir / "purchase_intervals.csv",
        ("product_id", "product_name", "previous_purchase_date",
         "purchase_date", "days_since_previous"),
        intervals,
    )
    write_csv_atomic(
        out_dir / "quarantine.csv",
        ("event_id", "item_id", "reason"),
        [vars(q) for q in quarantined],
    )

    print(f"read {stats['rows_read']} rows from {stats['files']} file(s); "
          f"{stats['duplicates']} duplicate(s) ignored")
    print(f"{len(purchases)} purchases, {len(quarantined)} event(s) quarantined\n")
    print(f"{'product':<12}{'purchases':>10}{'avg days':>10}")
    for entry in summary.values():
        gaps = entry["intervals_days"]
        average = f"{sum(gaps) / len(gaps):.2f}" if gaps else "-"
        print(f"{entry['name']:<12}{entry['purchases']:>10}{average:>10}")

    if quarantined:
        print("\nQuarantine reasons:")
        for reason in sorted({q.reason for q in quarantined}):
            print(f"  - {reason}")

    if truth_path:
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        results = compare_with_truth(summary, truth)
        print("\nComparison with the answer key:")
        for result in results:
            print(f"  {'OK  ' if result['ok'] else 'FAIL'} {result['product']:<12}{result['detail']}")
        if not all(r["ok"] for r in results):
            return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean raw events into purchases.")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_CLEAN_DIR)
    parser.add_argument("--truth", type=Path, help="answer key to compare against")
    args = parser.parse_args()
    return run(args.raw_dir, args.out_dir, args.truth)


if __name__ == "__main__":
    sys.exit(main())
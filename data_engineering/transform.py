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

TWO MORE SAFETY RULES
---------------------
- DUPLICATE IDS: the same event id in two raw files is normal after a re-run,
  and harmless when the copies are identical (counted and ignored). If the
  copies DIFFER, the source or the extraction is broken: we keep neither,
  quarantine the item(s) involved and make the run end with an error code.
- ALL-OR-NOTHING OUTPUT: the three result files are published together as
  one "run" folder, and a small pointer file is switched to it in one atomic
  step (see `publish_run`). A reader sees the previous complete run or the
  new complete run, never a mixture.

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
import hashlib
import json
import os
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backend.app.core.units import SUPPORTED_UNITS
from data_engineering.extract import DEFAULT_OUT_DIR as DEFAULT_RAW_DIR
from data_engineering.extract import render_csv

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
    conflicted: set[int] = set()   # ids seen with two different contents
    unparsed: list[Quarantined] = []
    stats = {"files": 0, "rows_read": 0, "duplicates": 0, "conflicts": 0}

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
                if event.id in conflicted:
                    continue  # already reported; a third copy adds nothing
                if event.id in events:
                    kept = events[event.id]
                    if event == kept:
                        # A re-run wrote the same row again. Harmless.
                        stats["duplicates"] += 1
                        continue
                    # Same id, different content. The source is append-only,
                    # so this should be impossible: something is broken, and
                    # we cannot know which copy is true. Keep NEITHER, and
                    # taint every item either copy claims to belong to.
                    differing = [f.name for f in fields(Event)
                                 if getattr(kept, f.name) != getattr(event, f.name)]
                    reason = (f"conflicting copies of event id {event.id} "
                              f"(differ in: {', '.join(differing)})")
                    stats["conflicts"] += 1
                    del events[event.id]
                    conflicted.add(event.id)
                    for item_id in sorted({kept.item_id, event.item_id}):
                        unparsed.append(Quarantined(event.id, item_id, reason))
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

# Fields that describe WHAT the item is. The backend has no endpoint that
# edits them after creation, and every event copies them from the item, so all
# events of one item must carry identical values. (item_id is the grouping key,
# so it is the same by construction; actor_user_id and occurred_at legitimately
# differ per event and are not checked.)
#
# If the backend ever gains an "edit quantity/unit" feature, this rule must be
# revisited, otherwise legitimate edits would quarantine whole items.
CONSTANT_FIELDS = ("list_id", "product_id", "quantity", "unit")


def check_item_consistency(events: list[Event]) -> str | None:
    """Cross-event check: do all events of one item agree on what the item is?

    Every event can be valid on its own and the sequence of states can be
    correct, while the events still describe different things, for example
    added(product 10, qty 2) -> purchased(product 15, qty 5). Whichever
    event we trusted for the purchase would be a guess, so the whole item
    is rejected (the caller quarantines it) and the reason lists every
    field that disagrees.
    """
    first = events[0]
    problems = []
    for field in CONSTANT_FIELDS:
        values = [getattr(e, field) for e in events]
        if any(v != values[0] for v in values[1:]):
            # dict.fromkeys keeps order and removes repeats: "10 -> 15 -> 10"
            shown = " -> ".join(str(v) for v in dict.fromkeys(values))
            problems.append(f"{field} ({shown})")
    if problems:
        return "inconsistent within item: " + ", ".join(problems)
    return None


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
            # Rule 2: do the events agree on what the item is?
            reason = check_item_consistency(item_events)
        if reason is None:
            # Rule 3: is the sequence of states possible?
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

# ---------------------------------------------------------------------------
# Publishing the results as ONE unit
# ---------------------------------------------------------------------------
#
# Layout:
#   clean/_latest.json                 <- pointer: {"run_id": ...}
#   clean/runs/<run_id>/purchases.csv, purchase_intervals.csv, quarantine.csv
#
# Writing each file atomically is not enough: a crash after the first file
# leaves a mixture of two runs. Instead:
#   1. write all files into a hidden staging folder,
#   2. rename the folder to runs/<run_id>  (one atomic rename),
#   3. switch the pointer file to it       (one atomic rename).
# Step 3 is the COMMIT. Before it, readers still see the previous run; after
# it, the new one. There is no moment with a mixture.
#
# The run id is a hash of the content, so re-running on unchanged data
# produces the same id and creates nothing new.

RUNS_DIR = "runs"
POINTER_FILE = "_latest.json"
KEEP_RUNS = 5   # the current run plus the most recent older ones


def publish_run(out_dir: Path, tables: dict[str, str]) -> str:
    """Publish rendered CSV texts as one run. Returns the run id."""
    digest = hashlib.sha256()
    for name in sorted(tables):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(tables[name].encode("utf-8") + b"\0")
    run_id = digest.hexdigest()[:12]

    runs = out_dir / RUNS_DIR
    runs.mkdir(parents=True, exist_ok=True)
    for leftover in runs.glob(".tmp-*"):          # from an earlier crash
        shutil.rmtree(leftover, ignore_errors=True)

    run_dir = runs / run_id
    if not run_dir.exists():
        staging = runs / f".tmp-{run_id}"
        staging.mkdir()
        for name, text in tables.items():
            # newline="" writes the text exactly as rendered by the csv module.
            (staging / name).write_text(text, encoding="utf-8", newline="")
        os.replace(staging, run_dir)               # the folder appears complete

    pointer = out_dir / POINTER_FILE
    temporary = pointer.with_name(pointer.name + ".tmp")
    temporary.write_text(json.dumps({
        "run_id": run_id,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(tables),
    }, indent=2), encoding="utf-8")
    os.replace(temporary, pointer)                 # COMMIT

    # Tidy up AFTER the commit: a failure here cannot hurt the result.
    older = sorted(
        (d for d in runs.iterdir()
         if d.is_dir() and d.name != run_id and not d.name.startswith(".")),
        key=lambda d: d.stat().st_mtime, reverse=True,
    )
    for old in older[KEEP_RUNS - 1:]:
        shutil.rmtree(old, ignore_errors=True)
    return run_id


def latest_clean_dir(out_dir: Path) -> Path:
    """The folder holding the newest COMPLETE results. Consumers (the ML
    layer) use this instead of hard-coding paths."""
    pointer = out_dir / POINTER_FILE
    if not pointer.exists():
        raise FileNotFoundError(f"No published results in {out_dir}. Run the pipeline first.")
    run_id = json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
    return out_dir / RUNS_DIR / run_id


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

    # Render all three files in memory first, then publish them together.
    tables = {
        "purchases.csv": render_csv(
            ("item_id", "product_id", "product_name", "list_id", "quantity",
             "unit", "purchased_at", "purchase_date"),
            [
                {**vars(p), "purchase_date": p.purchase_date,
                 "product_name": names.get(p.product_id, ("", ""))[0]}
                for p in purchases
            ],
        ),
        "purchase_intervals.csv": render_csv(
            ("product_id", "product_name", "previous_purchase_date",
             "purchase_date", "days_since_previous"),
            intervals,
        ),
        "quarantine.csv": render_csv(
            ("event_id", "item_id", "reason"),
            [vars(q) for q in quarantined],
        ),
    }
    run_id = publish_run(out_dir, tables)


    problem = False
    print(f"published run {run_id} ({out_dir / RUNS_DIR / run_id})")
    print(f"read {stats['rows_read']} rows from {stats['files']} file(s); "
          f"{stats['duplicates']} duplicate(s) ignored")
    if stats["conflicts"]:
        # The items are isolated, so the numbers are safe, but a conflict is
        # a fault in the pipeline or the source: signal it with an error code.
        problem = True
        print(f"ERROR: {stats['conflicts']} event id(s) appear twice with "
              "DIFFERENT content. Items involved were quarantined; investigate "
              "the source and the extraction.")
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
            problem = True

    return 1 if problem else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean raw events into purchases.")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_CLEAN_DIR)
    parser.add_argument("--truth", type=Path, help="answer key to compare against")
    args = parser.parse_args()
    return run(args.raw_dir, args.out_dir, args.truth)


if __name__ == "__main__":
    sys.exit(main())
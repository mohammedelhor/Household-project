"""Suggestions: "which products will this household (or user) probably need soon?"

    suggest(events, names, list_scopes, as_of=now)  ->  one Suggestion per (scope, product)

A PURE function: no database, no files, no clock (the moment is a parameter).
That is what makes it testable with exact numbers, and it means the API layer
later is only a thin wrapper that reads the published file.

SCOPES
------
A scope is the owner of a shopping list (a household or a single user, see
data_engineering/scopes.py). The rhythm of a product is computed per scope:
Family A's milk (every 7 days) and Family B's milk (every 30) are never mixed.
A (scope, product) with no purchase in that scope yields no suggestion: the
system does not guess what a household buys from what others buy.

THE RULE (the Historical Median Baseline from ml/report.py)
-----------------------------------------------------------
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
  of the SAME scope waiting to be bought. The caller should not suggest what
  is already there.

HISTORY STRENGTH (not a probability)
------------------------------------
  none: 0 gaps   short: 1-2   moderate: 3-6   long: 7 or more
It says how much history the rule used, nothing about how likely the guess is
to be right (see ml/evidence.py, the single place the thresholds live, and
ml/report.py, which measures the error per tier).
"""

from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from data_engineering.scopes import Scope
from data_engineering.transform import Event
from ml.asof import events_as_of, purchases_as_of
from ml.evidence import MIN_GAPS_TO_SUGGEST, history_strength_for

SOON_DAYS = 2
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Suggestion:
    scope: Scope
    product_id: int
    product_name: str
    last_purchase_date: date | None
    typical_gap_days: int | None        # the median gap, in whole days
    expected_date: date | None
    days_until: int | None              # negative = overdue by that many days
    status: str                         # overdue | due_soon | not_yet | unknown
    history_strength: str               # none | short | moderate | long
    gaps_used: int
    already_on_list: bool


def products_waiting_on_lists(
    events: list[Event], list_scopes: dict[int, Scope]
) -> dict[Scope, set[int]]:
    """Per scope: products with at least one item neither bought nor archived.

    Replays each item the same way the cleaning step does: a purchase puts the
    item in the 'bought' state, a revert takes it back, archiving ends it. The
    item's scope is the scope of its list.
    """
    by_item: dict[int, list[Event]] = defaultdict(list)
    for event in events:
        by_item[event.item_id].append(event)

    waiting: dict[Scope, set[int]] = defaultdict(set)
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
        first = item_events[0]
        scope = list_scopes.get(first.list_id)
        if first.product_id is not None and scope is not None and not bought and not archived:
            waiting[scope].add(first.product_id)
    return waiting


def status_for(days_until: int) -> str:
    if days_until < 0:
        return "overdue"
    return "due_soon" if days_until <= SOON_DAYS else "not_yet"


def suggest(events: list[Event], names: dict[int, str], list_scopes: dict[int, Scope],
            *, as_of: datetime) -> list[Suggestion]:
    """One suggestion per (scope, product) with a purchase in that scope,
    most urgent first inside each scope (scopes in order)."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    today = as_of.astimezone(timezone.utc).date()

    known = events_as_of(events, as_of)                   # what was known, nothing else
    purchases, _ = purchases_as_of(events, as_of, list_scopes)
    waiting = products_waiting_on_lists(known, list_scopes)

    dates_by_unit: dict[tuple[Scope, int], list[date]] = defaultdict(list)
    for purchase in purchases:                            # already in time order
        if purchase.product_id is not None and purchase.scope is not None:
            dates_by_unit[(purchase.scope, purchase.product_id)].append(purchase.purchase_date)

    suggestions: list[Suggestion] = []
    for (scope, product_id), dates in dates_by_unit.items():
        name = names.get(product_id, str(product_id))
        on_list = product_id in waiting.get(scope, set())
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]

        if len(gaps) < MIN_GAPS_TO_SUGGEST:
            suggestions.append(Suggestion(
                scope, product_id, name, dates[-1], None, None, None,
                "unknown", "none", 0, on_list))
            continue

        gap = int(statistics.median(gaps) + 0.5)          # .5 rounds up, like the evaluation
        expected = dates[-1] + timedelta(days=gap)
        days_until = (expected - today).days
        suggestions.append(Suggestion(
            scope, product_id, name, dates[-1], gap, expected, days_until,
            status_for(days_until), history_strength_for(len(gaps)), len(gaps), on_list))

    # Scope by scope; most urgent first (most overdue = smallest days_until); unknown last.
    suggestions.sort(key=lambda s: (s.scope, s.days_until is None, s.days_until or 0, s.product_name))
    return suggestions


# ---------------------------------------------------------------------------
# Publishing for the API (the API never touches the database or the ML code)
# ---------------------------------------------------------------------------
#
# The API only needs to READ a small file. This is the contract between the
# two layers, so it is deliberately plain JSON with a version number:
#
#   {"schema_version": 2, "generated_at": ..., "as_of_date": ...,
#    "dataset": {"run_id": ..., "input_fingerprint": ...},
#    "suggestions": [{"scope_type", "scope_id", "product_id", "product_name",
#                     "last_purchase_date", "typical_gap_days", "expected_date",
#                     "history_strength", "gaps_used"}, ...]}
#
# Version 2 (from 1): every suggestion belongs to a scope (the API must show a
# user only the scopes they belong to), and `confidence` was renamed to
# `history_strength` because it only describes how much history there was.
#
# WHAT IS LEFT OUT ON PURPOSE: `status` and `days_until` change every day and
# `already_on_list` changes whenever someone edits a list, so a file written
# at 09:00 would be wrong by evening. The file keeps only what stays true
# (the expected DATE); the API works out status and days from today's date.

SERVING_SCHEMA_VERSION = 2
DEFAULT_SERVING_PATH = PROJECT_ROOT / "data" / "serving" / "suggestions.json"


def build_serving_payload(suggestions: list[Suggestion], *, run_id: str, fingerprint: str,
                          generated_at: datetime) -> dict:
    return {
        "schema_version": SERVING_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "as_of_date": generated_at.astimezone(timezone.utc).date().isoformat(),
        "dataset": {"run_id": run_id, "input_fingerprint": fingerprint},
        "suggestions": [
            {
                "scope_type": s.scope.type,
                "scope_id": s.scope.id,
                "product_id": s.product_id,
                "product_name": s.product_name,
                "last_purchase_date": s.last_purchase_date.isoformat() if s.last_purchase_date else None,
                "typical_gap_days": s.typical_gap_days,
                "expected_date": s.expected_date.isoformat() if s.expected_date else None,
                "history_strength": s.history_strength,
                "gaps_used": s.gaps_used,
            }
            for s in suggestions
        ],
    }


def write_serving_file(path: Path, payload: dict) -> None:
    """Write atomically: the API reads this file while we may be replacing it,
    and must see either the whole old file or the whole new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)          # atomic on the same filesystem


def main() -> int:
    """Print today's suggestions from the latest published data.

        uv run python -m ml.suggest
        uv run python -m ml.suggest --as-of 2026-10-20     # what it would have said then
        uv run python -m ml.suggest --publish              # write the file the API serves
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
    parser.add_argument("--publish", action="store_true", help="write the file the API serves")
    parser.add_argument("--serving-path", type=Path, default=DEFAULT_SERVING_PATH)
    args = parser.parse_args()
    if args.publish and args.as_of:
        # A back-dated file would be served as if it were current.
        print("--publish cannot be combined with --as-of")
        return 1

    try:
        events, manifest, names, list_scopes = load_published_dataset(args.raw_dir, args.clean_dir)
    except (FileNotFoundError, StaleInputError) as exc:
        print(f"CANNOT SUGGEST: {exc}")
        return 1

    moment = (datetime.combine(args.as_of, datetime.max.time(), tzinfo=timezone.utc)
              if args.as_of else datetime.now(timezone.utc))
    print(f"dataset run: {manifest['run_id']}   as of: {moment.date()}\n")
    results = suggest(events, names, list_scopes, as_of=moment)
    print(f"{'scope':12} {'product':12} {'status':9} {'days':>5} {'expected':11} {'gap':>4} {'history':8} note")
    for s in results:
        note = "already on a list" if s.already_on_list else ""
        print(f"{s.scope.key:12} {s.product_name:12} {s.status:9} "
              f"{'-' if s.days_until is None else format(s.days_until, '+d'):>5} "
              f"{str(s.expected_date or '-'):11} {s.typical_gap_days or '-':>4} "
              f"{s.history_strength:8} {note}")

    if args.publish:
        payload = build_serving_payload(
            results, run_id=manifest["run_id"],
            fingerprint=manifest["inputs"]["fingerprint"], generated_at=moment)
        write_serving_file(args.serving_path, payload)
        print(f"\npublished for the API: {args.serving_path}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

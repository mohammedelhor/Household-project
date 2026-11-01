"""Quality gate: a clear PASS or FAIL before results reach the ML layer.

A report of errors that nobody has to act on is not a gate. This one returns
a verdict, and the caller (transform.run) uses it to decide whether the new
results become "the latest" that the ML layer reads:

    PASS  -> published, the pointer moves to the new run.
    FAIL  -> the run folder is kept for diagnosis, but the pointer does NOT
             move: training keeps reading the last run that passed, and the
             pipeline ends with an error code.

TWO SEVERITIES
--------------
  critical: the data is unsafe to learn from (wrong shape, impossible values,
            broken links, most of the input rejected). Any failure = FAIL.
  warning : suspicious but possibly real life (a quiet week, a few rejected
            events). Reported, never blocks.

HOW THE THRESHOLDS WERE CHOSEN (so you can redo it by hand)
-----------------------------------------------------------
Start from what you observe, then leave room for normal variation:
  * Share of events rejected into quarantine: observed 0% on clean data and
    ~5% in our own stress test (mistake_rate=0.05 in the generator, which
    the pipeline still handles correctly). So: WARN above 5%. FAIL at 50%:
    if most of the input is unusable the pipeline or the source is broken,
    not just "a few bad taps".
  * Freshness: a household can skip a week without shopping, so 7 days is
    only a WARNING.
  * Future dates: a clock can be off by hours, so tolerate 1 day; beyond that
    a "purchase in the future" is a bug, and it would also break any
    time-based split later, so it is critical.
After a few weeks of real runs, look at the distribution of these numbers and
move the thresholds; they are constants below for exactly that reason.

The gate checks the RENDERED CSV TEXT, i.e. exactly what the ML layer will read.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from data_engineering.contracts import CONTRACTS, validate_table

QUARANTINE_WARN_SHARE = 0.05      # more than 5% of events rejected -> warning
QUARANTINE_FAIL_SHARE = 0.50      # half or more rejected          -> FAIL
FUTURE_TOLERANCE = timedelta(days=1)
STALE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class Check:
    name: str
    severity: str        # "critical" | "warning"
    passed: bool
    detail: str


@dataclass(frozen=True)
class GateReport:
    checks: tuple[Check, ...]

    @property
    def status(self) -> str:
        failed_critical = any(c.severity == "critical" and not c.passed for c in self.checks)
        return "FAIL" if failed_critical else "PASS"

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "critical" and not c.passed]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "warning" and not c.passed]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "critical_failures": len(self.failures),
            "warnings": len(self.warnings),
            "checks": [vars(c) for c in self.checks],
        }


def _rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text, newline="")))


def run_gate(
    tables: dict[str, str],
    *,
    known_product_ids: set[int],
    events_read: int,
    quarantined_events: int,
    newest_event_at: datetime | None,
    previous_purchases: int | None,
    now: datetime,
) -> GateReport:
    """Evaluate every check. `tables` maps file name -> rendered CSV text."""
    checks: list[Check] = []

    def add(name: str, severity: str, passed: bool, detail: str = "") -> None:
        checks.append(Check(name, severity, passed, detail if not passed else (detail or "ok")))

    # ---- 1. every file honours its contract (columns, types, allowed values, ...)
    contract_ok = True
    for file_name, contract in CONTRACTS.items():
        violations = validate_table(contract, tables[file_name])
        contract_ok &= not violations
        add(f"contract: {file_name}", "critical", not violations,
            " | ".join(v.describe() for v in violations))

    # Cross-file checks need readable rows. If a file broke its contract the
    # cells cannot be trusted, so stop here: the FAIL is already certain.
    if not contract_ok:
        return GateReport(tuple(checks))

    purchases = _rows(tables["purchases.csv"])
    intervals = _rows(tables["purchase_intervals.csv"])

    # ---- 2. columns that describe the same thing must agree
    mismatched = [
        r["item_id"] for r in purchases
        if datetime.fromisoformat(r["purchased_at"]).astimezone(timezone.utc).date().isoformat()
        != r["purchase_date"]
    ]
    add("purchase_date matches purchased_at (UTC)", "critical", not mismatched,
        f"{len(mismatched)} row(s), first items: {mismatched[:3]}")

    # ---- 3. links between tables
    unknown = sorted({int(r["product_id"]) for r in purchases
                      if r["product_id"] and int(r["product_id"]) not in known_product_ids})
    add("every purchase's product exists in the products snapshot", "critical", not unknown,
        f"unknown product ids: {unknown[:5]}")

    # Intervals must be derivable from the purchases: for each product,
    # (number of purchases - 1) intervals, and their days must add up to the
    # span between the first and last purchase date.
    dates: dict[int, list[date]] = defaultdict(list)
    for r in purchases:
        if r["product_id"]:
            dates[int(r["product_id"])].append(date.fromisoformat(r["purchase_date"]))
    interval_count: dict[int, int] = defaultdict(int)
    interval_days: dict[int, int] = defaultdict(int)
    for r in intervals:
        interval_count[int(r["product_id"])] += 1
        interval_days[int(r["product_id"])] += int(r["days_since_previous"])
    inconsistent = [
        product for product in set(dates) | set(interval_count)
        if interval_count[product] != max(len(dates.get(product, [])) - 1, 0)
        or interval_days[product] != ((max(dates[product]) - min(dates[product])).days
                                      if dates.get(product) else 0)
    ]
    add("intervals can be derived from the purchases", "critical", not inconsistent,
        f"inconsistent products: {sorted(inconsistent)[:5]}")

    # ---- 4. time sanity
    future = [r["item_id"] for r in purchases
              if datetime.fromisoformat(r["purchased_at"]) > now + FUTURE_TOLERANCE]
    add("no purchase is in the future", "critical", not future,
        f"{len(future)} purchase(s) later than now + 1 day, items: {future[:3]}")

    # ---- 5. how much of the input was rejected
    share = quarantined_events / events_read if events_read else 0.0
    add(f"less than {QUARANTINE_FAIL_SHARE:.0%} of events rejected", "critical",
        share < QUARANTINE_FAIL_SHARE, f"{share:.1%} of {events_read} events rejected")
    add(f"at most {QUARANTINE_WARN_SHARE:.0%} of events rejected", "warning",
        share <= QUARANTINE_WARN_SHARE, f"{share:.1%} of {events_read} events rejected")

    # ---- 6. things that are suspicious but can be normal life
    add("there is at least one purchase", "warning", bool(purchases),
        "no purchases yet: nothing to learn from")
    if previous_purchases is not None:
        add("purchases did not shrink since the previous run", "warning",
            len(purchases) >= previous_purchases,
            f"{len(purchases)} now vs {previous_purchases} before "
            "(a revert can explain it, an unexplained drop should be looked at)")
    if newest_event_at is not None:
        age = now - newest_event_at
        add("newest event is recent", "warning", age <= STALE_AFTER,
            f"newest event is {age.days} day(s) old")

    return GateReport(tuple(checks))
"""Data contracts: the exact shape of what the cleaning step hands to the ML layer.

WHY A CONTRACT
--------------
Two layers that talk through files need an agreement written down: which
columns exist, what type each one has, which may be empty, which values are
allowed, and what each column MEANS (and in which unit). Without it, a change
on one side (a renamed column, a new unit) surprises the other side in the
middle of a training run, or worse, silently changes the model's inputs.

With the contract in code:
  * the quality gate (quality_gate.py) checks every output against it BEFORE
    the results are published, so a breaking change is a loud FAIL, not a
    surprise;
  * the ML layer can read the types from here instead of guessing them from
    text, which is also the bridge to Parquet later (the contract IS the
    schema);
  * DATA_DICTIONARY.md is generated from it, so documentation cannot drift
    away from the code (a test checks that).

VERSIONING (CONTRACT_VERSION, "major.minor.patch")
-------------------------------------------------
  major: a column was removed, renamed, or changed meaning/type  (breaks readers)
  minor: a column was ADDED at the end                           (old readers still work)
  patch: wording only
Every published run records the contract version in its manifest.

CSV NOTE
--------
CSV has no NULL: an empty cell means "no value". A column marked nullable
below may be empty; a column not marked nullable must never be.

USAGE (regenerates data_engineering/DATA_DICTIONARY.md after you change a contract)
-----
    uv run python -m data_engineering.contracts

(It writes the file itself instead of printing, because Windows PowerShell's
`>` would save it in a different text encoding.)
"""

from __future__ import annotations

import csv
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from backend.app.core.units import SUPPORTED_UNITS
from data_engineering.scopes import SCOPE_TYPES

# 2.0.0: purchases and intervals now belong to a SCOPE (household or user).
# The columns were added, but the MEANING of purchase_intervals changed (it was
# per product, it is now per scope and product), and a reader that still
# groups by product alone would silently mix households: a major change.
CONTRACT_VERSION = "2.0.0"
DICTIONARY_PATH = Path(__file__).with_name("DATA_DICTIONARY.md")

DTYPES = ("int", "text", "decimal", "date", "datetime")


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    name: str
    dtype: str                       # one of DTYPES
    description: str                 # what it MEANS (this is the data dictionary)
    nullable: bool = False           # may the cell be empty?
    unit: str | None = None          # unit of measurement, if it has one
    allowed: frozenset[str] | None = None    # closed set of legal values
    minimum: int | None = None       # numeric lower bound ...
    minimum_exclusive: bool = False  # ... exclusive when True (value > minimum)


@dataclass(frozen=True)
class TableContract:
    file: str
    description: str
    columns: tuple[Column, ...]
    unique: tuple[str, ...] = ()     # columns whose values must not repeat

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


PURCHASES = TableContract(
    file="purchases.csv",
    description=(
        "One row per shopping-list item that is STILL marked as purchased at "
        "the end of its history (a purchase that was reverted does not appear)."
    ),
    unique=("item_id", "source_event_id"),
    columns=(
        Column("item_id", "int", "Shopping-list item this purchase belongs to."),
        Column("product_id", "int", "Catalog product; empty for free-text items.",
               nullable=True),
        Column("product_name", "text", "Product name from the newest products "
               "snapshot; empty for free-text items.", nullable=True),
        Column("list_id", "int", "Shopping list the item was on."),
        Column("scope_type", "text", "Whose history this is: `household` for a "
               "household's shared list, `user` for a personal list.",
               allowed=frozenset(SCOPE_TYPES)),
        Column("scope_id", "int", "Id of the household or of the user (see "
               "`scope_type`). A user id and a household id with the same number "
               "are different scopes.", minimum=0, minimum_exclusive=True),
        Column("quantity", "decimal", "Quantity bought, in `unit`.",
               unit="see unit column", minimum=0, minimum_exclusive=True),
        Column("unit", "text", "Unit of `quantity`.",
               allowed=frozenset(SUPPORTED_UNITS)),
        Column("purchased_at", "datetime", "When the item was marked purchased "
               "(timezone-aware, UTC). It is the moment of the tap, not "
               "necessarily of the real purchase."),
        Column("purchase_date", "date", "UTC calendar date of `purchased_at`.",
               unit="UTC date"),
        Column("source_event_id", "int", "Id of the item_events row that "
               "recorded the purchase: lets any row be traced to the source."),
    ),
)

PURCHASE_INTERVALS = TableContract(
    file="purchase_intervals.csv",
    description=(
        "Days between consecutive purchases of the same product BY THE SAME "
        "SCOPE (items with no catalog product are skipped). Two households "
        "never share an interval. Rows are grouped by scope, then product, "
        "and ordered by time inside each. The quality gate recomputes every "
        "row from purchases.csv and rejects any difference."
    ),
    columns=(
        Column("scope_type", "text", "`household` or `user` (see purchases.csv).",
               allowed=frozenset(SCOPE_TYPES)),
        Column("scope_id", "int", "Id of the household or user.",
               minimum=0, minimum_exclusive=True),
        Column("product_id", "int", "Catalog product."),
        Column("product_name", "text", "Product name from the newest snapshot."),
        Column("previous_purchase_date", "date", "UTC date of the earlier purchase.",
               unit="UTC date"),
        Column("purchase_date", "date", "UTC date of the later purchase.",
               unit="UTC date"),
        Column("days_since_previous", "int", "Whole days between the two dates "
               "(0 = bought twice on the same date).", unit="days", minimum=0),
    ),
)

QUARANTINE = TableContract(
    file="quarantine.csv",
    description=(
        "Events that were NOT used, with the reason. Nothing is dropped "
        "silently; a person can review this file."
    ),
    columns=(
        Column("event_id", "int", "Id of the rejected event; empty if the row "
               "could not even be read.", nullable=True),
        Column("item_id", "int", "Item whose whole history was rejected; empty "
               "if unknown.", nullable=True),
        Column("reason", "text", "Why the event/item was rejected."),
    ),
)

CONTRACTS: dict[str, TableContract] = {
    c.file: c for c in (PURCHASES, PURCHASE_INTERVALS, QUARANTINE)
}

# What the data does and does NOT mean. These are the limits a model builder
# must know before choosing a target. They appear in DATA_DICTIONARY.md.
BUSINESS_NOTES = (
    "**Every purchase belongs to one scope: a household or a single user.** "
    "The scope is the owner of the list the item was on. Histories of "
    "different scopes must never be combined into one prediction unit, not "
    "even for the same product: the unit is (scope_type, scope_id, product_id).",
    "**Purchased means marked as purchased in the app.** It is not proof that "
    "the product was really bought, it is not the quantity in stock "
    "(inventory) and it is not the quantity used up (consumption).",
    "**An interval is the time between two such marks of the same product.** "
    "It is not how long the product lasted: people stockpile, buy early, or "
    "forget to tap. A model trained on it predicts the next purchase mark, so "
    "its output is a suggestion to add the product to the list, never a claim "
    "that the product ran out.",
    "**These tables show the FINAL state of every item (hindsight).** A "
    "purchase that was reverted later is absent, even for dates before the "
    "revert. Do not build training features straight from them: reconstruct "
    "what was known at each prediction time first (planned: as-of replay).",
    "**Dates are UTC dates.** A purchase late in the evening local time may "
    "fall on the next UTC date. This simplification is postponed on purpose.",
    "**An item with an inconsistent history is rejected as a whole** "
    "(see quarantine.csv); we cannot tell which of its events to believe.",
    "**CSV has no NULL:** an empty cell means no value.",
)


# ---------------------------------------------------------------------------
# Checking values against a contract
# ---------------------------------------------------------------------------

_INT = re.compile(r"^-?\d+$")


def check_value(column: Column, text: str) -> str | None:
    """Return None if `text` is a legal cell for `column`, else the reason."""
    if text == "":
        return None if column.nullable else "empty but required"

    if column.dtype == "int":
        if not _INT.match(text):
            return "not an integer"
    elif column.dtype == "decimal":
        try:
            if not Decimal(text).is_finite():
                return "not a finite number"
        except InvalidOperation:
            return "not a number"
    elif column.dtype == "date":
        try:
            date.fromisoformat(text)
        except ValueError:
            return "not an ISO date (YYYY-MM-DD)"
    elif column.dtype == "datetime":
        try:
            if datetime.fromisoformat(text).tzinfo is None:
                return "datetime has no time zone"
        except ValueError:
            return "not an ISO datetime"

    if column.allowed is not None and text not in column.allowed:
        return "value not allowed"
    if column.minimum is not None:
        value = Decimal(text)
        if value < column.minimum or (column.minimum_exclusive and value == column.minimum):
            sign = ">" if column.minimum_exclusive else ">="
            return f"must be {sign} {column.minimum}"
    return None


@dataclass(frozen=True)
class Violation:
    file: str
    rule: str            # short machine-friendly name, e.g. "columns", "unique:item_id"
    column: str | None
    count: int
    examples: tuple[str, ...]   # a few concrete cases (line numbers), for humans

    def describe(self) -> str:
        where = f"{self.file}" + (f".{self.column}" if self.column else "")
        shown = "; ".join(self.examples)
        return f"{where}: {self.rule} ({self.count} row(s)) e.g. {shown}"


def validate_table(contract: TableContract, csv_text: str, *, max_examples: int = 3) -> list[Violation]:
    """Check one rendered CSV against its contract. Returns all violations
    (an empty list means the file honours the contract)."""
    reader = csv.reader(io.StringIO(csv_text, newline=""))
    header = next(reader, [])
    expected = list(contract.column_names)

    if header != expected:
        missing = [c for c in expected if c not in header]
        extra = [c for c in header if c not in expected]
        detail = f"expected {expected}, got {header}"
        if missing:
            detail += f"; missing {missing}"
        if extra:
            detail += f"; unexpected {extra}"
        # Without the right header the cells cannot be interpreted: stop here.
        return [Violation(contract.file, "columns", None, 1, (detail,))]

    index = {name: position for position, name in enumerate(expected)}
    problems: dict[tuple[str, str], list[str]] = {}   # (rule, column) -> examples
    counts: dict[tuple[str, str], int] = {}
    seen: dict[str, dict[str, int]] = {name: {} for name in contract.unique}

    def note(rule: str, column: str, example: str) -> None:
        key = (rule, column)
        counts[key] = counts.get(key, 0) + 1
        if len(problems.setdefault(key, [])) < max_examples:
            problems[key].append(example)

    for line, row in enumerate(reader, start=2):      # line 1 is the header
        if len(row) != len(expected):
            note("row length", "", f"line {line} has {len(row)} cells")
            continue
        for column in contract.columns:
            reason = check_value(column, row[index[column.name]])
            if reason:
                note(reason, column.name, f"line {line}: {row[index[column.name]]!r}")
        for name in contract.unique:
            value = row[index[name]]
            if value in seen[name]:
                note("duplicate value in unique column", name,
                     f"line {line} repeats line {seen[name][value]}: {value!r}")
            else:
                seen[name][value] = line

    return [
        Violation(contract.file, rule, column or None, counts[(rule, column)], tuple(examples))
        for (rule, column), examples in problems.items()
    ]


# ---------------------------------------------------------------------------
# Data dictionary (generated documentation)
# ---------------------------------------------------------------------------

def render_data_dictionary() -> str:
    """The human-readable dictionary, generated from the contracts above."""
    lines = [
        "# Data dictionary: output of the cleaning step",
        "",
        f"Contract version: **{CONTRACT_VERSION}**. "
        "Generated from `data_engineering/contracts.py`; do not edit by hand "
        "(run `uv run python -m data_engineering.contracts`).",
        "",
        "## What the data means (read this first)",
        "",
    ]
    lines += [f"- {note}" for note in BUSINESS_NOTES]

    for contract in CONTRACTS.values():
        lines += ["", f"## {contract.file}", "", contract.description, ""]
        if contract.unique:
            lines += [f"Unique: {', '.join(f'`{c}`' for c in contract.unique)}.", ""]
        lines += ["| column | type | required | rules | meaning |", "|---|---|---|---|---|"]
        for column in contract.columns:
            rules = []
            if column.allowed:
                rules.append("one of " + ", ".join(sorted(column.allowed)))
            if column.minimum is not None:
                rules.append(f"{'>' if column.minimum_exclusive else '>='} {column.minimum}")
            if column.unit:
                rules.append(f"unit: {column.unit}")
            lines.append(
                f"| `{column.name}` | {column.dtype} | {'no' if column.nullable else 'yes'} "
                f"| {'; '.join(rules) or '-'} | {column.description} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    # newline="" keeps "\n" as is, so the file is identical on every system.
    DICTIONARY_PATH.write_text(render_data_dictionary(), encoding="utf-8", newline="")
    print(f"wrote {DICTIONARY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

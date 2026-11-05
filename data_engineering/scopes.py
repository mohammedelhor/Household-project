"""Scopes: WHOSE purchase history is this?

A prediction is only meaningful for one household (or one person). Two
households that both buy milk, one every 7 days and one every 30, must never
share a prediction. So every list, and therefore every item and every purchase,
belongs to exactly one SCOPE:

    list type          scope_type   scope_id
    ----------------   ----------   -----------------
    household list     household    households.id
    personal list      user         shopping_lists.owner_user_id

`scope_type` is part of the identity on purpose: user 3 and household 3 are
different scopes, and a bare number would confuse them.

WHERE THE SCOPE COMES FROM
--------------------------
item_events carry a list_id, and the database guarantees that every list has
exactly one owner (ck_shopping_list_exactly_one_owner). The extraction copies
a small snapshot of the lists (id, owner_user_id, household_id) next to the
events, and this module turns it into list_id -> Scope. No column was added to
item_events: ownership is recovered from the lists instead.

THE ASSUMPTIONS, AND HOW EACH ONE IS ENFORCED
---------------------------------------------
1. Every list_id in the events is in the snapshot.
   The snapshot is taken AFTER the events (lists are never deleted, only
   archived), so a list that has an event is already in it. If one is still
   missing, the item is rejected (quarantine) and the quality gate FAILS:
   we never guess a scope and never fall back to a "global" one.
2. A list has one scope for ever.
   The application has no endpoint that changes it, but a migration or a
   direct SQL statement could. Every snapshot is kept, and if two snapshots
   give the same list different scopes the quality gate FAILS (the history
   would otherwise be reassigned to a new owner after the fact). A database
   trigger that forbids the change is added by a migration.
3. Each snapshot row has exactly one owner. A row with none or both is
   invalid: the items on that list are rejected with that reason.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

HOUSEHOLD = "household"
USER = "user"
SCOPE_TYPES = (HOUSEHOLD, USER)

LIST_COLUMNS = ("id", "owner_user_id", "household_id")
LISTS_FOLDER = "shopping_lists"
LISTS_PATTERN = "shopping_lists_*.csv"
LISTS_ENCODING = "utf-8-sig"          # tolerate a byte order mark, like the other raw files


class ListSnapshotError(Exception):
    """The lists snapshot is missing or unreadable (nothing can be scoped)."""


@dataclass(frozen=True, order=True)
class Scope:
    type: str      # "household" or "user"
    id: int

    def __post_init__(self) -> None:
        if self.type not in SCOPE_TYPES:
            raise ValueError(f"unknown scope type {self.type!r}")

    @property
    def key(self) -> str:
        """Stable text form, e.g. 'household:3'. Used in files and reports."""
        return f"{self.type}:{self.id}"

    @staticmethod
    def parse(text: str) -> "Scope":
        kind, _, number = text.partition(":")
        return Scope(kind, int(number))


def scope_of_row(owner_user_id: str | None, household_id: str | None) -> Scope:
    """The scope a snapshot row describes. Raises ValueError unless EXACTLY one
    owner is set (the database enforces the same rule)."""
    owner = (owner_user_id or "").strip()
    household = (household_id or "").strip()
    if bool(owner) == bool(household):
        raise ValueError("a list must have exactly one owner (user or household), "
                         f"found owner_user_id={owner!r}, household_id={household!r}")
    return Scope(USER, int(owner)) if owner else Scope(HOUSEHOLD, int(household))


@dataclass
class ListScopes:
    by_list: dict[int, Scope] = field(default_factory=dict)      # list id -> scope (newest snapshot wins)
    invalid: dict[int, str] = field(default_factory=dict)        # list id -> why it has no scope
    conflicts: list[str] = field(default_factory=list)          # lists whose scope changed between snapshots
    snapshots: int = 0


def list_snapshot_files(raw_dir: Path) -> list[Path]:
    """Oldest first: names carry a sortable UTC timestamp."""
    return sorted((raw_dir / LISTS_FOLDER).glob(LISTS_PATTERN))


def load_list_scopes(raw_dir: Path) -> ListScopes:
    """Read EVERY snapshot (not only the newest) so a changed owner is noticed."""
    files = list_snapshot_files(raw_dir)
    if not files:
        raise ListSnapshotError(
            f"no lists snapshot in {raw_dir / LISTS_FOLDER}: without it nobody's "
            "purchases can be assigned to a household. Run the extraction first."
        )

    result = ListScopes(snapshots=len(files))
    seen: dict[int, tuple[Scope, str]] = {}        # list id -> (first scope, file where it appeared)
    for path in files:
        with open(path, newline="", encoding=LISTS_ENCODING) as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or tuple(reader.fieldnames) != LIST_COLUMNS:
                raise ListSnapshotError(
                    f"{path.name}: expected columns {list(LIST_COLUMNS)}, got {reader.fieldnames}")
            for row in reader:
                try:
                    list_id = int(row["id"])
                    scope = scope_of_row(row["owner_user_id"], row["household_id"])
                except ValueError as exc:
                    try:
                        bad_id = int(row["id"])
                    except (TypeError, ValueError):
                        raise ListSnapshotError(f"{path.name}: unreadable row {row}") from exc
                    result.invalid[bad_id] = str(exc)
                    continue
                if list_id in seen and seen[list_id][0] != scope:
                    result.conflicts.append(
                        f"list {list_id}: {seen[list_id][0].key} in {seen[list_id][1]}, "
                        f"{scope.key} in {path.name}")
                seen.setdefault(list_id, (scope, path.name))
                result.by_list[list_id] = scope         # later snapshots overwrite earlier ones
                result.invalid.pop(list_id, None)
    return result

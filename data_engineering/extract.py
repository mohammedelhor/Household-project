"""Incremental extraction of the application's data into raw CSV files.

This is the "E" of ETL: copy data out of the application database, change
NOTHING about it, and keep it exactly as it was. Cleaning comes in a later
step. Keeping raw data untouched means we can always redo the cleaning if we
find a mistake in it.

WHAT IT DOES
------------
- item_events (append-only, grows forever): only NEW rows are fetched each
  run, using "the highest id I already extracted" as a bookmark (watermark).
- products (small, rows can change): a full snapshot is written each run.
- shopping_lists (small): a snapshot of (id, owner_user_id, household_id),
  written only when it changed. It says WHOSE list every event belongs to
  (see scopes.py). It is taken AFTER the events on purpose: lists are never
  deleted, so any list that has an event is already in a later snapshot.

THREE DECISIONS WORTH UNDERSTANDING
-----------------------------------
1. SAFETY LAG. A row's `id` is handed out when the insert STARTS, but the row
   only becomes visible when its transaction COMMITS. Row 10 can become
   visible after row 11. If we extracted 11 and saved "last id = 11", row 10
   would be lost forever. So we only take rows older than a few minutes
   (by then every transaction has finished) and we stop at the first row that
   is too recent, so there is never a gap.

2. FILE NAMES CARRY THE ID RANGE (item_events_000000001_000000253.csv).
   Batch files are never edited. If the program dies after writing a file but
   before saving the bookmark, the next run fetches the SAME range and writes
   the SAME file name with the same content. No duplicates, nothing to clean.

3. ATOMIC WRITES. Files are written under a temporary name and renamed when
   complete, so a crash never leaves a half-written file that looks valid.

4. RECONCILIATION (the safety net under decision 1). The safety lag is only
   an ASSUMPTION: it holds if no transaction stays open longer than the lag.
   If one does, a row can commit AFTER we moved the bookmark past its id, and
   the "id > bookmark" query would never see it. So after every extraction we
   compare what the source holds up to the bookmark with the ids we have in
   our files. Rows that arrived late are fetched into a separate "late" batch
   file; nothing is lost and nothing is silently assumed.
   (Preventing the cause is the database's job: a timeout on idle
   transactions, so the assumption is actually enforced.)

COUPLING NOTE
-------------
This file imports the table definitions from backend/app/db/models.py. That
keeps column names and types in ONE place (if the backend renames a column,
the extraction fails loudly instead of silently reading the wrong thing). The
price is that the two layers are linked. We may separate them later.

KNOWN WEAKNESS OF CSV (on purpose, we will see it and then decide)
------------------------------------------------------------------
CSV stores everything as text: no types, and NULL and an empty string look
the same. We will watch what this costs us before moving to Parquet.

USAGE (from the project root)
-----------------------------
    uv run python -m data_engineering.extract
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import hashlib
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine

from backend.app.db.models import ItemEvent, Product, ShoppingList
from data_engineering.scopes import LIST_COLUMNS, LISTS_FOLDER

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "raw"

EVENT_COLUMNS = (
    "id", "item_id", "product_id", "list_id", "event_type",
    "quantity", "unit", "actor_user_id", "occurred_at",
)
PRODUCT_COLUMNS = (
    "id", "name", "normalized_name", "default_unit",
    "category", "created_at", "is_active",
)

STATE_FILE = "_state.json"
MANIFEST_FILE = "_manifest.jsonl"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC and convert aware ones to UTC.

    PostgreSQL returns timezone-aware values; SQLite (used in unit tests)
    returns naive ones. Comparing the two kinds raises an error, so we
    normalise everything first.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_value(value) -> str:
    """Turn one database value into the text stored in the CSV."""
    if value is None:
        return ""  # CSV cannot tell NULL from an empty string: known weakness
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)  # keep the exact digits, never go through float
    return str(value)


def render_csv(columns: tuple[str, ...], rows) -> str:
    """The exact text of a CSV file, built in memory.

    Having this as its own function lets us compare "what we are about to
    write" with "what is already on disk" before touching the disk.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([format_value(row[column]) for column in columns])
    return buffer.getvalue()


def write_csv_atomic(path: Path, columns: tuple[str, ...], rows) -> None:
    """Write a CSV under a temporary name, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")

    # newline="" so the text is written exactly as rendered (no Windows
    # translation of line endings, which would also break comparisons).
    with open(temporary, "w", newline="", encoding="utf-8") as handle:
        handle.write(render_csv(columns, rows))

    os.replace(temporary, path)  # atomic: readers see the old file or the new one


def read_state(out_dir: Path) -> dict:
    path = out_dir / STATE_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(out_dir: Path, state: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / STATE_FILE
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def append_manifest(out_dir: Path, entry: dict) -> None:
    """Keep a log of every batch (what, how many, when). Informational only."""
    with open(out_dir / MANIFEST_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def take_stable_prefix(rows: list, cutoff: datetime) -> list:
    """Keep rows from the start of the list up to (not including) the first
    row that is newer than `cutoff`.

    We stop at the FIRST recent row, not just skip it. If we skipped it and
    kept later rows, our bookmark would jump past the skipped row and it
    would be lost (see "SAFETY LAG" at the top of this file).
    """
    stable = []
    for row in rows:
        if _as_utc(row["occurred_at"]) >= cutoff:
            break
        stable.append(row)
    return stable


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_item_events(
    engine: Engine,
    out_dir: Path,
    *,
    lag_minutes: int = 5,
    max_rows: int = 50_000,
    now: datetime | None = None,
) -> Path | None:
    """Fetch new events and save them as one batch file. Returns its path,
    or None when there was nothing new."""
    now = _as_utc(now) if now else datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=lag_minutes)

    state = read_state(out_dir)
    last_id = state.get("item_events_last_id", 0)

    # A typed query built from the table definitions (not raw SQL text), so
    # every database returns real Python types (datetime, Decimal, bool).
    # With raw text SQLite would hand back strings and 1/0 instead.
    table = ItemEvent.__table__
    query = (
        select(*(table.c[name] for name in EVENT_COLUMNS))
        .where(table.c.id > last_id)
        .order_by(table.c.id)
        .limit(max_rows)
    )
    with engine.connect() as connection:
        fetched = connection.execute(query).mappings().all()

    rows = take_stable_prefix(list(fetched), cutoff)
    if not rows:
        return None

    first_id, final_id = rows[0]["id"], rows[-1]["id"]
    path = out_dir / "item_events" / f"item_events_{first_id:09d}_{final_id:09d}.csv"

    # Order matters. File first, bookmark second. A crash in between means
    # the next run rewrites the same file, which is harmless (decision 2).
    write_csv_atomic(path, EVENT_COLUMNS, rows)
    state["item_events_last_id"] = final_id
    write_state(out_dir, state)
    append_manifest(out_dir, {
        "table": "item_events",
        "file": path.name,
        "rows": len(rows),
        "first_id": first_id,
        "last_id": final_id,
        "extracted_at": now.isoformat(),
    })
    return path


def extract_products_snapshot(
    engine: Engine,
    out_dir: Path,
    *,
    now: datetime | None = None,
) -> Path | None:
    """Save ALL products, but only when they differ from the newest snapshot.

    Products are few and can be edited, so incremental extraction would miss
    changes to rows we already copied: we take the full table each time.
    Writing a new file on every run, though, would pile up identical copies
    forever. So we compare the content with the newest snapshot and write
    only on a real change. Returns the new file, or None if nothing changed.
    """
    now = _as_utc(now) if now else datetime.now(timezone.utc)

    table = Product.__table__
    query = select(*(table.c[name] for name in PRODUCT_COLUMNS)).order_by(table.c.id)
    with engine.connect() as connection:
        rows = connection.execute(query).mappings().all()

    content = render_csv(PRODUCT_COLUMNS, rows)

    # Names contain a sortable UTC timestamp, so the last name is the newest.
    existing = sorted((out_dir / "products").glob("products_*.csv"))
    if existing:
        with open(existing[-1], newline="", encoding="utf-8") as handle:
            if handle.read() == content:
                return None

    path = out_dir / "products" / f"products_{now.strftime('%Y%m%dT%H%M%SZ')}.csv"
    write_csv_atomic(path, PRODUCT_COLUMNS, rows)
    return path


def extract_lists_snapshot(
    engine: Engine,
    out_dir: Path,
    *,
    now: datetime | None = None,
) -> Path | None:
    """Save every list's owner, but only when it differs from the newest
    snapshot. Every snapshot is kept: if a list's owner ever changed, the
    quality gate needs the older ones to notice (see scopes.py)."""
    now = _as_utc(now) if now else datetime.now(timezone.utc)

    table = ShoppingList.__table__
    query = select(*(table.c[name] for name in LIST_COLUMNS)).order_by(table.c.id)
    with engine.connect() as connection:
        rows = connection.execute(query).mappings().all()

    content = render_csv(LIST_COLUMNS, rows)
    existing = sorted((out_dir / LISTS_FOLDER).glob("shopping_lists_*.csv"))
    if existing:
        with open(existing[-1], newline="", encoding="utf-8") as handle:
            if handle.read() == content:
                return None

    path = out_dir / LISTS_FOLDER / f"shopping_lists_{now.strftime('%Y%m%dT%H%M%SZ')}.csv"
    write_csv_atomic(path, LIST_COLUMNS, rows)
    return path


@dataclass(frozen=True)
class Reconciliation:
    recovered: int = 0            # rows that had committed late and were fetched now
    late_file: Path | None = None
    vanished: int = 0             # ids we hold that the source no longer has


def read_extracted_ids(out_dir: Path) -> set[int]:
    """Every event id present in our batch files (duplicates collapse)."""
    ids: set[int] = set()
    for path in (out_dir / "item_events").glob("item_events_*.csv"):
        with open(path, newline="", encoding="utf-8-sig") as handle:   # tolerate a BOM
            ids.update(int(row["id"]) for row in csv.DictReader(handle))
    return ids


def reconcile_item_events(
    engine: Engine,
    out_dir: Path,
    *,
    now: datetime | None = None,
) -> Reconciliation:
    """Find rows with id <= bookmark that exist in the source but not in our
    files, and fetch them. See "RECONCILIATION" at the top of this file.

    We compare the two SETS of ids, never just their sizes: a row that
    arrived late and a row that vanished can cancel out in a count (source
    {1, 3} vs ours {1, 2} both have two ids, yet 3 is missing and 2 is gone).
    Cost: one index-only read of the ids up to the bookmark per run, plus a
    local scan of our own files. Fine at this size; if it ever gets slow,
    optimise by comparing per id range, but never by dropping the id check.
    """
    now = _as_utc(now) if now else datetime.now(timezone.utc)
    watermark = read_state(out_dir).get("item_events_last_id", 0)
    if watermark == 0:
        return Reconciliation()

    table = ItemEvent.__table__
    ours = read_extracted_ids(out_dir)

    with engine.connect() as connection:
        source_ids = set(connection.execute(
            select(table.c.id).where(table.c.id <= watermark)
        ).scalars())

        missing = sorted(source_ids - ours)
        vanished = len(ours - source_ids)

        rows = []
        for start in range(0, len(missing), 1000):     # keep IN lists a sane size
            chunk = missing[start:start + 1000]
            query = (
                select(*(table.c[name] for name in EVENT_COLUMNS))
                .where(table.c.id.in_(chunk))
                .order_by(table.c.id)
            )
            rows += connection.execute(query).mappings().all()

    if not rows:
        return Reconciliation(vanished=vanished)

    # The name carries the id range AND a short hash of the exact id list, so
    # two different late batches can never overwrite each other, while the
    # same batch rewritten after a crash gets the same name (idempotent).
    ids = [row["id"] for row in rows]
    digest = hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()[:8]
    path = out_dir / "item_events" / f"item_events_late_{ids[0]:09d}_{ids[-1]:09d}_{digest}.csv"
    write_csv_atomic(path, EVENT_COLUMNS, rows)
    append_manifest(out_dir, {
        "table": "item_events", "file": path.name, "rows": len(rows),
        "first_id": ids[0], "last_id": ids[-1], "late": True,
        "extracted_at": now.isoformat(),
    })
    return Reconciliation(recovered=len(rows), late_file=path, vanished=vanished)


def run(
    out_dir: Path,
    *,
    engine: Engine,
    lag_minutes: int = 5,
    max_rows: int = 50_000,
    now: datetime | None = None,
) -> int:
    """Both extractions, with a report. Separate from main() so pipeline.py
    can call it without going through the command line."""
    events_path = extract_item_events(
        engine, out_dir, lag_minutes=lag_minutes, max_rows=max_rows, now=now
    )
    reconciliation = reconcile_item_events(engine, out_dir, now=now)
    products_path = extract_products_snapshot(engine, out_dir, now=now)
    lists_path = extract_lists_snapshot(engine, out_dir, now=now)    # AFTER the events

    if reconciliation.recovered:
        # The lag assumption failed once; we healed it, but a person should
        # know, because it means some transaction stayed open too long.
        print(f"WARNING: {reconciliation.recovered} event(s) had committed late "
              f"and were recovered into {reconciliation.late_file.name}. "
              "Check for long-running transactions in the application.")
    if reconciliation.vanished:
        print(f"WARNING: {reconciliation.vanished} event id(s) we extracted earlier "
              "no longer exist in the source. Our copies are kept. The table is "
              "meant to be append-only, so find out who deleted them.")
    if events_path:
        print(f"item_events: wrote {events_path.name}")
    else:
        print("item_events: nothing new")
    if products_path:
        print(f"products:    wrote {products_path.name}")
    else:
        print("products:    unchanged")
    if lists_path:
        print(f"lists:       wrote {lists_path.name}")
    else:
        print("lists:       unchanged")
    return 0


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Extract raw data to CSV files.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--lag-minutes", type=int, default=5,
                        help="ignore rows newer than this (see SAFETY LAG)")
    parser.add_argument("--max-rows", type=int, default=50_000)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SOURCE_DATABASE_URL")
    if not url:
        print("SOURCE_DATABASE_URL is not set. Add it to .env.")
        return 1

    engine = create_engine(url, pool_pre_ping=True)
    try:
        return run(
            args.out_dir, engine=engine,
            lag_minutes=args.lag_minutes, max_rows=args.max_rows,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

"""Tests for the incremental extraction (SQLite, no external services).

Each test protects a decision explained at the top of data_engineering/extract.py.
"""
import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.models import Base, ItemEvent, Product
from data_engineering.extract import (
    extract_item_events,
    extract_products_snapshot,
    take_stable_prefix,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=1)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def add_event(engine, event_id, occurred_at=OLD, event_type="added", product_id=1):
    """Insert one event with an explicit id (SQLite does not enforce FKs)."""
    with Session(engine) as session:
        session.add(
            ItemEvent(
                id=event_id,
                item_id=event_id,
                product_id=product_id,
                list_id=1,
                event_type=event_type,
                quantity=Decimal("2.000"),
                unit="kg",
                actor_user_id=1,
                occurred_at=occurred_at,
            )
        )
        session.commit()


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_first_run_extracts_everything_in_id_order(engine, tmp_path):
    for event_id in (3, 1, 2):  # inserted out of order on purpose
        add_event(engine, event_id)

    path = extract_item_events(engine, tmp_path, now=NOW)

    assert path.name == "item_events_000000001_000000003.csv"
    assert [r["id"] for r in read_rows(path)] == ["1", "2", "3"]
    assert json.loads((tmp_path / "_state.json").read_text())["item_events_last_id"] == 3


def test_values_are_kept_exactly(engine, tmp_path):
    add_event(engine, 1, occurred_at=datetime(2026, 10, 30, 9, 30, 15, tzinfo=timezone.utc))

    (row,) = read_rows(extract_item_events(engine, tmp_path, now=NOW))

    assert row["quantity"] == "2.000"          # exact digits, no float rounding
    assert row["unit"] == "kg"
    assert row["occurred_at"] == "2026-10-30T09:30:15+00:00"


def test_second_run_with_no_new_rows_writes_nothing(engine, tmp_path):
    add_event(engine, 1)
    extract_item_events(engine, tmp_path, now=NOW)

    assert extract_item_events(engine, tmp_path, now=NOW) is None
    assert len(list((tmp_path / "item_events").glob("*.csv"))) == 1


def test_only_new_rows_go_into_the_next_batch(engine, tmp_path):
    add_event(engine, 1)
    add_event(engine, 2)
    extract_item_events(engine, tmp_path, now=NOW)

    add_event(engine, 3)
    add_event(engine, 4)
    second = extract_item_events(engine, tmp_path, now=NOW)

    assert second.name == "item_events_000000003_000000004.csv"
    assert [r["id"] for r in read_rows(second)] == ["3", "4"]


def test_recent_rows_are_left_for_later(engine, tmp_path):
    add_event(engine, 1)
    add_event(engine, 2, occurred_at=NOW - timedelta(minutes=1))  # inside the 5 min lag

    path = extract_item_events(engine, tmp_path, lag_minutes=5, now=NOW)

    assert [r["id"] for r in read_rows(path)] == ["1"]
    assert json.loads((tmp_path / "_state.json").read_text())["item_events_last_id"] == 1

    # Later, once row 2 is old enough, it is picked up.
    later = extract_item_events(engine, tmp_path, lag_minutes=5, now=NOW + timedelta(minutes=10))
    assert [r["id"] for r in read_rows(later)] == ["2"]


def test_a_recent_row_blocks_the_older_rows_after_it_so_no_gap_is_left():
    # Row 2 is recent but row 3 is old. If we skipped 2 and kept 3, the
    # bookmark would jump to 3 and row 2 would be lost forever.
    rows = [
        {"id": 1, "occurred_at": OLD},
        {"id": 2, "occurred_at": NOW},
        {"id": 3, "occurred_at": OLD},
    ]

    stable = take_stable_prefix(rows, NOW - timedelta(minutes=5))

    assert [r["id"] for r in stable] == [1]


def test_rerun_after_a_crash_rewrites_the_same_file_without_duplicates(engine, tmp_path):
    add_event(engine, 1)
    add_event(engine, 2)
    first = extract_item_events(engine, tmp_path, now=NOW)
    original = first.read_text(encoding="utf-8")

    # Simulate a crash between "file written" and "bookmark saved".
    (tmp_path / "_state.json").unlink()

    again = extract_item_events(engine, tmp_path, now=NOW)

    assert again == first
    assert again.read_text(encoding="utf-8") == original
    assert len(list((tmp_path / "item_events").glob("*.csv"))) == 1


def test_max_rows_splits_work_across_runs_without_losing_rows(engine, tmp_path):
    for event_id in range(1, 6):
        add_event(engine, event_id)

    ids = []
    while (path := extract_item_events(engine, tmp_path, max_rows=2, now=NOW)):
        ids += [r["id"] for r in read_rows(path)]

    assert ids == ["1", "2", "3", "4", "5"]


def test_no_temporary_files_are_left_behind(engine, tmp_path):
    add_event(engine, 1)
    extract_item_events(engine, tmp_path, now=NOW)

    assert list(tmp_path.rglob("*.tmp")) == []


def test_manifest_records_each_batch(engine, tmp_path):
    add_event(engine, 1)
    add_event(engine, 2)
    extract_item_events(engine, tmp_path, now=NOW)

    (line,) = (tmp_path / "_manifest.jsonl").read_text().splitlines()
    entry = json.loads(line)

    assert (entry["rows"], entry["first_id"], entry["last_id"]) == (2, 1, 2)


def test_products_snapshot_contains_every_product(engine, tmp_path):
    with Session(engine) as session:
        session.add_all([
            Product(id=1, name="Rice", normalized_name="rice", default_unit="kg", category="grains"),
            Product(id=2, name="Milk", normalized_name="milk", default_unit=None, category=None),
        ])
        session.commit()

    path = extract_products_snapshot(engine, tmp_path, now=NOW)
    rows = read_rows(path)

    assert path.name == "products_20261031T120000Z.csv"
    assert [r["name"] for r in rows] == ["Rice", "Milk"]
    assert rows[1]["default_unit"] == ""    # NULL becomes empty text: CSV's weakness
    assert rows[0]["is_active"] == "true"


def _add_product(engine, product_id, name):
    with Session(engine) as session:
        session.add(Product(id=product_id, name=name, normalized_name=name.lower()))
        session.commit()


def test_unchanged_products_do_not_create_a_new_snapshot(engine, tmp_path):
    _add_product(engine, 1, "Rice")
    extract_products_snapshot(engine, tmp_path, now=NOW)

    again = extract_products_snapshot(engine, tmp_path, now=NOW + timedelta(hours=1))

    assert again is None
    assert len(list((tmp_path / "products").glob("*.csv"))) == 1


def test_a_changed_product_table_creates_a_new_snapshot(engine, tmp_path):
    _add_product(engine, 1, "Rice")
    extract_products_snapshot(engine, tmp_path, now=NOW)
    _add_product(engine, 2, "Milk")

    later = extract_products_snapshot(engine, tmp_path, now=NOW + timedelta(hours=1))

    assert later is not None
    assert [r["name"] for r in read_rows(later)] == ["Rice", "Milk"]
    assert len(list((tmp_path / "products").glob("*.csv"))) == 2


def test_an_edit_to_an_existing_product_is_captured(engine, tmp_path):
    # The reason we snapshot the whole table: edits to old rows must not be missed.
    _add_product(engine, 1, "Rice")
    extract_products_snapshot(engine, tmp_path, now=NOW)
    with Session(engine) as session:
        session.get(Product, 1).category = "grains"
        session.commit()

    later = extract_products_snapshot(engine, tmp_path, now=NOW + timedelta(hours=1))

    assert read_rows(later)[0]["category"] == "grains"
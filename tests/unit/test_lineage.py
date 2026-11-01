"""Tests for lineage: the input fingerprint and the manifest helpers."""
import json

from data_engineering.extract import EVENT_COLUMNS, PRODUCT_COLUMNS, write_csv_atomic
from data_engineering.lineage import (
    describe_inputs,
    git_state,
    read_previous_purchase_count,
)


def event_row(event_id):
    return {"id": event_id, "item_id": 1, "product_id": 1, "list_id": 1,
            "event_type": "added", "quantity": "2", "unit": "kg", "actor_user_id": 1,
            "occurred_at": "2026-10-01T10:00:00+00:00"}


def make_raw(raw_dir, ids=(1,)):
    write_csv_atomic(raw_dir / "item_events" / "item_events_000000001_000000001.csv",
                     EVENT_COLUMNS, [event_row(i) for i in ids])
    write_csv_atomic(raw_dir / "products" / "products_20261031T000000Z.csv", PRODUCT_COLUMNS, [])


def test_same_inputs_give_the_same_fingerprint(tmp_path):
    make_raw(tmp_path)

    assert describe_inputs(tmp_path)["fingerprint"] == describe_inputs(tmp_path)["fingerprint"]


def test_any_change_in_a_raw_file_changes_the_fingerprint(tmp_path):
    make_raw(tmp_path, ids=(1,))
    before = describe_inputs(tmp_path)["fingerprint"]

    make_raw(tmp_path, ids=(1, 2))   # same file names, different content

    assert describe_inputs(tmp_path)["fingerprint"] != before


def test_inputs_list_every_file_with_its_hash_and_extraction_time(tmp_path):
    make_raw(tmp_path)
    (tmp_path / "_manifest.jsonl").write_text(
        json.dumps({"file": "item_events_000000001_000000001.csv",
                    "extracted_at": "2026-10-31T10:00:00+00:00"}) + "\n"
        + json.dumps({"file": "item_events_000000001_000000001.csv",
                      "extracted_at": "2026-11-01T10:00:00+00:00"}) + "\n",   # a re-run
        encoding="utf-8",
    )

    inputs = describe_inputs(tmp_path)

    (event_file,) = inputs["event_files"]
    assert len(event_file["sha256"]) == 64
    assert event_file["extracted_at"] == "2026-10-31T10:00:00+00:00"   # the FIRST time
    assert inputs["products_snapshot"]["name"] == "products_20261031T000000Z.csv"


def test_git_state_never_raises(tmp_path):
    state = git_state(tmp_path)   # not a repository: must degrade, not crash

    assert state["commit"] == "unknown"


def test_previous_count_is_none_without_a_published_run(tmp_path):
    assert read_previous_purchase_count(tmp_path) is None


def test_a_resaved_products_snapshot_with_the_same_rows_is_the_same_input(tmp_path):
    make_raw(tmp_path)
    before = describe_inputs(tmp_path)["fingerprint"]

    # Same content, newer name: for example the extraction wrote a copy.
    source = tmp_path / "products" / "products_20261031T000000Z.csv"
    (tmp_path / "products" / "products_20261105T000000Z.csv").write_bytes(source.read_bytes())

    assert describe_inputs(tmp_path)["fingerprint"] == before
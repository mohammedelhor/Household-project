"""Tests for scopes: whose purchase history is this?

The acceptance idea (from the design review): the same product bought at very
different rhythms by two households must never be merged, in ANY layer.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.models import Base, ShoppingList
from data_engineering import transform
from data_engineering.extract import (EVENT_COLUMNS, extract_item_events,
                                      extract_lists_snapshot, run as extract_run,
                                      write_csv_atomic)
from data_engineering.lineage import describe_inputs
from data_engineering.pipeline import run_pipeline
from data_engineering.scopes import (LIST_COLUMNS, ListSnapshotError, Scope,
                                     load_list_scopes, scope_of_row)
from data_engineering.synthetic_data import (TWO_HOUSEHOLDS, build_ground_truth,
                                             generate_households, write_to_database)
from data_engineering.transform import (Event, build_purchases, compute_intervals,
                                        latest_clean_dir, summarise_by_scope)

END = datetime(2026, 10, 31, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


# ------------------------------ the Scope value -------------------------------

def test_a_user_and_a_household_with_the_same_number_are_different():
    assert Scope("user", 3) != Scope("household", 3)
    assert Scope("user", 3).key == "user:3" and Scope.parse("household:3") == Scope("household", 3)


def test_unknown_scope_types_are_rejected():
    with pytest.raises(ValueError):
        Scope("family", 1)


@pytest.mark.parametrize("owner, household, expected", [
    ("7", "", Scope("user", 7)),
    ("", "3", Scope("household", 3)),
])
def test_a_row_with_exactly_one_owner_has_a_scope(owner, household, expected):
    assert scope_of_row(owner, household) == expected


@pytest.mark.parametrize("owner, household", [("", ""), ("7", "3"), (None, None)])
def test_a_row_without_exactly_one_owner_is_refused(owner, household):
    with pytest.raises(ValueError, match="exactly one owner"):
        scope_of_row(owner, household)


# ------------------------------ reading snapshots -----------------------------

def snapshot(raw, rows, name="shopping_lists_20261001T000000Z.csv"):
    write_csv_atomic(raw / "shopping_lists" / name, LIST_COLUMNS, rows)


def test_without_a_snapshot_nothing_can_be_scoped(tmp_path):
    with pytest.raises(ListSnapshotError, match="Run the extraction"):
        load_list_scopes(tmp_path)


def test_a_snapshot_maps_every_list_to_its_scope(tmp_path):
    snapshot(tmp_path, [{"id": 1, "owner_user_id": None, "household_id": 4},
                        {"id": 2, "owner_user_id": 9, "household_id": None}])

    scopes = load_list_scopes(tmp_path)

    assert scopes.by_list == {1: Scope("household", 4), 2: Scope("user", 9)}
    assert scopes.invalid == {} and scopes.conflicts == []


def test_a_list_with_two_owners_is_invalid_not_guessed(tmp_path):
    snapshot(tmp_path, [{"id": 1, "owner_user_id": 9, "household_id": 4}])

    scopes = load_list_scopes(tmp_path)

    assert 1 not in scopes.by_list and "exactly one owner" in scopes.invalid[1]


def test_wrong_columns_stop_the_run(tmp_path):
    path = tmp_path / "shopping_lists" / "shopping_lists_20261001T000000Z.csv"
    path.parent.mkdir(parents=True)
    path.write_text("id,owner\r\n1,2\r\n", encoding="utf-8")

    with pytest.raises(ListSnapshotError, match="expected columns"):
        load_list_scopes(tmp_path)


def test_a_file_with_a_byte_order_mark_is_read(tmp_path):
    path = tmp_path / "shopping_lists" / "shopping_lists_20261001T000000Z.csv"
    path.parent.mkdir(parents=True)
    path.write_text("id,owner_user_id,household_id\r\n1,,4\r\n", encoding="utf-8-sig")

    assert load_list_scopes(tmp_path).by_list == {1: Scope("household", 4)}


def test_a_list_that_changes_owner_between_snapshots_is_reported(tmp_path):
    snapshot(tmp_path, [{"id": 1, "owner_user_id": None, "household_id": 4}],
             "shopping_lists_20261001T000000Z.csv")
    snapshot(tmp_path, [{"id": 1, "owner_user_id": 9, "household_id": None}],
             "shopping_lists_20261015T000000Z.csv")

    scopes = load_list_scopes(tmp_path)

    assert len(scopes.conflicts) == 1 and "household:4" in scopes.conflicts[0]
    assert "user:9" in scopes.conflicts[0]


# ------------------------------ extraction ------------------------------------

@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_the_lists_snapshot_is_written_only_when_it_changed(engine, tmp_path):
    with Session(engine) as session:
        session.add(ShoppingList(id=1, household_id=4, name="A"))
        session.commit()

    first = extract_lists_snapshot(engine, tmp_path, now=NOW)
    assert first is not None
    assert extract_lists_snapshot(engine, tmp_path, now=NOW + timedelta(hours=1)) is None

    with Session(engine) as session:
        session.add(ShoppingList(id=2, owner_user_id=9, name="B"))
        session.commit()
    assert extract_lists_snapshot(engine, tmp_path, now=NOW + timedelta(hours=2)) is not None
    assert len(list((tmp_path / "shopping_lists").glob("*.csv"))) == 2   # history is kept


def test_the_snapshot_holds_ownership_only(engine, tmp_path):
    with Session(engine) as session:
        session.add(ShoppingList(id=1, household_id=4, name="Secret list name"))
        session.commit()

    path = extract_lists_snapshot(engine, tmp_path, now=NOW)

    assert path.read_text(encoding="utf-8").splitlines()[0] == "id,owner_user_id,household_id"
    assert "Secret" not in path.read_text(encoding="utf-8")


def test_extraction_takes_the_lists_snapshot_after_the_events(engine, tmp_path, monkeypatch):
    order = []
    import data_engineering.extract as extract
    for name in ("extract_item_events", "reconcile_item_events",
                 "extract_products_snapshot", "extract_lists_snapshot"):
        original = getattr(extract, name)
        monkeypatch.setattr(extract, name, (lambda o, n: lambda *a, **k: (order.append(n), o(*a, **k))[1])(original, name))

    extract_run(tmp_path, engine=engine, now=NOW)

    assert order.index("extract_lists_snapshot") > order.index("extract_item_events")


# ------------------------------ cleaning --------------------------------------

def ev(event_id, kind, item_id, list_id, minutes=0):
    return Event(id=event_id, item_id=item_id, product_id=1, list_id=list_id, event_type=kind,
                 quantity=Decimal("2"), unit="kg", actor_user_id=1,
                 occurred_at=T0 + timedelta(minutes=minutes))


def test_a_purchase_gets_the_scope_of_its_list_not_of_its_product():
    events = [ev(1, "added", 1, 1), ev(2, "purchased", 1, 1),
              ev(3, "added", 2, 2), ev(4, "purchased", 2, 2, minutes=5)]
    scopes = {1: Scope("household", 1), 2: Scope("household", 2)}

    purchases, quarantined = build_purchases(events, None, scopes)

    assert quarantined == []
    assert {p.item_id: p.scope for p in purchases} == {1: Scope("household", 1), 2: Scope("household", 2)}


def test_scope_resolution_follows_the_list_when_an_item_moves_to_another_owner():
    """Same product, same events: only the owner of the list changed."""
    events = [ev(1, "added", 1, 1), ev(2, "purchased", 1, 1)]

    before, _ = build_purchases(events, None, {1: Scope("household", 1)})
    after, _ = build_purchases(events, None, {1: Scope("household", 2)})

    assert before[0].scope == Scope("household", 1)
    assert after[0].scope == Scope("household", 2)           # not tied to the product


def test_an_item_on_a_list_without_an_owner_is_rejected_not_given_a_default_scope():
    events = [ev(1, "added", 1, 99), ev(2, "purchased", 1, 99)]

    purchases, quarantined = build_purchases(events, None, {1: Scope("household", 1)})

    assert purchases == []
    assert {q.item_id for q in quarantined} == {1}
    assert "has no owner" in quarantined[0].reason


def test_intervals_are_never_computed_across_scopes():
    names = {1: ("Milk", "milk")}
    events = []
    for household, days in ((1, (0, 7, 14)), (2, (1, 31))):
        for n, day in enumerate(days):
            item = household * 100 + n
            events += [ev(item * 10, "added", item, household, minutes=day * 1440),
                       ev(item * 10 + 1, "purchased", item, household, minutes=day * 1440 + 1)]
    scopes = {1: Scope("household", 1), 2: Scope("household", 2)}

    purchases, _ = build_purchases(events, None, scopes)
    rows = compute_intervals(purchases, names)

    assert [(r["scope_id"], r["days_since_previous"]) for r in rows] == [(1, 7), (1, 7), (2, 30)]


def test_intervals_need_a_scope_on_every_purchase():
    events = [ev(1, "added", 1, 1), ev(2, "purchased", 1, 1),
              ev(3, "added", 2, 1, minutes=2000), ev(4, "purchased", 2, 1, minutes=2001)]
    purchases, _ = build_purchases(events)                    # no scopes given

    with pytest.raises(ValueError, match="no scope"):
        compute_intervals(purchases, {1: ("Milk", "milk")})


# -------------------- the acceptance scenario, end to end ---------------------

@pytest.fixture
def two_households(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    items = generate_households(TWO_HOUSEHOLDS, end=END, days=180, seed=42, cancel_rate=0.2)
    with Session(engine) as session:
        keys = write_to_database(session, items, {s.key: s.name for s in TWO_HOUSEHOLDS})
    truth = build_ground_truth(items, end=END, days=180, seed=42, scope_keys=keys)
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(truth), encoding="utf-8")
    yield engine, items, keys, truth, truth_path
    engine.dispose()


def test_the_pipeline_reproduces_the_answer_key_for_both_households(two_households, tmp_path, capsys):
    engine, _, keys, truth, truth_path = two_households

    code = run_pipeline(engine=engine, raw_dir=tmp_path / "raw", clean_dir=tmp_path / "clean",
                        truth_path=truth_path, now=NOW, data_origin="synthetic")

    out = capsys.readouterr().out
    assert code == 0, out
    assert set(keys.values()) == set(truth["scopes"])
    assert all(f"OK   {scope}" in out for scope in truth["scopes"])
    manifest = json.loads((latest_clean_dir(tmp_path / "clean") / "manifest.json").read_text())
    assert manifest["data_origin"] == "synthetic"


def test_the_published_intervals_keep_the_households_apart(two_households, tmp_path):
    engine, _, keys, _, _ = two_households
    run_pipeline(engine=engine, raw_dir=tmp_path / "raw", clean_dir=tmp_path / "clean", now=NOW)

    import csv
    with open(latest_clean_dir(tmp_path / "clean") / "purchase_intervals.csv", newline="",
              encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    a, b = (int(keys[k].split(":")[1]) for k in ("family_a", "family_b"))
    milk = {s: [int(r["days_since_previous"]) for r in rows
                if r["product_name"] == "Milk" and int(r["scope_id"]) == s] for s in (a, b)}

    assert sum(milk[a]) / len(milk[a]) < 9          # about every 7 days
    assert sum(milk[b]) / len(milk[b]) > 25         # about every 30 days
    assert {r["scope_type"] for r in rows} == {"household"}


def test_cancelled_purchases_never_become_purchases(two_households, tmp_path):
    engine, items, _, _, _ = two_households
    run_pipeline(engine=engine, raw_dir=tmp_path / "raw", clean_dir=tmp_path / "clean", now=NOW)

    import csv
    with open(latest_clean_dir(tmp_path / "clean") / "purchases.csv", newline="",
              encoding="utf-8") as handle:
        published = list(csv.DictReader(handle))

    assert sum(1 for i in items if i.cancelled) > 0
    assert len(published) == sum(1 for i in items if not i.cancelled)


def test_a_run_without_a_lists_snapshot_stops_and_publishes_nothing(two_households, tmp_path, capsys):
    engine, *_ = two_households
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    run_pipeline(engine=engine, raw_dir=raw, clean_dir=clean, now=NOW)
    before = latest_clean_dir(clean).name
    for path in (raw / "shopping_lists").glob("*.csv"):
        path.unlink()

    code = transform.run(raw, clean, now=NOW)

    assert code == 1 and "lists snapshot" in capsys.readouterr().out
    assert latest_clean_dir(clean).name == before


def test_a_list_missing_from_the_snapshot_fails_the_gate_and_keeps_the_previous_run(
        two_households, tmp_path, capsys):
    engine, *_ = two_households
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    run_pipeline(engine=engine, raw_dir=raw, clean_dir=clean, now=NOW)
    before = latest_clean_dir(clean).name
    for path in (raw / "shopping_lists").glob("*.csv"):       # a snapshot that lacks one list
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\r\n".join(lines[:-1]) + "\r\n", encoding="utf-8")

    code = transform.run(raw, clean, now=NOW)

    out = capsys.readouterr().out
    assert code == 1 and "REJECTED" in out
    assert "every list used by an event has an owner in the lists snapshot" in out
    assert latest_clean_dir(clean).name == before


def test_a_changed_owner_fails_the_gate(two_households, tmp_path, capsys):
    engine, *_ = two_households
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    run_pipeline(engine=engine, raw_dir=raw, clean_dir=clean, now=NOW)
    before = latest_clean_dir(clean).name
    newest = sorted((raw / "shopping_lists").glob("*.csv"))[-1]
    lines = newest.read_text(encoding="utf-8").splitlines()
    first = lines[1].split(",")                                # swap the owner of list 1
    lines[1] = f"{first[0]},77,"
    write_csv_atomic(raw / "shopping_lists" / "shopping_lists_29991231T000000Z.csv",
                     LIST_COLUMNS, [dict(zip(LIST_COLUMNS, row.split(","))) for row in lines[1:]])

    code = transform.run(raw, clean, now=NOW)

    assert code == 1
    assert "no list changed owner between lists snapshots" in capsys.readouterr().out
    assert latest_clean_dir(clean).name == before


def test_the_lists_snapshots_are_part_of_the_input_identity(two_households, tmp_path):
    engine, *_ = two_households
    raw = tmp_path / "raw"
    run_pipeline(engine=engine, raw_dir=raw, clean_dir=tmp_path / "clean", now=NOW)
    before = describe_inputs(raw)["fingerprint"]

    write_csv_atomic(raw / "shopping_lists" / "shopping_lists_29991231T000000Z.csv", LIST_COLUMNS,
                     [{"id": 1, "owner_user_id": None, "household_id": 1}])

    assert describe_inputs(raw)["fingerprint"] != before


def test_the_scopes_of_the_published_purchases_match_the_summary(two_households, tmp_path):
    engine, _, keys, _, _ = two_households
    raw = tmp_path / "raw"
    run_pipeline(engine=engine, raw_dir=raw, clean_dir=tmp_path / "clean", now=NOW)
    events, _, _ = transform.load_events(raw)
    purchases, _ = build_purchases(events, None, load_list_scopes(raw).by_list)
    names = transform.load_product_names(raw)

    summary = summarise_by_scope(purchases, compute_intervals(purchases, names), names)

    assert set(summary) == set(keys.values())

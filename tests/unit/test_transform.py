"""Tests for the cleaning step. No database needed.

Two kinds of tests:
 - small ones, one per rule, so a failure names the rule that broke;
 - end-to-end ones that feed the generator's data through the whole pipeline
   and require it to reproduce the generator's answer key exactly.
"""
import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data_engineering import transform
from data_engineering.extract import EVENT_COLUMNS, PRODUCT_COLUMNS, write_csv_atomic
from data_engineering.synthetic_data import PRODUCT_PROFILES, build_ground_truth, generate_history
from data_engineering.transform import (
    Event,
    Purchase,
    build_purchases,
    compare_with_truth,
    compute_intervals,
    evaluate_item,
    load_events,
    load_product_names,
    parse_event,
    summarise_by_product,
    validate_event,
)

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def ev(event_id, kind, minutes=0, item_id=1, product_id=1, quantity="2", unit="kg"):
    return Event(
        id=event_id, item_id=item_id, product_id=product_id, list_id=1,
        event_type=kind, quantity=Decimal(quantity), unit=unit, actor_user_id=1,
        occurred_at=T0 + timedelta(minutes=minutes),
    )


def row(**overrides):
    base = {
        "id": "1", "item_id": "1", "product_id": "1", "list_id": "1",
        "event_type": "added", "quantity": "2.000", "unit": "kg",
        "actor_user_id": "1", "occurred_at": "2026-10-01T10:00:00+00:00",
    }
    base.update(overrides)
    return base


# ----------------------------- parsing ---------------------------------

def test_parse_event_converts_text_to_real_types():
    event = parse_event(row())

    assert event.id == 1 and event.quantity == Decimal("2.000")
    assert event.occurred_at == T0


def test_parse_event_empty_optional_fields_become_none():
    event = parse_event(row(product_id="", actor_user_id=""))

    assert event.product_id is None and event.actor_user_id is None


@pytest.mark.parametrize("bad", [
    {"quantity": "abc"},
    {"id": "x"},
    {"occurred_at": "yesterday"},
    {"occurred_at": "2026-10-01T10:00:00"},   # no time zone
])
def test_parse_event_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        parse_event(row(**bad))


# ------------------------- single-event rules --------------------------

@pytest.mark.parametrize("kwargs, fragment", [
    ({"kind": "eaten"}, "unknown event_type"),
    ({"kind": "added", "quantity": "0"}, "quantity not positive"),
    ({"kind": "added", "quantity": "-1"}, "quantity not positive"),
    ({"kind": "added", "quantity": "NaN"}, "quantity not positive"),
    ({"kind": "added", "unit": "cup"}, "unsupported unit"),
])
def test_validate_event_reports_the_broken_rule(kwargs, fragment):
    assert fragment in validate_event(ev(1, **kwargs))


def test_validate_event_accepts_a_good_event():
    assert validate_event(ev(1, "added")) is None


# ------------------------- replaying an item ---------------------------

def test_simple_purchase():
    purchase, problem = evaluate_item([ev(1, "added"), ev(2, "purchased", 60)])

    assert problem is None
    assert purchase.purchased_at == T0 + timedelta(minutes=60)


def test_mistaken_tap_uses_the_second_purchase():
    purchase, problem = evaluate_item([
        ev(1, "added"), ev(2, "purchased", 60),
        ev(3, "reverted", 65), ev(4, "purchased", 70),
    ])

    assert problem is None
    assert purchase.purchased_at == T0 + timedelta(minutes=70)


def test_reverted_and_never_rebought_is_not_a_purchase():
    purchase, problem = evaluate_item([
        ev(1, "added"), ev(2, "purchased", 60), ev(3, "reverted", 65),
    ])

    assert (purchase, problem) == (None, None)


def test_never_bought_is_not_a_purchase():
    assert evaluate_item([ev(1, "added")]) == (None, None)
    assert evaluate_item([ev(1, "added"), ev(2, "archived", 5)]) == (None, None)


def test_archiving_after_purchase_keeps_the_purchase():
    purchase, problem = evaluate_item([
        ev(1, "added"), ev(2, "purchased", 60), ev(3, "archived", 90),
    ])

    assert problem is None and purchase is not None


@pytest.mark.parametrize("events, fragment", [
    ([ev(1, "purchased")], "starts with 'purchased'"),
    ([ev(1, "added"), ev(2, "added", 1)], "'added' appears twice"),
    ([ev(1, "added"), ev(2, "purchased", 1), ev(3, "purchased", 2)], "already purchased"),
    ([ev(1, "added"), ev(2, "reverted", 1)], "'reverted' while not purchased"),
    ([ev(1, "added"), ev(2, "archived", 1), ev(3, "purchased", 2)], "after 'archived'"),
])
def test_inconsistent_histories_are_rejected_with_a_reason(events, fragment):
    purchase, problem = evaluate_item(events)

    assert purchase is None and fragment in problem


# ----------------------- grouping and quarantine -----------------------

def test_events_are_ordered_by_time_not_by_file_order():
    # Rows arrive scrambled; the history must still be replayed in time order.
    events = [ev(3, "purchased", 60), ev(1, "added", 0)]

    purchases, quarantined = build_purchases(events)

    assert len(purchases) == 1 and quarantined == []


def test_same_timestamp_is_ordered_by_id():
    events = [ev(2, "purchased", 0), ev(1, "added", 0)]

    purchases, quarantined = build_purchases(events)

    assert len(purchases) == 1 and quarantined == []


def test_one_bad_event_quarantines_the_whole_item_but_not_others():
    events = [
        ev(1, "added", item_id=1), ev(2, "purchased", 5, item_id=1, unit="cup"),
        ev(3, "added", item_id=2), ev(4, "purchased", 5, item_id=2),
    ]

    purchases, quarantined = build_purchases(events)

    assert [p.item_id for p in purchases] == [2]
    assert {q.event_id for q in quarantined} == {1, 2}
    assert all("unsupported unit" in q.reason for q in quarantined)


def test_unparseable_rows_taint_their_item():
    events = [ev(1, "added"), ev(2, "purchased", 5)]

    purchases, quarantined = build_purchases(events, tainted_items={1: "unparseable row: x"})

    assert purchases == [] and len(quarantined) == 2


# ------------------------------ intervals ------------------------------

def purchase(day, product_id=1):
    return Purchase(item_id=day, product_id=product_id, list_id=1, quantity=Decimal(1),
                    unit="kg", purchased_at=datetime(2026, 10, day, 10, tzinfo=timezone.utc))


def test_intervals_are_whole_days_between_purchase_dates():
    names = {1: ("Rice", "rice")}

    rows = compute_intervals([purchase(1), purchase(15), purchase(29)], names)

    assert [r["days_since_previous"] for r in rows] == [14, 14]


def test_intervals_ignore_hours_and_do_not_mix_products():
    names = {1: ("Rice", "rice"), 2: ("Milk", "milk")}
    late = Purchase(item_id=9, product_id=1, list_id=1, quantity=Decimal(1), unit="kg",
                    purchased_at=datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc))
    early = Purchase(item_id=8, product_id=1, list_id=1, quantity=Decimal(1), unit="kg",
                     purchased_at=datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc))

    rows = compute_intervals([early, late, purchase(5, product_id=2)], names)

    assert [(r["product_name"], r["days_since_previous"]) for r in rows] == [("Rice", 2)]


def test_custom_items_without_a_product_have_no_intervals():
    assert compute_intervals([purchase(1, None), purchase(8, None)], {}) == []


# ---------------------- end to end vs the answer key -------------------

END = datetime(2026, 10, 31, tzinfo=timezone.utc)


def write_raw_files(raw_dir, items):
    """Write the generator's history the way extract.py would have."""
    product_ids = {p.name: i for i, p in enumerate(PRODUCT_PROFILES, start=1)}
    rows = []
    for item_id, item in enumerate(items, start=1):
        for event in item.events:
            rows.append({
                "item_id": item_id, "product_id": product_ids[item.profile.name],
                "list_id": 1, "event_type": event.event_type,
                "quantity": item.profile.quantity, "unit": item.profile.unit,
                "actor_user_id": 1, "occurred_at": event.occurred_at,
            })
    rows.sort(key=lambda r: r["occurred_at"])     # ids grow with time, like the real table
    for event_id, r in enumerate(rows, start=1):
        r["id"] = event_id

    write_csv_atomic(
        raw_dir / "item_events" / f"item_events_{1:09d}_{len(rows):09d}.csv",
        EVENT_COLUMNS, rows,
    )
    write_csv_atomic(
        raw_dir / "products" / "products_20261031T000000Z.csv",
        PRODUCT_COLUMNS,
        [
            {"id": i, "name": p.name, "normalized_name": p.name.casefold(),
             "default_unit": p.unit, "category": p.category,
             "created_at": END, "is_active": True}
            for i, p in enumerate(PRODUCT_PROFILES, start=1)
        ],
    )


@pytest.mark.parametrize("mistake_rate", [0.0, 0.05, 1.0])
def test_pipeline_reproduces_the_generators_answer_key(tmp_path, mistake_rate):
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42,
                             mistake_rate=mistake_rate)
    write_raw_files(tmp_path, items)

    events, unparsed, _ = load_events(tmp_path)
    purchases, quarantined = build_purchases(events)
    names = load_product_names(tmp_path)
    summary = summarise_by_product(purchases, compute_intervals(purchases, names), names)
    truth = build_ground_truth(items, PRODUCT_PROFILES, end=END, days=180, seed=42)

    assert unparsed == [] and quarantined == []
    results = compare_with_truth(summary, truth)
    assert all(r["ok"] for r in results), results


def test_pipeline_notices_when_the_data_is_wrong(tmp_path):
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42)
    write_raw_files(tmp_path, items)
    truth = build_ground_truth(items, PRODUCT_PROFILES, end=END, days=180, seed=42)

    # Remove one of the rice purchases from the raw file: the comparison must fail.
    path = next((tmp_path / "item_events").glob("*.csv"))
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rice_purchase = next(r for r in rows if r["product_id"] == "1" and r["event_type"] == "purchased")
    rows = [r for r in rows if r["item_id"] != rice_purchase["item_id"]]
    write_csv_atomic(path, EVENT_COLUMNS, rows)

    events, _, _ = load_events(tmp_path)
    purchases, _ = build_purchases(events)
    names = load_product_names(tmp_path)
    summary = summarise_by_product(purchases, compute_intervals(purchases, names), names)

    results = {r["product"]: r for r in compare_with_truth(summary, truth)}
    assert results["Rice"]["ok"] is False
    assert results["Milk"]["ok"] is True


def test_duplicate_ids_across_files_are_counted_once(tmp_path):
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42)
    write_raw_files(tmp_path, items)
    original = next((tmp_path / "item_events").glob("*.csv"))
    (tmp_path / "item_events" / "item_events_999999998_999999999.csv").write_text(
        original.read_text(encoding="utf-8"), encoding="utf-8"
    )

    events, _, stats = load_events(tmp_path)

    assert stats["duplicates"] == len(events)


def test_command_line_writes_files_and_checks_the_answer_key(tmp_path, monkeypatch, capsys):
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42)
    write_raw_files(raw, items)
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(
        build_ground_truth(items, PRODUCT_PROFILES, end=END, days=180, seed=42)
    ))

    monkeypatch.setattr("sys.argv", [
        "transform", "--raw-dir", str(raw), "--out-dir", str(clean), "--truth", str(truth_path),
    ])
    code = transform.main()

    assert code == 0
    assert "FAIL" not in capsys.readouterr().out
    for name in ("purchases.csv", "purchase_intervals.csv", "quarantine.csv"):
        assert (clean / name).exists()


def test_command_line_without_raw_files_explains_what_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["transform", "--raw-dir", str(tmp_path / "none")])

    assert transform.main() == 1
    assert "Run the extraction first" in capsys.readouterr().out
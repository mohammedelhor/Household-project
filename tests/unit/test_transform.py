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
from data_engineering.scopes import LIST_COLUMNS, Scope, load_list_scopes
from data_engineering.synthetic_data import PRODUCT_PROFILES, build_ground_truth, generate_history
from data_engineering.transform import (
    Event,
    Purchase,
    build_purchases,
    check_item_consistency,
    latest_clean_dir,
    publish_run,
    compare_with_truth,
    compute_intervals,
    evaluate_item,
    load_events,
    load_product_names,
    parse_event,
    summarise_by_scope,
    validate_event,
)

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

HOUSEHOLD_1 = Scope("household", 1)      # the scope every test list belongs to by default


def write_lists_snapshot(raw_dir, rows=None, name="shopping_lists_20261031T000000Z.csv"):
    """The lists snapshot extract.py writes: list 1 belongs to household 1 unless told otherwise."""
    rows = rows if rows is not None else [{"id": 1, "owner_user_id": None, "household_id": 1}]
    write_csv_atomic(raw_dir / "shopping_lists" / name, LIST_COLUMNS, rows)


def ev(event_id, kind, minutes=0, item_id=1, product_id=1, quantity="2", unit="kg",
       list_id=1):
    return Event(
        id=event_id, item_id=item_id, product_id=product_id, list_id=list_id,
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


# ---------------------- cross-event consistency ------------------------

@pytest.mark.parametrize("changed, field", [
    ({"product_id": 15}, "product_id (1 -> 15)"),
    ({"quantity": "5"}, "quantity (2 -> 5)"),
    ({"unit": "g"}, "unit (kg -> g)"),
    ({"list_id": 9}, "list_id (1 -> 9)"),
])
def test_an_item_whose_events_disagree_is_quarantined(changed, field):
    # Each event is valid alone and the state sequence is fine.
    events = [ev(1, "added"), ev(2, "purchased", 5, **changed), ev(3, "archived", 9)]

    purchases, quarantined = build_purchases(events)

    assert purchases == []
    assert {q.event_id for q in quarantined} == {1, 2, 3}
    assert all(field in q.reason for q in quarantined)


def test_the_reason_lists_every_field_that_disagrees():
    events = [ev(1, "added", product_id=10, quantity="2"),
              ev(2, "purchased", 5, product_id=15, quantity="5")]

    reason = check_item_consistency(events)

    assert "product_id (10 -> 15)" in reason and "quantity (2 -> 5)" in reason


def test_same_value_written_differently_is_not_a_disagreement():
    # 2 and 2.000 are the same quantity once parsed as numbers.
    events = [ev(1, "added", quantity="2"), ev(2, "purchased", 5, quantity="2.000")]

    assert check_item_consistency(events) is None


def test_null_product_on_every_event_is_consistent():
    # Free-text items have no product_id at all; that is constant, not a clash.
    events = [ev(1, "added", product_id=None), ev(2, "purchased", 5, product_id=None)]

    assert check_item_consistency(events) is None


def test_an_inconsistent_item_does_not_affect_other_items():
    events = [
        ev(1, "added", item_id=1), ev(2, "purchased", 5, item_id=1, quantity="9"),
        ev(3, "added", item_id=2), ev(4, "purchased", 5, item_id=2),
    ]

    purchases, quarantined = build_purchases(events)

    assert [p.item_id for p in purchases] == [2]
    assert {q.event_id for q in quarantined} == {1, 2}


def test_unparseable_rows_taint_their_item():
    events = [ev(1, "added"), ev(2, "purchased", 5)]

    purchases, quarantined = build_purchases(events, tainted_items={1: "unparseable row: x"})

    assert purchases == [] and len(quarantined) == 2


# ------------------------------ intervals ------------------------------

def purchase(day, product_id=1, scope=HOUSEHOLD_1):
    return Purchase(item_id=day, product_id=product_id, list_id=1, quantity=Decimal(1),
                    unit="kg", purchased_at=datetime(2026, 10, day, 10, tzinfo=timezone.utc),
                    source_event_id=day, scope=scope)


def test_intervals_are_whole_days_between_purchase_dates():
    names = {1: ("Rice", "rice")}

    rows = compute_intervals([purchase(1), purchase(15), purchase(29)], names)

    assert [r["days_since_previous"] for r in rows] == [14, 14]


def test_intervals_ignore_hours_and_do_not_mix_products():
    names = {1: ("Rice", "rice"), 2: ("Milk", "milk")}
    late = Purchase(item_id=9, product_id=1, list_id=1, quantity=Decimal(1), unit="kg",
                    purchased_at=datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc),
                    source_event_id=9, scope=HOUSEHOLD_1)
    early = Purchase(item_id=8, product_id=1, list_id=1, quantity=Decimal(1), unit="kg",
                     purchased_at=datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc),
                     source_event_id=8, scope=HOUSEHOLD_1)

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
    write_lists_snapshot(raw_dir)
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
    purchases, quarantined = build_purchases(events, None, load_list_scopes(tmp_path).by_list)
    names = load_product_names(tmp_path)
    summary = summarise_by_scope(purchases, compute_intervals(purchases, names), names)
    truth = build_ground_truth(items, end=END, days=180, seed=42,
                               scope_keys={None: HOUSEHOLD_1.key})

    assert unparsed == [] and quarantined == []
    results = compare_with_truth(summary, truth)
    assert all(r["ok"] for r in results), results


def test_pipeline_notices_when_the_data_is_wrong(tmp_path):
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42)
    write_raw_files(tmp_path, items)
    truth = build_ground_truth(items, end=END, days=180, seed=42,
                               scope_keys={None: HOUSEHOLD_1.key})

    # Remove one of the rice purchases from the raw file: the comparison must fail.
    path = next((tmp_path / "item_events").glob("*.csv"))
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rice_purchase = next(r for r in rows if r["product_id"] == "1" and r["event_type"] == "purchased")
    rows = [r for r in rows if r["item_id"] != rice_purchase["item_id"]]
    write_csv_atomic(path, EVENT_COLUMNS, rows)

    events, _, _ = load_events(tmp_path)
    purchases, _ = build_purchases(events, None, load_list_scopes(tmp_path).by_list)
    names = load_product_names(tmp_path)
    summary = summarise_by_scope(purchases, compute_intervals(purchases, names), names)

    results = {r["product"]: r for r in compare_with_truth(summary, truth)}
    assert results["household:1 Rice"]["ok"] is False
    assert results["household:1 Milk"]["ok"] is True


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
        build_ground_truth(items, end=END, days=180, seed=42, scope_keys={None: HOUSEHOLD_1.key})
    ))

    monkeypatch.setattr("sys.argv", [
        "transform", "--raw-dir", str(raw), "--out-dir", str(clean), "--truth", str(truth_path),
    ])
    code = transform.main()

    assert code == 0
    assert "FAIL" not in capsys.readouterr().out
    for name in ("purchases.csv", "purchase_intervals.csv", "quarantine.csv"):
        assert (latest_clean_dir(clean) / name).exists()


def test_command_line_without_raw_files_explains_what_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["transform", "--raw-dir", str(tmp_path / "none")])

    assert transform.main() == 1
    assert "Run the extraction first" in capsys.readouterr().out


def test_late_batch_files_are_read_like_any_other_batch(tmp_path):
    _write_batch(tmp_path, "item_events_000000001_000000003.csv",
                 [_raw_row(1), _raw_row(3, kind="archived")])
    _write_batch(tmp_path, "item_events_late_000000002_000000002_ab12cd34.csv",
                 [_raw_row(2, kind="purchased")])

    events, _, stats = load_events(tmp_path)

    assert sorted(e.id for e in events) == [1, 2, 3] and stats["files"] == 2


# ----------------------- conflicting duplicate ids ---------------------

def _write_batch(raw_dir, name, rows):
    write_csv_atomic(raw_dir / "item_events" / name, EVENT_COLUMNS, rows)
    if not (raw_dir / "shopping_lists").exists():
        write_lists_snapshot(raw_dir)


def _raw_row(event_id, item_id=1, kind="added", product_id=1, quantity="2"):
    return {"id": event_id, "item_id": item_id, "product_id": product_id, "list_id": 1,
            "event_type": kind, "quantity": quantity, "unit": "kg", "actor_user_id": 1,
            "occurred_at": "2026-10-01T10:00:00+00:00"}


def test_same_id_with_different_content_is_a_conflict_not_a_silent_choice(tmp_path):
    _write_batch(tmp_path, "item_events_000000001_000000002.csv",
                 [_raw_row(1), _raw_row(2, kind="purchased")])
    _write_batch(tmp_path, "item_events_000000003_000000004.csv",
                 [_raw_row(2, kind="purchased", quantity="9")])   # id 2 again, different

    events, unparsed, stats = load_events(tmp_path)

    assert stats["conflicts"] == 1 and stats["duplicates"] == 0
    assert [e.id for e in events] == [1]                  # neither copy of id 2 is kept
    assert unparsed[0].event_id == 2 and "differ in: quantity" in unparsed[0].reason


def test_a_conflict_quarantines_the_whole_item_and_fails_the_run(tmp_path, capsys):
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    _write_batch(raw, "item_events_000000001_000000002.csv",
                 [_raw_row(1), _raw_row(2, kind="purchased")])
    _write_batch(raw, "item_events_000000003_000000003.csv",
                 [_raw_row(2, kind="purchased", quantity="9")])
    write_csv_atomic(raw / "products" / "products_20261031T000000Z.csv", PRODUCT_COLUMNS, [])

    code = transform.run(raw, clean)

    assert code == 1
    assert "DIFFERENT content" in capsys.readouterr().out
    # Here most events end up rejected, so the quality gate FAILS: the run is
    # kept for diagnosis, but it is NOT published.
    with pytest.raises(FileNotFoundError):
        latest_clean_dir(clean)
    (rejected,) = (clean / "runs").iterdir()
    quarantine = (rejected / "quarantine.csv").read_text(encoding="utf-8")
    assert "conflicting copies of event id 2" in quarantine
    purchases = (rejected / "purchases.csv").read_text(encoding="utf-8")
    assert len(purchases.strip().splitlines()) == 1       # header only: no purchase from item 1


def test_conflict_between_copies_that_claim_different_items_taints_both(tmp_path):
    _write_batch(tmp_path, "item_events_000000001_000000001.csv", [_raw_row(1, item_id=1)])
    _write_batch(tmp_path, "item_events_000000002_000000002.csv", [_raw_row(1, item_id=2)])

    _, unparsed, _ = load_events(tmp_path)

    assert sorted(q.item_id for q in unparsed) == [1, 2]


# ------------------------ all-or-nothing publishing --------------------

TABLES = {"purchases.csv": "a\r\n1\r\n", "purchase_intervals.csv": "b\r\n",
          "quarantine.csv": "c\r\n"}


def test_published_run_is_complete_and_found_through_the_pointer(tmp_path):
    run_id = publish_run(tmp_path, TABLES)

    folder = latest_clean_dir(tmp_path)

    assert folder.name == run_id
    assert {p.name for p in folder.iterdir()} == set(TABLES)
    assert (folder / "purchases.csv").read_bytes() == b"a\r\n1\r\n"


def test_same_content_gives_the_same_run_and_creates_nothing_new(tmp_path):
    first = publish_run(tmp_path, TABLES)
    second = publish_run(tmp_path, TABLES)

    assert first == second
    assert len(list((tmp_path / "runs").iterdir())) == 1


def test_a_crash_before_the_commit_leaves_the_previous_run_untouched(tmp_path, monkeypatch):
    old = publish_run(tmp_path, TABLES)
    real_replace = transform.os.replace

    def crash_at_the_pointer(source, destination):
        if str(destination).endswith("_latest.json"):
            raise OSError("simulated crash")
        return real_replace(source, destination)

    monkeypatch.setattr(transform.os, "replace", crash_at_the_pointer)
    with pytest.raises(OSError):
        publish_run(tmp_path, {**TABLES, "purchases.csv": "different\r\n"})
    monkeypatch.undo()

    folder = latest_clean_dir(tmp_path)
    assert folder.name == old                                   # readers still see the old run
    assert (folder / "purchases.csv").read_bytes() == b"a\r\n1\r\n"


def test_leftover_staging_folders_are_removed_on_the_next_run(tmp_path):
    (tmp_path / "runs" / ".tmp-deadbeef").mkdir(parents=True)

    publish_run(tmp_path, TABLES)

    assert not (tmp_path / "runs" / ".tmp-deadbeef").exists()


def test_old_runs_are_pruned_but_the_current_one_stays(tmp_path):
    ids = [publish_run(tmp_path, {**TABLES, "quarantine.csv": f"{n}\r\n"}) for n in range(8)]

    remaining = {p.name for p in (tmp_path / "runs").iterdir()}

    assert len(remaining) == transform.KEEP_RUNS
    assert ids[-1] in remaining and latest_clean_dir(tmp_path).exists()


def test_reading_before_any_run_explains_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match="Run the pipeline first"):
        latest_clean_dir(tmp_path)


# ------------------- gate, lineage and publishing together --------------

NOW_AFTER = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _prepared_raw(tmp_path):
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42)
    write_raw_files(raw, items)
    return raw, clean


def test_a_passing_run_is_published_with_its_manifest_and_report(tmp_path):
    raw, clean = _prepared_raw(tmp_path)

    assert transform.run(raw, clean, now=NOW_AFTER) == 0

    folder = latest_clean_dir(clean)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((folder / "quality_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert manifest["run_id"] == folder.name
    assert manifest["contract_version"] and manifest["transform_version"]
    assert manifest["counts"]["purchases"] == 90
    assert manifest["inputs"]["event_files"][0]["sha256"]
    assert any(rule.startswith("R5") for rule in manifest["rules"])


def test_every_purchase_leads_back_to_its_source_event(tmp_path):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)

    with open(latest_clean_dir(clean) / "purchases.csv", newline="", encoding="utf-8") as handle:
        purchases = list(csv.DictReader(handle))
    events = {str(e.id): e for e in load_events(raw)[0]}

    assert len(purchases) == 90
    for row in purchases:
        source = events[row["source_event_id"]]
        assert source.event_type == "purchased"
        assert str(source.item_id) == row["item_id"]


def test_a_failing_gate_keeps_the_previous_published_run(tmp_path, capsys):
    raw, clean = _prepared_raw(tmp_path)
    assert transform.run(raw, clean, now=NOW_AFTER) == 0
    published = latest_clean_dir(clean).name

    # Break the data: a NEWER products snapshot without Rice. Every Rice
    # purchase now points at a product that does not exist.
    write_csv_atomic(
        raw / "products" / "products_20261101T000000Z.csv", PRODUCT_COLUMNS,
        [{"id": i, "name": p.name, "normalized_name": p.name.casefold(),
          "default_unit": p.unit, "category": p.category, "created_at": END, "is_active": True}
         for i, p in enumerate(PRODUCT_PROFILES, start=1) if p.name != "Rice"],
    )
    capsys.readouterr()

    code = transform.run(raw, clean, now=NOW_AFTER)

    out = capsys.readouterr().out
    assert code == 1
    assert "quality gate: FAIL" in out and "REJECTED run" in out
    assert latest_clean_dir(clean).name == published              # readers still get the good run
    rejected = [d for d in (clean / "runs").iterdir() if d.name != published]
    assert len(rejected) == 1                                      # kept for diagnosis
    report = json.loads((rejected[0] / "quality_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"


def test_same_inputs_keep_the_run_id_and_new_inputs_change_it(tmp_path):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    first = latest_clean_dir(clean).name

    transform.run(raw, clean, now=NOW_AFTER)
    assert latest_clean_dir(clean).name == first

    # A new raw batch with an event that does not create any purchase: the
    # outputs may stay identical, but the INPUT changed, so it is a new run.
    write_csv_atomic(
        raw / "item_events" / "item_events_000999999_000999999.csv", EVENT_COLUMNS,
        [{"id": 999999, "item_id": 999999, "product_id": 1, "list_id": 1,
          "event_type": "added", "quantity": "1", "unit": "kg", "actor_user_id": 1,
          "occurred_at": "2026-10-30T10:00:00+00:00"}],
    )
    transform.run(raw, clean, now=NOW_AFTER)
    assert latest_clean_dir(clean).name != first


# ------------- files written by other tools (BOM, wrong columns) -------------

def _products_rows(skip=None):
    return [{"id": i, "name": p.name, "normalized_name": p.name.casefold(),
             "default_unit": p.unit, "category": p.category, "created_at": END, "is_active": True}
            for i, p in enumerate(PRODUCT_PROFILES, start=1) if p.name != skip]


def _write_with_bom(path, columns, rows):
    """What PowerShell 5's `Set-Content -Encoding utf8` or Excel produce."""
    from data_engineering.extract import render_csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(render_csv(columns, rows))


def test_a_file_with_a_byte_order_mark_is_read_correctly(tmp_path):
    raw, clean = _prepared_raw(tmp_path)
    snapshot = next((raw / "products").glob("*.csv"))
    _write_with_bom(snapshot, PRODUCT_COLUMNS, _products_rows())

    assert transform.run(raw, clean, now=NOW_AFTER) == 0
    assert load_product_names(raw)[1] == ("Rice", "rice")     # no "﻿id" column trouble


def test_a_bom_snapshot_missing_a_product_is_rejected_by_the_gate_not_by_a_crash(tmp_path, capsys):
    # The exact experiment from the hands-on test on Windows.
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    published = latest_clean_dir(clean).name
    _write_with_bom(raw / "products" / "products_29991231T000000Z.csv",
                    PRODUCT_COLUMNS, _products_rows(skip="Rice"))
    capsys.readouterr()

    code = transform.run(raw, clean, now=NOW_AFTER)

    assert code == 1 and "quality gate: FAIL" in capsys.readouterr().out
    assert latest_clean_dir(clean).name == published


@pytest.mark.parametrize("folder, name", [
    ("products", "products_29991231T000000Z.csv"),
    ("item_events", "item_events_000999999_000999999.csv"),
])
def test_a_file_with_the_wrong_columns_stops_the_run_with_a_clear_message(tmp_path, capsys, folder, name):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    published = latest_clean_dir(clean).name
    (raw / folder / name).write_text("identifier,label\r\n1,x\r\n", encoding="utf-8")
    capsys.readouterr()

    code = transform.run(raw, clean, now=NOW_AFTER)     # must not raise

    out = capsys.readouterr().out
    assert code == 1 and "RAW DATA ERROR" in out and name in out
    assert latest_clean_dir(clean).name == published


# ------------------- run identity includes the logic --------------------

def test_changed_logic_creates_a_new_run_even_when_the_numbers_are_identical(tmp_path, monkeypatch):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    first = latest_clean_dir(clean)
    first_manifest = (first / "manifest.json").read_text(encoding="utf-8")

    monkeypatch.setattr(transform, "TRANSFORM_VERSION", "9.9.9")   # a rule changed, output did not
    transform.run(raw, clean, now=NOW_AFTER)
    second = latest_clean_dir(clean)

    assert second.name != first.name
    assert (second / "purchases.csv").read_bytes() == (first / "purchases.csv").read_bytes()
    assert json.loads((second / "manifest.json").read_text(encoding="utf-8"))["transform_version"] == "9.9.9"
    assert (first / "manifest.json").read_text(encoding="utf-8") == first_manifest   # old run untouched


def test_a_change_in_the_source_code_changes_the_run_identity(tmp_path, monkeypatch):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    first = latest_clean_dir(clean).name

    edited = tmp_path / "transform_copy.py"
    edited.write_text("# a different version of the logic\n", encoding="utf-8")
    monkeypatch.setattr(transform, "CODE_FILES", (edited,))
    transform.run(raw, clean, now=NOW_AFTER)

    assert latest_clean_dir(clean).name != first


def test_same_logic_and_inputs_reuse_the_run_and_its_manifest(tmp_path):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    folder = latest_clean_dir(clean)
    manifest = (folder / "manifest.json").read_text(encoding="utf-8")

    transform.run(raw, clean, now=NOW_AFTER + timedelta(days=1))

    assert latest_clean_dir(clean) == folder
    assert (folder / "manifest.json").read_text(encoding="utf-8") == manifest


def test_every_execution_is_logged_with_its_code_state(tmp_path):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    transform.run(raw, clean, now=NOW_AFTER + timedelta(days=1))   # reuses the folder

    lines = [json.loads(line) for line in
             (clean / "publish_log.jsonl").read_text(encoding="utf-8").splitlines()]

    assert [e["published"] for e in lines] == [True, True]
    assert lines[0]["run_id"] == lines[1]["run_id"]
    assert lines[0]["at"] != lines[1]["at"]
    assert {"commit", "uncommitted_changes", "source_fingerprint"} <= set(lines[0]["code"])


def test_a_rejected_execution_is_logged_as_not_published(tmp_path):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)
    write_csv_atomic(raw / "products" / "products_29991231T000000Z.csv", PRODUCT_COLUMNS,
                     _products_rows(skip="Rice"))

    transform.run(raw, clean, now=NOW_AFTER)

    last = json.loads((clean / "publish_log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["gate"] == "FAIL" and last["published"] is False


# ---------- every file that affects the pipeline's decisions is in the identity ----------

import ast
import importlib.util
import shutil
from pathlib import Path


def _copies_of_the_code(tmp_path):
    """Copy every identity file somewhere writable so a test can edit one."""
    folder = tmp_path / "code"
    folder.mkdir()
    copies = []
    for original in transform.CODE_FILES:
        copy = folder / original.name
        shutil.copyfile(original, copy)
        copies.append(copy)
    return tuple(copies)


def test_the_identity_covers_the_five_files_that_decide_the_pipeline():
    assert {p.name for p in transform.CODE_FILES} == {
        "transform.py", "contracts.py", "quality_gate.py", "units.py", "scopes.py"}


@pytest.mark.parametrize("changed", ["transform.py", "contracts.py", "quality_gate.py", "units.py", "scopes.py"])
def test_changing_any_identity_file_changes_the_logic_identity(tmp_path, monkeypatch, changed):
    copies = _copies_of_the_code(tmp_path)
    monkeypatch.setattr(transform, "CODE_FILES", copies)
    before = transform.logic_identity()

    target = next(p for p in copies if p.name == changed)
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    assert transform.logic_identity() != before


@pytest.mark.parametrize("changed", ["quality_gate.py", "units.py"])
def test_editing_gate_or_units_creates_a_new_run_with_identical_numbers(tmp_path, monkeypatch, changed):
    # The case from the review: the rules of the gate or the supported units
    # change, the numbers do not, and an old run folder must NOT be reused.
    raw, clean = _prepared_raw(tmp_path)
    copies = _copies_of_the_code(tmp_path)
    monkeypatch.setattr(transform, "CODE_FILES", copies)
    transform.run(raw, clean, now=NOW_AFTER)
    first = latest_clean_dir(clean)

    target = next(p for p in copies if p.name == changed)
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    transform.run(raw, clean, now=NOW_AFTER)
    second = latest_clean_dir(clean)

    assert second.name != first.name
    assert (second / "purchases.csv").read_bytes() == (first / "purchases.csv").read_bytes()
    recorded = json.loads((second / "manifest.json").read_text(encoding="utf-8"))["code"]["source_files"]
    old = json.loads((first / "manifest.json").read_text(encoding="utf-8"))["code"]["source_files"]
    assert [name for name in recorded if recorded[name] != old[name]] == [changed]   # says WHICH file


def test_the_manifest_records_a_hash_for_every_identity_file(tmp_path):
    raw, clean = _prepared_raw(tmp_path)
    transform.run(raw, clean, now=NOW_AFTER)

    code = json.loads((latest_clean_dir(clean) / "manifest.json").read_text(encoding="utf-8"))["code"]

    assert set(code["source_files"]) == {"transform.py", "contracts.py", "quality_gate.py", "units.py", "scopes.py"}
    assert all(len(h) == 16 for h in code["source_files"].values())


def test_no_project_module_imported_by_the_logic_is_forgotten():
    # If this fails, the logic started importing a project file that is neither
    # part of the identity (CODE_FILES) nor explicitly excluded with a reason
    # (NOT_PART_OF_IDENTITY): a change in it would silently reuse an old run.
    covered = {p.resolve() for p in transform.CODE_FILES}
    unaccounted = []
    for source in transform.CODE_FILES:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if not name.startswith(("data_engineering", "backend")):
                    continue
                origin = Path(importlib.util.find_spec(name).origin).resolve()
                if origin not in covered and origin.name not in transform.NOT_PART_OF_IDENTITY:
                    unaccounted.append(f"{source.name} imports {name}")

    assert unaccounted == []

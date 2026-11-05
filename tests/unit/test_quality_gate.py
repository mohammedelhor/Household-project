"""Tests for the quality gate: every critical check must be able to FAIL."""
from datetime import datetime, timedelta, timezone

import pytest

from data_engineering.contracts import CONTRACTS
from data_engineering.extract import render_csv
from data_engineering.quality_gate import run_gate
from data_engineering.scopes import Scope

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
PURCHASE_COLUMNS = CONTRACTS["purchases.csv"].column_names
INTERVAL_COLUMNS = CONTRACTS["purchase_intervals.csv"].column_names
FAMILY_A = Scope("household", 1)


def purchase(item_id, day, **overrides):
    row = {"item_id": item_id, "product_id": 1, "product_name": "Rice", "list_id": 1,
           "scope_type": "household", "scope_id": 1, "quantity": "2.000", "unit": "kg",
           "purchased_at": f"2026-10-{day:02d}T10:00:00+00:00",
           "purchase_date": f"2026-10-{day:02d}", "source_event_id": 100 + item_id}
    row.update(overrides)
    return row


def interval(first_day, second_day):
    return {"scope_type": "household", "scope_id": 1, "product_id": 1, "product_name": "Rice",
            "previous_purchase_date": f"2026-10-{first_day:02d}",
            "purchase_date": f"2026-10-{second_day:02d}",
            "days_since_previous": second_day - first_day}


def tables(purchases=None, intervals=None, quarantine=()):
    purchases = [purchase(1, 1), purchase(2, 15)] if purchases is None else purchases
    intervals = [interval(1, 15)] if intervals is None else intervals
    return {
        "purchases.csv": render_csv(PURCHASE_COLUMNS, purchases),
        "purchase_intervals.csv": render_csv(INTERVAL_COLUMNS, intervals),
        "quarantine.csv": render_csv(("event_id", "item_id", "reason"), quarantine),
    }


def gate(tables_=None, **overrides):
    arguments = dict(known_product_ids={1}, events_read=100, quarantined_events=0,
                     newest_event_at=NOW - timedelta(days=1), previous_purchases=None, now=NOW,
                     list_scopes={1: FAMILY_A, 2: Scope("household", 2)},
                     unresolved_list_ids=set(), scope_conflicts=[])
    arguments.update(overrides)
    return run_gate(tables_ if tables_ is not None else tables(), **arguments)


def failed_names(report):
    return [c.name for c in report.failures]


def test_good_data_passes_with_no_warnings():
    report = gate()

    assert report.status == "PASS" and report.passed
    assert report.failures == [] and report.warnings == []


# ------------------------------ critical checks ------------------------------

@pytest.mark.parametrize("bad_purchase, fragment", [
    (purchase(2, 15, quantity="-1"), "contract: purchases.csv"),
    (purchase(2, 15, unit="cup"), "contract: purchases.csv"),
    (purchase(2, 15, purchased_at="2026-10-15T10:00:00"), "contract: purchases.csv"),
    (purchase(1, 15), "contract: purchases.csv"),                      # item_id repeats
])
def test_contract_violations_fail_the_gate(bad_purchase, fragment):
    report = gate(tables([purchase(1, 1), bad_purchase]))

    assert report.status == "FAIL" and fragment in failed_names(report)


def test_when_a_file_breaks_its_contract_no_other_check_pretends_to_run():
    report = gate(tables([purchase(1, 1, quantity="-1")]))

    assert all(c.name.startswith("contract:") for c in report.checks)


def test_date_that_disagrees_with_the_timestamp_fails():
    report = gate(tables([purchase(1, 1), purchase(2, 15, purchase_date="2026-10-16")],
                         [interval(1, 16)]))

    assert "purchase_date matches purchased_at (UTC)" in failed_names(report)


def test_a_purchase_of_an_unknown_product_fails():
    report = gate(known_product_ids={2})

    assert "every purchase's product exists in the products snapshot" in failed_names(report)


def test_intervals_that_do_not_follow_from_the_purchases_fail():
    wrong = {**interval(1, 15), "days_since_previous": 3, "purchase_date": "2026-10-04"}

    report = gate(tables(intervals=[wrong]))

    assert "every interval matches the one derived from the purchases" in failed_names(report)


# -- the example from the review: purchases on Oct 1, Oct 2 and Oct 5 --------

def _three_purchases():
    return [purchase(1, 1), purchase(2, 2), purchase(3, 5)]


def _interval_row(first, second, days):
    return {"scope_type": "household", "scope_id": 1, "product_id": 1, "product_name": "Rice",
            "previous_purchase_date": f"2026-10-{first:02d}",
            "purchase_date": f"2026-10-{second:02d}", "days_since_previous": days}


def test_correct_intervals_of_the_review_example_pass():
    correct = [_interval_row(1, 2, 1), _interval_row(2, 5, 3)]

    assert gate(tables(_three_purchases(), correct)).status == "PASS"


@pytest.mark.parametrize("wrong", [
    # right dates, wrong days (1 and 3 became 2 and 2)
    [_interval_row(1, 2, 2), _interval_row(2, 5, 2)],
    # days that agree with the rows' own dates, but the dates are not the purchases'
    [_interval_row(1, 3, 2), _interval_row(3, 5, 2)],
])
def test_wrong_intervals_are_rejected_even_when_count_and_total_days_are_right(wrong):
    # The weaker checks of the first version would have accepted these: two
    # intervals (the right count) adding up to 4 days (the span Oct 1 -> Oct 5).
    assert len(wrong) == 2 and sum(r["days_since_previous"] for r in wrong) == 4

    report = gate(tables(_three_purchases(), wrong))

    assert report.status == "FAIL"
    assert failed_names(report) == ["every interval matches the one derived from the purchases"]
    assert "interval #" in report.failures[0].detail


def test_intervals_in_the_wrong_order_are_rejected():
    swapped = [_interval_row(2, 5, 3), _interval_row(1, 2, 1)]

    assert gate(tables(_three_purchases(), swapped)).status == "FAIL"


def test_an_extra_interval_is_rejected():
    extra = [_interval_row(1, 2, 1), _interval_row(2, 5, 3), _interval_row(5, 9, 4)]

    assert gate(tables(_three_purchases(), extra)).status == "FAIL"


def test_an_interval_for_a_product_without_purchases_is_rejected():
    stray = {**interval(1, 15), "product_id": 2}

    assert gate(tables(intervals=[interval(1, 15), stray]), known_product_ids={1, 2}).status == "FAIL"


def test_the_gate_derives_intervals_per_product_and_in_time_order():
    from data_engineering.quality_gate import derive_intervals
    rows = [purchase(1, 9), purchase(2, 1), purchase(3, 4, product_id=2, product_name="Milk"),
            purchase(4, 20, product_id=2, product_name="Milk")]
    rows = [{**r, "product_id": str(r["product_id"]), "scope_id": str(r["scope_id"])}
            for r in rows]   # as read from CSV

    assert derive_intervals(rows) == {
        ("household", 1, 1): [("2026-10-01", "2026-10-09", 8)],
        ("household", 1, 2): [("2026-10-04", "2026-10-20", 16)],
    }


def test_missing_intervals_fail():
    report = gate(tables(intervals=[]))

    assert "every interval matches the one derived from the purchases" in failed_names(report)


def test_a_purchase_in_the_future_fails():
    report = gate(tables([purchase(1, 1), purchase(2, 15)]), now=datetime(2026, 10, 10, tzinfo=timezone.utc))

    assert "no purchase is in the future" in failed_names(report)


def test_a_clock_a_few_hours_ahead_is_tolerated():
    report = gate(now=datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc))   # purchase at 10:00 same day

    assert "no purchase is in the future" not in failed_names(report)


def test_most_events_rejected_fails_but_a_few_only_warn():
    assert "less than 50% of events rejected" in failed_names(gate(quarantined_events=60))

    few = gate(quarantined_events=10)
    assert few.status == "PASS"
    assert [c.name for c in few.warnings] == ["at most 5% of events rejected"]


# ------------------------------- warnings only -------------------------------

def test_no_purchases_is_a_warning_not_a_failure():
    report = gate(tables([], []))

    assert report.status == "PASS"
    assert "there is at least one purchase" in [c.name for c in report.warnings]


def test_shrinking_purchases_warn():
    report = gate(previous_purchases=5)

    assert report.status == "PASS"
    assert "purchases did not shrink since the previous run" in [c.name for c in report.warnings]


def test_stale_data_warns():
    report = gate(newest_event_at=NOW - timedelta(days=30))

    assert report.status == "PASS"
    assert "newest event is recent" in [c.name for c in report.warnings]


def test_report_is_serialisable_and_says_why():
    report = gate(quarantined_events=60)

    data = report.to_dict()

    assert data["status"] == "FAIL" and data["critical_failures"] == 1
    assert any("60.0%" in c["detail"] for c in data["checks"] if not c["passed"])


# ------------------------------ scopes ---------------------------------------

def household_purchase(item_id, day, household, list_id):
    return purchase(item_id, day, scope_id=household, list_id=list_id)


def household_interval(household, first_day, second_day):
    return {**interval(first_day, second_day), "scope_id": household}


def test_two_households_buying_the_same_product_never_share_an_interval():
    """The acceptance case at the gate: Family A every 7 days, Family B every 30."""
    purchases = [household_purchase(1, 1, 1, 1), household_purchase(2, 8, 1, 1),
                 household_purchase(3, 2, 2, 2), household_purchase(4, 30, 2, 2)]
    separate = [household_interval(1, 1, 8), household_interval(2, 2, 30)]

    assert gate(tables(purchases, separate)).passed


def test_an_interval_that_mixes_two_households_is_rejected():
    purchases = [household_purchase(1, 1, 1, 1), household_purchase(2, 8, 1, 1),
                 household_purchase(3, 2, 2, 2), household_purchase(4, 30, 2, 2)]
    mixed = [household_interval(1, 1, 2), household_interval(1, 2, 8), household_interval(1, 8, 30)]

    assert "every interval matches the one derived from the purchases" in failed_names(
        gate(tables(purchases, mixed)))


def test_a_list_without_an_owner_fails_the_gate():
    report = gate(unresolved_list_ids={7})

    assert report.status == "FAIL"
    assert "every list used by an event has an owner in the lists snapshot" in failed_names(report)


def test_a_list_that_changed_owner_between_snapshots_fails_the_gate():
    report = gate(scope_conflicts=["list 1: household:1 in a.csv, user:9 in b.csv"])

    assert "no list changed owner between lists snapshots" in failed_names(report)


def test_a_purchase_whose_scope_differs_from_its_list_fails_the_gate():
    wrong = purchase(2, 15, scope_id=2)               # list 1 belongs to household 1, not 2

    report = gate(tables([purchase(1, 1), wrong], [interval(1, 15)]))

    assert "every purchase carries the scope of its list" in failed_names(report)


def test_a_user_and_a_household_with_the_same_number_are_different_scopes():
    purchases = [purchase(1, 1, scope_type="user", scope_id=1, list_id=3),
                 purchase(2, 15, list_id=1)]
    # one purchase each: no interval exists, because the scopes differ
    report = gate(tables(purchases, []), list_scopes={1: FAMILY_A, 3: Scope("user", 1)})

    assert report.passed

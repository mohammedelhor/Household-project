"""Tests for the quality gate: every critical check must be able to FAIL."""
from datetime import datetime, timedelta, timezone

import pytest

from data_engineering.extract import render_csv
from data_engineering.quality_gate import run_gate

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
PURCHASE_COLUMNS = ("item_id", "product_id", "product_name", "list_id", "quantity",
                    "unit", "purchased_at", "purchase_date", "source_event_id")
INTERVAL_COLUMNS = ("product_id", "product_name", "previous_purchase_date",
                    "purchase_date", "days_since_previous")


def purchase(item_id, day, **overrides):
    row = {"item_id": item_id, "product_id": 1, "product_name": "Rice", "list_id": 1,
           "quantity": "2.000", "unit": "kg",
           "purchased_at": f"2026-10-{day:02d}T10:00:00+00:00",
           "purchase_date": f"2026-10-{day:02d}", "source_event_id": 100 + item_id}
    row.update(overrides)
    return row


def interval(first_day, second_day):
    return {"product_id": 1, "product_name": "Rice",
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
                     newest_event_at=NOW - timedelta(days=1), previous_purchases=None, now=NOW)
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

    assert "intervals can be derived from the purchases" in failed_names(report)


def test_missing_intervals_fail():
    report = gate(tables(intervals=[]))

    assert "intervals can be derived from the purchases" in failed_names(report)


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
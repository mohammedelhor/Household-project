"""Tests for the synthetic data generator (pure part: no database needed).

The generator is only useful if its output is trustworthy, because later we
validate the whole data pipeline against it.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.app.core.units import SUPPORTED_UNITS
from data_engineering.synthetic_data import (
    PRODUCT_PROFILES,
    ProductProfile,
    assert_safe_target,
    build_ground_truth,
    generate_history,
)

END = datetime(2026, 10, 31, tzinfo=timezone.utc)


def _history(**overrides):
    params = {"end": END, "days": 180, "seed": 42}
    params.update(overrides)
    return generate_history(PRODUCT_PROFILES, **params)


def _signature(items):
    """A comparable fingerprint of a whole history."""
    return [
        (i.profile.name, i.created_at, [(e.event_type, e.occurred_at) for e in i.events])
        for i in items
    ]


def test_same_seed_gives_identical_history():
    assert _signature(_history(seed=7)) == _signature(_history(seed=7))


def test_different_seed_gives_different_history():
    assert _signature(_history(seed=1)) != _signature(_history(seed=2))


def test_adding_a_product_does_not_change_existing_products():
    # Each product has its own random generator, so a validated history must
    # not shift when a new product is added to the list.
    extra = ProductProfile("Tea", "kg", Decimal("1"), 40, 2.0, "pantry")
    base = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42)
    more = generate_history(PRODUCT_PROFILES + (extra,), end=END, days=180, seed=42)

    rice_base = [i for i in base if i.profile.name == "Rice"]
    rice_more = [i for i in more if i.profile.name == "Rice"]
    assert _signature(rice_base) == _signature(rice_more)


def test_every_item_starts_with_added_then_purchased_in_time_order():
    for item in _history():
        times = [e.occurred_at for e in item.events]
        assert item.events[0].event_type == "added"
        assert "purchased" in [e.event_type for e in item.events]
        assert times == sorted(times)


def test_no_event_happens_in_the_future():
    for item in _history():
        assert all(e.occurred_at < END for e in item.events)


def test_every_unit_is_supported_by_the_database_constraint():
    assert {i.profile.unit for i in _history()} <= SUPPORTED_UNITS


def test_realized_intervals_match_the_configured_rhythm():
    truth = build_ground_truth(_history(), PRODUCT_PROFILES, end=END, days=180, seed=42)

    for profile in PRODUCT_PROFILES:
        product = truth["products"][profile.name]
        assert product["purchases"] >= 5
        assert min(product["intervals_days"]) >= 1
        # The average of many noisy gaps stays close to the configured mean.
        assert abs(product["realized_mean_interval_days"] - profile.mean_days) < 1.5


def test_mistaken_taps_use_the_last_purchase_as_the_real_one():
    items = _history(mistake_rate=1.0)  # every purchase has a mistaken tap
    for item in items:
        types = [e.event_type for e in item.events]
        assert "reverted" in types
        purchases = [e.occurred_at for e in item.events if e.event_type == "purchased"]
        assert len(purchases) == 2
        assert item.effective_purchase == purchases[1]


def test_no_mistakes_when_rate_is_zero():
    for item in _history(mistake_rate=0.0):
        assert "reverted" not in [e.event_type for e in item.events]


def test_naive_end_is_rejected():
    with pytest.raises(ValueError):
        generate_history(PRODUCT_PROFILES, end=datetime(2026, 10, 31))


def test_unsupported_unit_in_a_profile_is_rejected():
    bad = ProductProfile("Soup", "cup", Decimal("1"), 10, 1.0, "pantry")
    with pytest.raises(ValueError, match="Unsupported unit"):
        generate_history((bad,), end=END)


def test_guard_refuses_a_missing_url():
    with pytest.raises(SystemExit):
        assert_safe_target(None)


@pytest.mark.parametrize("protected", ["DATABASE_URL", "TEST_DATABASE_URL"])
def test_guard_refuses_the_application_and_test_databases(monkeypatch, protected):
    url = "postgresql://user:pw@ep-aaa.example.com/neondb"
    monkeypatch.setenv(protected, url)

    with pytest.raises(SystemExit):
        assert_safe_target(url)


def test_guard_accepts_a_different_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@ep-aaa.example.com/neondb")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://u:p@ep-bbb.example.com/neondb")

    assert_safe_target("postgresql://u:p@ep-ccc.example.com/neondb")  # no exception
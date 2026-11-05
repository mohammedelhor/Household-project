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
    TWO_HOUSEHOLDS,
    assert_safe_target,
    build_ground_truth,
    generate_history,
    generate_households,
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
    truth = build_ground_truth(_history(), end=END, days=180, seed=42, scope_keys={None: "user:1"})

    for profile in PRODUCT_PROFILES:
        product = truth["scopes"]["user:1"]["products"][profile.name]
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


# ----------------------- households and cancelled purchases -------------------

def _two_households(**overrides):
    params = {"end": END, "days": 180, "seed": 42}
    params.update(overrides)
    return generate_households(TWO_HOUSEHOLDS, **params)


def _truth_of_two_households(items):
    return build_ground_truth(items, end=END, days=180, seed=42,
                              scope_keys={"family_a": "household:1", "family_b": "household:2"})


def test_two_households_have_the_rhythms_they_were_given():
    truth = _truth_of_two_households(_two_households())["scopes"]

    milk_a = truth["household:1"]["products"]["Milk"]["realized_mean_interval_days"]
    milk_b = truth["household:2"]["products"]["Milk"]["realized_mean_interval_days"]
    assert abs(milk_a - 7) < 1.5 and abs(milk_b - 30) < 4     # same product, very different rhythm
    assert "Rice" not in truth["household:1"]["products"]       # a product A never buys


def test_every_item_knows_its_household_and_the_histories_are_independent():
    items = _two_households()
    assert {i.household for i in items} == {"family_a", "family_b"}

    only_a = generate_households(TWO_HOUSEHOLDS[:1], end=END, days=180, seed=42)
    mine = [i for i in items if i.household == "family_a"]
    assert [(i.profile.name, i.created_at) for i in mine] == \
           [(i.profile.name, i.created_at) for i in only_a]      # B's existence changes nothing for A


def test_cancel_rate_zero_changes_nothing_and_a_positive_rate_never_touches_real_purchases():
    plain = _history()
    with_phantoms = _history(cancel_rate=0.5)

    real = [i for i in with_phantoms if not i.cancelled]
    assert _signature(real) == _signature(plain)               # separate random stream
    assert any(i.cancelled for i in with_phantoms)
    assert not any(i.cancelled for i in plain)


def test_a_phantom_is_bought_then_reverted_and_never_a_purchase():
    phantoms = [i for i in _history(cancel_rate=1.0) if i.cancelled]

    assert phantoms
    for phantom in phantoms:
        assert [e.event_type for e in phantom.events] == ["added", "purchased", "reverted"]
        assert phantom.archived_at is None
        with pytest.raises(ValueError):
            phantom.effective_purchase


def test_phantoms_are_not_in_the_answer_key():
    plain = build_ground_truth(_history(), end=END, days=180, seed=42, scope_keys={None: "user:1"})
    noisy = build_ground_truth(_history(cancel_rate=0.5), end=END, days=180, seed=42,
                               scope_keys={None: "user:1"})

    assert noisy["scopes"] == plain["scopes"]


def test_the_answer_key_refuses_to_guess_a_scope_name():
    with pytest.raises(TypeError):
        build_ground_truth(_history(), end=END, days=180, seed=42)    # scope_keys is required

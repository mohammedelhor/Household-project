"""Tests for as-of reconstruction: the model must never see the future."""
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data_engineering.synthetic_data import PRODUCT_PROFILES, generate_history
from data_engineering.transform import Event, build_purchases
from ml.asof import events_as_of, intervals_as_of, purchases_as_of

END = datetime(2026, 10, 31, tzinfo=timezone.utc)
OCT = lambda day, hour=10: datetime(2026, 10, day, hour, tzinfo=timezone.utc)  # noqa: E731


def event(event_id, kind, when, item_id=1, product_id=1):
    return Event(id=event_id, item_id=item_id, product_id=product_id, list_id=1,
                 event_type=kind, quantity=Decimal("2"), unit="kg", actor_user_id=1,
                 occurred_at=when)


# The history from the module's docstring.
HISTORY = [
    event(1, "added", OCT(1)),
    event(2, "purchased", OCT(10)),
    event(3, "reverted", OCT(12)),
    event(4, "purchased", OCT(15)),
]


def purchase_days(events, cutoff):
    purchases, _ = purchases_as_of(events, cutoff)
    return [p.purchased_at.day for p in purchases]


# ------------------------------ the worked example ------------------------------

def test_before_the_first_purchase_there_is_none():
    assert purchase_days(HISTORY, OCT(9)) == []


def test_a_purchase_that_is_reverted_later_still_existed_at_the_time():
    assert purchase_days(HISTORY, OCT(11)) == [10]


def test_after_the_revert_the_purchase_is_gone():
    assert purchase_days(HISTORY, OCT(13)) == []


def test_after_the_second_purchase_only_it_counts():
    assert purchase_days(HISTORY, OCT(16)) == [15]


def test_the_final_table_would_leak_the_future_at_oct_11():
    final, _ = build_purchases(HISTORY)

    # The final state says "bought on Oct 15". At Oct 11 that had not happened.
    assert [p.purchased_at.day for p in final] == [15]
    assert purchase_days(HISTORY, OCT(11)) != [p.purchased_at.day for p in final]


def test_an_event_exactly_at_the_cutoff_is_known():
    assert purchase_days(HISTORY, OCT(10)) == [10]


def test_a_naive_cutoff_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        events_as_of(HISTORY, datetime(2026, 10, 11))


# ----------------------------- the no-leakage property -----------------------------

def _history_with_mistakes():
    """Generated data with many mistaken taps, as Event objects."""
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=7, mistake_rate=0.4)
    product_ids = {p.name: i for i, p in enumerate(PRODUCT_PROFILES, start=1)}
    rows = []
    for item_id, item in enumerate(items, start=1):
        for planned in item.events:
            rows.append((planned.occurred_at, item_id, planned.event_type,
                         product_ids[item.profile.name], item.profile))
    rows.sort(key=lambda r: r[0])
    return [
        Event(id=n, item_id=item_id, product_id=product_id, list_id=1, event_type=kind,
              quantity=Decimal(str(profile.quantity)), unit=profile.unit, actor_user_id=1,
              occurred_at=when)
        for n, (when, item_id, kind, product_id, profile) in enumerate(rows, start=1)
    ]


def _signature(purchases):
    return [(p.item_id, p.product_id, p.purchased_at, p.source_event_id) for p in purchases]


CUTOFFS = [END - timedelta(days=d, hours=h) for d in (150, 100, 60, 31, 7, 1) for h in (0, 7)]


@pytest.mark.parametrize("cutoff", CUTOFFS)
def test_changing_the_future_never_changes_what_was_known(cutoff):
    events = _history_with_mistakes()
    baseline = _signature(purchases_as_of(events, cutoff)[0])

    # Throw away every future event ...
    past_only = [e for e in events if e.occurred_at <= cutoff]
    assert _signature(purchases_as_of(past_only, cutoff)[0]) == baseline

    # ... or replace the future by nonsense (a revert storm, wrong quantities).
    rng = random.Random(cutoff.toordinal())
    future_noise = [
        Event(id=10_000 + i, item_id=rng.randint(1, 400), product_id=rng.randint(1, 5),
              list_id=1, event_type=rng.choice(["purchased", "reverted", "archived", "added"]),
              quantity=Decimal("9"), unit="kg", actor_user_id=1,
              occurred_at=cutoff + timedelta(minutes=1 + i))
        for i in range(300)
    ]
    assert _signature(purchases_as_of(past_only + future_noise, cutoff)[0]) == baseline


def test_far_in_the_future_everything_is_known_and_matches_the_final_state():
    events = _history_with_mistakes()

    assert _signature(purchases_as_of(events, END + timedelta(days=365))[0]) == \
        _signature(build_purchases(events)[0])


def test_a_purchase_reverted_later_is_known_just_before_the_revert_but_not_in_the_final_state():
    events = _history_with_mistakes()
    by_item = {}
    for e in events:
        by_item.setdefault(e.item_id, []).append(e)
    # An item that was purchased and then reverted (a mistaken tap, undone).
    item_events = next(
        evs for evs in by_item.values()
        if any(e.event_type == "reverted" for e in evs)
    )
    revert = next(e for e in item_events if e.event_type == "reverted")
    mistaken = [e for e in item_events
                if e.event_type == "purchased" and e.occurred_at < revert.occurred_at][-1]
    final = {p.item_id: p.source_event_id for p in build_purchases(events)[0]}

    just_before = purchases_as_of(events, revert.occurred_at - timedelta(microseconds=1))[0]
    known = {p.item_id: p.source_event_id for p in just_before}

    assert known[revert.item_id] == mistaken.id          # it existed at that moment
    assert final.get(revert.item_id) != mistaken.id      # the final state hides or replaces it


def test_intervals_as_of_use_only_purchases_known_at_the_cutoff():
    events = _history_with_mistakes()
    names = {i: (p.name, p.name.casefold()) for i, p in enumerate(PRODUCT_PROFILES, start=1)}
    cutoff = END - timedelta(days=60)

    rows = intervals_as_of(events, cutoff, names)

    assert rows and all(r["purchase_date"] <= cutoff.date() for r in rows)
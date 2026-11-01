"""Tests for the ML baseline: target/samples, predictors, evaluation.

The most important one is the LEAKAGE property: whatever happens after a
sample's cutoff must not change that sample's features.
"""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data_engineering.synthetic_data import PRODUCT_PROFILES, generate_history
from data_engineering.transform import Event
from ml.baselines import (ALL_BASELINES, FixedDays, LastInterval, MeanInterval,
                          MedianInterval)
from ml.evaluate import best_constant_reference, evaluate
from ml.target import Sample, build_samples

START = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)


def day(n, hour=10):
    return datetime(2026, 9, 1, hour, tzinfo=timezone.utc) + timedelta(days=n)


def buy(events, item_id, product_id, when, *, first_id=None):
    """Append added (1 day earlier) + purchased events for one item."""
    base = len(events) + 1 if first_id is None else first_id
    events.append(Event(base, item_id, product_id, 1, "added", Decimal("1"), "kg", 1,
                        when - timedelta(days=1)))
    events.append(Event(base + 1, item_id, product_id, 1, "purchased", Decimal("1"), "kg", 1, when))


def history(product_id, offsets):
    events: list[Event] = []
    for i, offset in enumerate(offsets, start=1):
        buy(events, item_id=product_id * 100 + i, product_id=product_id, when=day(offset))
    events.sort(key=lambda e: (e.occurred_at, e.id))
    return events


# ------------------------------- samples --------------------------------------

def test_one_sample_per_purchase_except_the_last():
    samples = build_samples(history(1, [0, 5, 12, 17]))
    assert len(samples) == 3                      # 4 purchases -> 3 "next" answers


def test_target_is_days_until_the_next_purchase():
    samples = build_samples(history(1, [0, 5, 12, 17]))
    assert [s.target_days for s in samples] == [5, 7, 5]


def test_history_holds_only_gaps_known_at_the_cutoff():
    samples = build_samples(history(1, [0, 5, 12, 17]))
    assert [s.history_days for s in samples] == [(), (5,), (5, 7)]


def test_last_purchase_date_is_the_cutoff_day():
    samples = build_samples(history(1, [0, 5, 12]))
    assert samples[1].last_purchase_date == day(5).date()


def test_one_purchase_gives_no_sample():
    assert build_samples(history(1, [0])) == []


def test_products_are_kept_apart():
    events = sorted(history(1, [0, 5, 10]) + history(2, [1, 9, 17]),
                    key=lambda e: (e.occurred_at, e.id))
    # ids must stay unique across the merged list
    events = [Event(n, e.item_id, e.product_id, e.list_id, e.event_type, e.quantity,
                    e.unit, e.actor_user_id, e.occurred_at) for n, e in enumerate(events, start=1)]
    by_product = {p: [s.target_days for s in build_samples(events) if s.product_id == p]
                  for p in (1, 2)}
    assert by_product == {1: [5, 5], 2: [8, 8]}


def test_free_text_items_are_ignored():
    events = history(1, [0, 5, 10])
    events = [Event(e.id, e.item_id, None, e.list_id, e.event_type, e.quantity, e.unit,
                    e.actor_user_id, e.occurred_at) for e in events]
    assert build_samples(events) == []


def test_samples_are_ordered_by_cutoff():
    events = sorted(history(1, [0, 9, 18]) + history(2, [2, 4, 6]),
                    key=lambda e: (e.occurred_at, e.id))
    events = [Event(n, e.item_id, e.product_id, e.list_id, e.event_type, e.quantity,
                    e.unit, e.actor_user_id, e.occurred_at) for n, e in enumerate(events, start=1)]
    cutoffs = [s.cutoff for s in build_samples(events)]
    assert cutoffs == sorted(cutoffs)


def test_a_purchase_that_was_reverted_does_not_become_a_target():
    events = history(1, [0, 5, 10])
    # item 102 (day 5) is reverted a day later and never bought again
    events.append(Event(99, 102, 1, 1, "reverted", Decimal("1"), "kg", 1, day(6)))
    events.sort(key=lambda e: (e.occurred_at, e.id))
    samples = build_samples(events)
    assert [s.target_days for s in samples] == [10]     # day 0 -> day 10


# ---------------------------- leakage property --------------------------------

def test_future_events_never_change_earlier_samples():
    """Add events AFTER a cutoff: the features of that sample must not move.

    Only the TARGET is allowed to depend on the future.
    """
    short = history(1, [0, 5, 12, 17])
    longer = history(1, [0, 5, 12, 17, 30, 31, 50])
    early, late = build_samples(short), build_samples(longer)
    for a, b in zip(early, late):
        assert (a.cutoff, a.last_purchase_date, a.history_days) == \
               (b.cutoff, b.last_purchase_date, b.history_days)


def test_a_later_revert_does_not_remove_a_purchase_from_the_history():
    """The history is as known at the cutoff: a purchase reverted LATER was
    still a purchase then. Only the settled answers ignore it."""
    events = history(1, [0, 5, 12])
    events.append(Event(99, 101, 1, 1, "reverted", Decimal("1"), "kg", 1, day(20)))
    events.sort(key=lambda e: (e.occurred_at, e.id))
    # item 101 (day 0) is reverted at day 20, after every cutoff below.
    samples = build_samples(events)

    # Settled state: day 0 never counted, so there is no sample at day 0 ...
    assert [s.cutoff for s in samples] == [day(5)]
    # ... but standing at day 5 the day-0 purchase WAS known, so the gap 0->5 exists.
    assert samples[0].history_days == (5,)
    assert samples[0].target_days == 7


# ------------------------------- predictors ------------------------------------

def test_predictors_on_a_small_history():
    h = (5, 9, 7, 3)
    assert LastInterval().predict(h) == 3.0
    assert MeanInterval().predict(h) == 6.0
    assert MedianInterval().predict(h) == 6.0
    assert FixedDays(14).predict(h) == 14.0


def test_predictors_return_none_without_history():
    for predictor in (LastInterval(), MeanInterval(), MedianInterval()):
        assert predictor.predict(()) is None


def test_the_median_ignores_one_outlier_and_the_mean_does_not():
    h = (7, 7, 7, 70)
    assert MedianInterval().predict(h) == 7.0
    assert MeanInterval().predict(h) == 22.75


def test_every_baseline_has_a_name():
    assert all(b.name for b in ALL_BASELINES)


# ------------------------------- evaluation ------------------------------------

def sample(history_days, target, product=1):
    return Sample(product, day(0), day(0).date(), tuple(history_days), target)


def test_metrics_by_hand():
    # median of (7,7) = 7 -> errors: 7-7=0, 7-9=-2, 7-6=+1
    samples = [sample((7, 7), 7), sample((7, 7), 9), sample((7, 7), 6)]
    m, _ = evaluate(samples, MedianInterval())
    assert m.n == 3
    assert m.mae == pytest.approx(1.0)               # (0+2+1)/3
    assert m.bias == pytest.approx(-1 / 3)           # (0-2+1)/3
    assert m.within_1_day == pytest.approx(2 / 3)    # errors 0 and 1


def test_samples_with_too_little_history_are_skipped_for_everyone():
    samples = [sample((), 5), sample((6,), 5), sample((6, 6), 5)]
    for predictor in (LastInterval(), MeanInterval(), FixedDays(14)):
        m, _ = evaluate(samples, predictor, min_intervals=2)
        assert m.n == 1               # the SAME single sample, so scores are comparable


def test_halves_round_up_not_to_even():
    m, _ = evaluate([sample((5, 6), 6)], MeanInterval(), min_intervals=2)   # mean 5.5 -> 6
    assert m.mae == 0.0


def test_per_product_metrics_are_separate():
    samples = [sample((5, 5), 5, product=1), sample((10, 10), 14, product=2)]
    _, per_product = evaluate(samples, MedianInterval())
    assert per_product[1].mae == 0.0 and per_product[2].mae == 4.0


def test_empty_input_gives_nan_not_a_crash():
    m, per_product = evaluate([], MedianInterval())
    assert m.n == 0 and math.isnan(m.mae) and per_product == {}


def test_best_constant_reference_is_never_worse_than_median_on_its_own_data():
    samples = [sample((6, 6), t) for t in (4, 6, 6, 9, 5, 7, 6)]
    reference, _ = best_constant_reference(samples)
    median, _ = evaluate(samples, MedianInterval())
    assert reference.mae <= median.mae + 1e-9


# ---------------------------- end to end on synthetic data ----------------------

def synthetic_events(mistake_rate=0.05):
    end = datetime(2026, 10, 31, tzinfo=timezone.utc)
    pid = {p.name: i for i, p in enumerate(PRODUCT_PROFILES, start=1)}
    items = generate_history(PRODUCT_PROFILES, end=end, days=180, seed=42,
                             mistake_rate=mistake_rate)
    rows = sorted(((e.occurred_at, iid, e.event_type, pid[it.profile.name], it.profile)
                   for iid, it in enumerate(items, start=1) for e in it.events),
                  key=lambda r: r[0])
    return [Event(n, iid, p, 1, kind, prof.quantity, prof.unit, 1, when)
            for n, (when, iid, kind, p, prof) in enumerate(rows, start=1)]


def test_on_synthetic_data_per_product_rules_beat_a_single_constant():
    samples = build_samples(synthetic_events())
    median, _ = evaluate(samples, MedianInterval())
    fixed, _ = evaluate(samples, FixedDays(14))
    assert median.mae < fixed.mae / 3


def test_on_synthetic_data_the_median_is_close_to_the_reference_floor():
    samples = build_samples(synthetic_events())
    median, _ = evaluate(samples, MedianInterval())
    reference, _ = best_constant_reference(samples)
    assert reference.mae <= median.mae <= reference.mae + 0.5


def test_mistaken_taps_do_not_change_the_number_of_samples_much():
    clean = build_samples(synthetic_events(0.0))
    noisy = build_samples(synthetic_events(0.4))
    assert abs(len(clean) - len(noisy)) <= 3
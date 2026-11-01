"""Tests for suggestions, with exact numbers worked out by hand."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data_engineering.transform import Event
from ml.suggest import (confidence_for, products_waiting_on_a_list, status_for, suggest)

T0 = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
day = lambda n, hour=10: T0 + timedelta(days=n, hours=hour - 10)  # noqa: E731
NAMES = {1: "Milk", 2: "Rice"}


class Log:
    """Builds an event list with unique ids."""
    def __init__(self):
        self.events: list[Event] = []
        self.item = 0

    def _add(self, item, product, kind, when):
        self.events.append(Event(len(self.events) + 1, item, product, 1, kind,
                                 Decimal("1"), "kg", 1, when))

    def bought(self, product, n, archive=True):
        """An item added a day before, purchased on day n, archived a day later."""
        self.item += 1
        self._add(self.item, product, "added", day(n - 1))
        self._add(self.item, product, "purchased", day(n))
        if archive:
            self._add(self.item, product, "archived", day(n + 1))
        return self.item

    def open_item(self, product, n):
        self.item += 1
        self._add(self.item, product, "added", day(n))
        return self.item


def by_name(result):
    return {s.product_name: s for s in result}


def test_expected_date_is_last_purchase_plus_median_gap():
    log = Log()
    for n in (0, 5, 10, 16):                    # gaps 5, 5, 6 -> median 5
        log.bought(1, n)
    s = by_name(suggest(log.events, NAMES, as_of=day(18)))["Milk"]

    assert s.typical_gap_days == 5
    assert s.last_purchase_date == day(16).date()
    assert s.expected_date == day(21).date()
    assert s.days_until == 3                    # expected Sep 22, today Sep 19


def test_status_boundaries():
    assert status_for(-1) == "overdue"
    assert status_for(0) == "due_soon"          # expected today
    assert status_for(2) == "due_soon"
    assert status_for(3) == "not_yet"


def test_overdue_product_is_listed_first():
    log = Log()
    for n in (0, 5, 10):                         # Milk: gap 5, expected day 15
        log.bought(1, n)
    for n in (0, 14, 28):                        # Rice: day 28 is still the FUTURE at day 20
        log.bought(2, n)
    result = suggest(log.events, NAMES, as_of=day(20))

    assert [s.product_name for s in result] == ["Milk", "Rice"]
    assert result[0].status == "overdue" and result[0].days_until == -5
    # Rice: only days 0 and 14 are known -> gap 14, expected day 28, today day 20
    assert result[1].status == "not_yet" and result[1].days_until == 8
    assert result[1].gaps_used == 1 and result[1].confidence == "low"


def test_even_number_of_gaps_rounds_the_median_half_up():
    log = Log()
    for n in (0, 5, 11):                         # gaps 5, 6 -> median 5.5 -> 6
        log.bought(1, n)
    s = by_name(suggest(log.events, NAMES, as_of=day(12)))["Milk"]
    assert s.typical_gap_days == 6


def test_unknown_when_there_is_no_gap_yet():
    log = Log()
    log.bought(1, 3)                             # a single purchase: no gap
    result = by_name(suggest(log.events, NAMES, as_of=day(10)))

    assert result["Milk"].status == "unknown" and result["Milk"].expected_date is None
    assert result["Milk"].last_purchase_date == day(3).date()
    assert result["Rice"].status == "unknown" and result["Rice"].last_purchase_date is None
    assert result["Rice"].confidence == "none"


def test_unknown_products_come_last():
    log = Log()
    for n in (0, 5, 10):
        log.bought(2, n)
    assert [s.product_name for s in suggest(log.events, NAMES, as_of=day(11))] == ["Rice", "Milk"]


def test_a_product_waiting_on_a_list_is_flagged():
    log = Log()
    for n in (0, 5, 10):
        log.bought(1, n)
    log.open_item(1, 14)                         # Milk added again, not bought yet
    result = by_name(suggest(log.events, NAMES, as_of=day(15)))
    assert result["Milk"].already_on_list is True
    assert result["Rice"].already_on_list is False


def test_a_bought_or_archived_item_is_not_waiting():
    log = Log()
    log.bought(1, 0, archive=False)              # bought, still visible on the list
    log.bought(2, 0, archive=True)
    assert products_waiting_on_a_list(log.events) == set()


def test_a_reverted_purchase_puts_the_item_back_on_the_list():
    log = Log()
    item = log.bought(1, 0, archive=False)
    log._add(item, 1, "reverted", day(1))
    assert products_waiting_on_a_list(log.events) == {1}


def test_future_events_do_not_change_a_suggestion_made_earlier():
    """The leakage property again: what happens after `as_of` is invisible."""
    log = Log()
    for n in (0, 5, 10):
        log.bought(1, n)
    before = suggest(list(log.events), NAMES, as_of=day(12))
    for n in (30, 31, 90):                       # much later purchases
        log.bought(1, n)
    after = suggest(log.events, NAMES, as_of=day(12))
    assert before == after


def test_as_of_must_have_a_time_zone():
    with pytest.raises(ValueError):
        suggest([], NAMES, as_of=datetime(2026, 9, 1))


@pytest.mark.parametrize("gaps, expected", [(0, "none"), (1, "low"), (2, "low"),
                                            (3, "medium"), (6, "medium"), (7, "high")])
def test_confidence_levels(gaps, expected):
    assert confidence_for(gaps) == expected


def test_today_is_the_utc_date_of_as_of():
    log = Log()
    for n in (0, 5, 10):
        log.bought(1, n)                         # expected Sep 16
    late = datetime(2026, 9, 15, 23, 30, tzinfo=timezone.utc)
    s = by_name(suggest(log.events, NAMES, as_of=late))["Milk"]
    assert s.days_until == 1 and s.expected_date == date(2026, 9, 16)
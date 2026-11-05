"""Tests for suggestions, with exact numbers worked out by hand."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data_engineering.scopes import Scope
from data_engineering.transform import Event
from ml.evidence import (MIN_GAPS_FOR_HEADLINE, MIN_GAPS_TO_SUGGEST, STRENGTHS, TIERS,
                         history_strength_for, tier_for)
from ml.suggest import products_waiting_on_lists, status_for
from ml.suggest import suggest as _suggest

HOME = Scope("household", 1)
SCOPES = {1: HOME, 2: Scope("household", 2)}


def suggest(events, names, *, as_of, scopes=SCOPES):
    return _suggest(events, names, scopes, as_of=as_of)


def products_waiting_on_a_list(events):
    return products_waiting_on_lists(events, SCOPES).get(HOME, set())

T0 = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
day = lambda n, hour=10: T0 + timedelta(days=n, hours=hour - 10)  # noqa: E731
NAMES = {1: "Milk", 2: "Rice"}


class Log:
    """Builds an event list with unique ids."""
    def __init__(self, list_id=1):
        self.events: list[Event] = []
        self.item = 0
        self.list_id = list_id

    def _add(self, item, product, kind, when):
        self.events.append(Event(len(self.events) + 1, item, product, self.list_id, kind,
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
    assert result[1].gaps_used == 1 and result[1].history_strength == "short"


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
    assert result["Milk"].history_strength == "none"
    # Rice was never bought in this scope: no guess is made from other scopes
    assert "Rice" not in result


def test_unknown_products_come_last():
    log = Log()
    for n in (0, 5, 10):
        log.bought(2, n)
    log.bought(1, 8)                             # Milk: a single purchase, no gap yet
    assert [s.product_name for s in suggest(log.events, NAMES, as_of=day(11))] == ["Rice", "Milk"]


def test_a_product_waiting_on_a_list_is_flagged():
    log = Log()
    for n in (0, 5, 10):
        log.bought(1, n)
    log.open_item(1, 14)                         # Milk added again, not bought yet
    log.bought(2, 0)
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


@pytest.mark.parametrize("gaps, expected", [(0, "none"), (1, "short"), (2, "short"),
                                            (3, "moderate"), (6, "moderate"), (7, "long")])
def test_history_strength_levels(gaps, expected):
    assert history_strength_for(gaps) == expected


def test_every_strength_the_suggestions_can_emit_is_backed_by_report_tiers():
    """The report measures the error per tier; a level must be a union of tiers,
    or users would see a label whose accuracy nobody measured."""
    for gaps in range(0, 40):
        strength = history_strength_for(gaps)
        assert strength in STRENGTHS
        assert (tier_for(gaps) is None) == (strength == "none")
    # tier boundaries coincide with level boundaries
    starts = {low for _, low, _ in TIERS}
    assert {1, 3, 7} <= starts


def test_the_suggestions_threshold_is_the_one_the_report_scores():
    assert MIN_GAPS_TO_SUGGEST == 1 and tier_for(MIN_GAPS_TO_SUGGEST) is not None
    assert MIN_GAPS_FOR_HEADLINE >= MIN_GAPS_TO_SUGGEST


# ------------------------------ scopes ---------------------------------------

def two_scope_log():
    a, b = Log(1), Log(2)
    for n in (0, 7, 14, 21):
        a.bought(1, n)
    for n in (0, 30, 60, 90):
        b.bought(1, n)
    events = sorted(a.events + b.events, key=lambda e: (e.occurred_at, e.id))
    return [Event(i, e.item_id + (1000 if e.list_id == 2 else 0), e.product_id, e.list_id,
                  e.event_type, e.quantity, e.unit, e.actor_user_id, e.occurred_at)
            for i, e in enumerate(events, start=1)]


def test_two_households_with_different_rhythms_are_never_merged():
    result = {s.scope: s for s in suggest(two_scope_log(), NAMES, as_of=day(95))}
    assert result[SCOPES[1]].typical_gap_days == 7
    assert result[SCOPES[2]].typical_gap_days == 30      # a pooled rule would say ~18


def test_future_events_of_another_scope_do_not_change_a_suggestion():
    base = Log(1)
    for n in (0, 7, 14):
        base.bought(1, n)
    other = Log(2)
    for n in (1, 40, 80):
        other.bought(1, n)
    shifted = [Event(e.id + 500, e.item_id + 1000, e.product_id, 2, e.event_type, e.quantity,
                     e.unit, e.actor_user_id, e.occurred_at) for e in other.events]
    alone = [s for s in suggest(base.events, NAMES, as_of=day(16))]
    mixed = [s for s in suggest(base.events + shifted, NAMES, as_of=day(16)) if s.scope == HOME]
    assert alone == mixed


def test_a_product_waiting_in_another_scope_is_not_flagged_here():
    a, b = Log(1), Log(2)
    for n in (0, 7, 14):
        a.bought(1, n)
    b.open_item(1, 15)                                  # waiting on the OTHER household's list
    events = a.events + [Event(e.id + 500, e.item_id + 1000, e.product_id, 2, e.event_type,
                               e.quantity, e.unit, e.actor_user_id, e.occurred_at)
                         for e in b.events]
    s = next(x for x in suggest(events, NAMES, as_of=day(16)) if x.scope == HOME)
    assert s.already_on_list is False


def test_today_is_the_utc_date_of_as_of():
    log = Log()
    for n in (0, 5, 10):
        log.bought(1, n)                         # expected Sep 16
    late = datetime(2026, 9, 15, 23, 30, tzinfo=timezone.utc)
    s = by_name(suggest(log.events, NAMES, as_of=late))["Milk"]
    assert s.days_until == 1 and s.expected_date == date(2026, 9, 16)


# ------------------------- the file the API serves ----------------------------

def test_serving_payload_has_the_documented_shape_and_no_daily_fields(tmp_path):
    import json
    from ml.suggest import SERVING_SCHEMA_VERSION, build_serving_payload, write_serving_file

    log = Log()
    for n in (0, 5, 10):
        log.bought(1, n)
    result = suggest(log.events, NAMES, as_of=day(12))
    payload = build_serving_payload(result, run_id="abc123", fingerprint="f" * 64,
                                    generated_at=day(12))

    assert payload["schema_version"] == SERVING_SCHEMA_VERSION == 2
    assert payload["dataset"] == {"run_id": "abc123", "input_fingerprint": "f" * 64}
    milk = next(s for s in payload["suggestions"] if s["product_name"] == "Milk")
    assert milk["expected_date"] == day(15).date().isoformat()
    assert milk["typical_gap_days"] == 5 and milk["history_strength"] == "short"   # 2 gaps
    assert (milk["scope_type"], milk["scope_id"]) == ("household", 1)
    assert "confidence" not in milk
    # what changes every day (or with every list edit) must NOT be stored
    assert not {"status", "days_until", "already_on_list"} & set(milk)

    path = tmp_path / "serving" / "suggestions.json"      # parent folder does not exist yet
    write_serving_file(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert not list(path.parent.glob("*.tmp"))             # no temp file left behind


def test_writing_again_replaces_the_file(tmp_path):
    from ml.suggest import write_serving_file
    path = tmp_path / "s.json"
    write_serving_file(path, {"a": 1})
    write_serving_file(path, {"a": 2})
    assert path.read_text(encoding="utf-8").count('"a": 2') == 1

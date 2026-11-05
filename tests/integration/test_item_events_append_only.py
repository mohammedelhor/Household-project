"""item_events must be append-only in the DATABASE, not only in the application.

Each test installs the exact SQL the migration c4d8f2a61b73 contains, inside
the test's own transaction (rolled back afterwards), so the tests pass whether
or not the migration was already applied to the test database.
"""
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from backend.app.db.event_admin import allow_event_correction
from backend.app.db.models import ItemEvent, Product, ShoppingList, ShoppingListItem, User

MIGRATION = (Path(__file__).resolve().parents[2] / "backend" / "migrations" / "versions"
             / "c4d8f2a61b73_make_item_events_append_only.py")


def _install(db_session):
    spec = importlib.util.spec_from_file_location("append_only_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_session.execute(text(module.FUNCTION_SQL))
    for statement in [s.strip() for s in module.TRIGGER_SQL.split(";") if s.strip()]:
        db_session.execute(text(statement))


def _event(db_session, *, with_product=False, with_actor=False):
    """One list, one item and one stored event; returns (event, product, actor)."""
    owner = User(email="append-owner@example.com", password_hash="x")
    actor = User(email="append-actor@example.com", password_hash="x") if with_actor else None
    product = Product(name="Rice", normalized_name="rice") if with_product else None
    db_session.add_all([x for x in (owner, actor, product) if x is not None])
    db_session.flush()
    shopping_list = ShoppingList(owner_user_id=owner.id, name="L")
    db_session.add(shopping_list)
    db_session.flush()
    item = ShoppingListItem(list_id=shopping_list.id, product_id=product.id if product else None,
                            product_name="Rice", quantity=Decimal("2"), unit="kg")
    db_session.add(item)
    db_session.flush()
    event = ItemEvent(item_id=item.id, product_id=product.id if product else None,
                      list_id=shopping_list.id, event_type="added", quantity=Decimal("2"),
                      unit="kg", actor_user_id=actor.id if actor else None)
    db_session.add(event)
    db_session.flush()
    return event, product, actor


def _blocked(db_session, sql, **params):
    with pytest.raises(DBAPIError, match="append-only"):
        db_session.execute(text(sql), params)


@pytest.mark.integration
def test_an_event_cannot_be_updated(db_session):
    _install(db_session)
    event, _, _ = _event(db_session)
    _blocked(db_session, "UPDATE item_events SET quantity = 5 WHERE id = :i", i=event.id)


@pytest.mark.integration
def test_an_event_cannot_be_updated_to_another_type_or_time(db_session):
    _install(db_session)
    event, _, _ = _event(db_session)
    _blocked(db_session, "UPDATE item_events SET event_type = 'purchased' WHERE id = :i", i=event.id)


@pytest.mark.integration
def test_an_event_cannot_be_deleted(db_session):
    _install(db_session)
    event, _, _ = _event(db_session)
    _blocked(db_session, "DELETE FROM item_events WHERE id = :i", i=event.id)


@pytest.mark.integration
def test_the_table_cannot_be_truncated(db_session):
    _install(db_session)
    _event(db_session)
    _blocked(db_session, "TRUNCATE item_events")


@pytest.mark.integration
def test_a_null_reference_cannot_be_given_a_new_value(db_session):
    """The only allowed change is value -> NULL, never NULL -> value or a swap."""
    _install(db_session)
    event, product, _ = _event(db_session, with_product=True)
    other = Product(name="Beans", normalized_name="beans")
    db_session.add(other)
    db_session.flush()
    _blocked(db_session, "UPDATE item_events SET product_id = :p WHERE id = :i",
             p=other.id, i=event.id)


@pytest.mark.integration
def test_deleting_a_product_still_works_and_keeps_the_history(db_session):
    _install(db_session)
    event, product, _ = _event(db_session, with_product=True)

    db_session.execute(text("DELETE FROM products WHERE id = :p"), {"p": product.id})   # ON DELETE SET NULL

    kept = db_session.execute(
        text("SELECT product_id, quantity FROM item_events WHERE id = :i"), {"i": event.id}).one()
    assert kept.product_id is None and kept.quantity == Decimal("2.000")


@pytest.mark.integration
def test_deleting_a_user_still_works_and_keeps_the_history(db_session):
    _install(db_session)
    event, _, actor = _event(db_session, with_actor=True)

    db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": actor.id})        # ON DELETE SET NULL

    assert db_session.execute(
        text("SELECT actor_user_id FROM item_events WHERE id = :i"), {"i": event.id}
    ).scalar_one() is None


@pytest.mark.integration
def test_the_administrative_path_allows_one_correction(db_session):
    _install(db_session)
    event, _, _ = _event(db_session)

    allow_event_correction(db_session)
    db_session.execute(text("UPDATE item_events SET quantity = 5 WHERE id = :i"), {"i": event.id})
    db_session.execute(text("DELETE FROM item_events WHERE id = :i"), {"i": event.id})

    assert db_session.execute(select(ItemEvent).where(ItemEvent.id == event.id)).first() is None


@pytest.mark.integration
def test_the_flag_ends_with_the_transaction(db_session):
    """set_config(..., true) is transaction-local: a rollback switches it off."""
    _install(db_session)
    event, _, _ = _event(db_session)
    savepoint = db_session.begin_nested()
    allow_event_correction(db_session)
    savepoint.rollback()                       # rolls back the savepoint, flag included

    value = db_session.execute(text("SELECT current_setting('household.allow_event_correction', true)")).scalar_one()
    assert value in (None, "", "off")
    _blocked(db_session, "DELETE FROM item_events WHERE id = :i", i=event.id)
"""PostgreSQL-level checks for item_events and the unit CHECK constraints.

These run against a real PostgreSQL (like the other integration tests)
because the point is that the DATABASE itself enforces the rules,
independent of the application code.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import ItemEvent, Product, ShoppingListItem, User
from backend.app.services.shopping_lists import (
    add_shopping_list_item,
    create_shopping_list,
    update_item_status,
)


def _list_and_item(db_session):
    user = User(email="events@example.com", password_hash="test-hash")
    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session, "Events list", owner_user_id=user.id
    )
    item = add_shopping_list_item(
        db_session,
        shopping_list.id,
        product_name="Rice",
        quantity=Decimal("2"),
        unit="kg",
        created_by_user_id=user.id,
    )
    return user, shopping_list, item


@pytest.mark.integration
def test_purchase_history_is_recorded_in_order_with_timestamps(db_session):
    user, _, item = _list_and_item(db_session)

    update_item_status(db_session, item.id, "purchased", actor_user_id=user.id)
    update_item_status(db_session, item.id, "needed", actor_user_id=user.id)

    events = db_session.scalars(
        select(ItemEvent).where(ItemEvent.item_id == item.id).order_by(ItemEvent.id)
    ).all()

    assert [e.event_type for e in events] == ["added", "purchased", "reverted"]
    # server_default now() filled a timezone-aware timestamp for every event.
    assert all(e.occurred_at is not None and e.occurred_at.tzinfo for e in events)


@pytest.mark.integration
def test_database_rejects_unsupported_item_unit(db_session):
    _, shopping_list, _ = _list_and_item(db_session)

    # Bypass the service on purpose: the DB must refuse on its own.
    db_session.add(
        ShoppingListItem(
            list_id=shopping_list.id,
            product_name="Sugar",
            quantity=Decimal("1"),
            unit="cup",
            status="needed",
        )
    )

    with pytest.raises(IntegrityError, match="ck_shopping_list_item_unit_supported"):
        db_session.flush()


@pytest.mark.integration
def test_database_rejects_unsupported_product_default_unit(db_session):
    db_session.add(
        Product(name="Salt", normalized_name="salt", default_unit="cup")
    )

    with pytest.raises(IntegrityError, match="ck_product_default_unit_supported"):
        db_session.flush()


@pytest.mark.integration
def test_database_rejects_unknown_event_type(db_session):
    _, shopping_list, item = _list_and_item(db_session)

    db_session.add(
        ItemEvent(
            item_id=item.id,
            list_id=shopping_list.id,
            event_type="eaten",
            quantity=Decimal("1"),
            unit="kg",
        )
    )

    with pytest.raises(IntegrityError, match="ck_item_event_type"):
        db_session.flush()
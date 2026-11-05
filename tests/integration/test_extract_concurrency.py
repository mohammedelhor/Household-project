"""The late-commit problem, reproduced with REAL concurrent PostgreSQL
transactions (two separate connections), not by inserting a low id by hand.

Why it matters: PostgreSQL hands out an id when the INSERT runs, but the row
only becomes visible when its transaction COMMITS. So with two open
transactions, the one that got the smaller id can become visible LAST. The unit
tests in tests/unit/test_extract.py cover the reconciliation LOGIC; this test
proves the PROBLEM is real in PostgreSQL and that the logic fixes it.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session
from backend.app.db.event_admin import allow_event_correction
from backend.app.db.models import ItemEvent, ShoppingList, ShoppingListItem, User
from backend.app.services.shopping_lists import add_shopping_list_item, create_shopping_list
from data_engineering.extract import (
    extract_item_events,
    read_extracted_ids,
    reconcile_item_events,
)


def _insert_event(connection, item_id, list_id, event_type):
    """Insert one event and return the id PostgreSQL assigned to it."""
    return connection.execute(
        insert(ItemEvent.__table__)
        .values(item_id=item_id, list_id=list_id, event_type=event_type,
                quantity=Decimal("1"), unit="kg")
        .returning(ItemEvent.__table__.c.id)
    ).scalar_one()


@pytest.mark.integration
def test_a_row_committed_after_a_higher_id_is_recovered_by_reconciliation(test_engine, tmp_path):
    # Real, committed starting data (the db_session fixture rolls back, which
    # would hide exactly the commit ordering we want to observe).
    with Session(test_engine) as session:
        user = User(email=f"concurrency-{uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        session.flush()
        shopping_list = create_shopping_list(session, "Concurrency list", owner_user_id=user.id)
        item = add_shopping_list_item(
            session, shopping_list.id, product_name="Rice", quantity=Decimal("2"),
            unit="kg", created_by_user_id=user.id,
        )
        session.commit()
        user_id, list_id, item_id = user.id, shopping_list.id, item.id

    # "Now" far enough ahead that the safety lag hides nothing: this models a
    # transaction that stays open LONGER than the lag, the exact case the lag
    # cannot protect against.
    later = datetime.now(timezone.utc) + timedelta(hours=1)

    connection_a = test_engine.connect()
    transaction_a = connection_a.begin()
    try:
        # A gets the SMALLER id but does not commit yet.
        id_a = _insert_event(connection_a, item_id, list_id, "purchased")

        # B starts later, gets a LARGER id and commits at once.
        with test_engine.begin() as connection_b:
            id_b = _insert_event(connection_b, item_id, list_id, "archived")
        assert id_a < id_b

        # The extraction runs while A is still open: it sees B but not A, and
        # moves the bookmark past A's id.
        extract_item_events(test_engine, tmp_path, now=later)
        assert id_b in read_extracted_ids(tmp_path)
        assert id_a not in read_extracted_ids(tmp_path)

        # A finally commits. Its row is now in the table, below the bookmark.
        transaction_a.commit()

        # The normal incremental query ("id > bookmark") can never see it...
        assert extract_item_events(test_engine, tmp_path, now=later) is None
        assert id_a not in read_extracted_ids(tmp_path)

        # ...but reconciliation does.
        result = reconcile_item_events(test_engine, tmp_path, now=later)
        assert result.recovered >= 1
        assert id_a in read_extracted_ids(tmp_path)

        # And it settles: the next reconciliation finds nothing more.
        assert reconcile_item_events(test_engine, tmp_path, now=later).recovered == 0
    finally:
        if transaction_a.is_active:
            transaction_a.rollback()
        connection_a.close()
        with Session(test_engine) as session:
            allow_event_correction(session)
            session.execute(delete(ItemEvent).where(ItemEvent.item_id == item_id))
            session.execute(delete(ShoppingListItem).where(ShoppingListItem.list_id == list_id))
            session.execute(delete(ShoppingList).where(ShoppingList.id == list_id))
            session.execute(delete(User).where(User.id == user_id))
            session.commit()
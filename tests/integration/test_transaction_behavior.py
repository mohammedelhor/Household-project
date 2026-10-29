from decimal import Decimal
from queue import Queue
from threading import Barrier, Thread
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.models import ShoppingList, ShoppingListItem, User
from backend.app.services.shopping_lists import (
    add_shopping_list_item,
    create_shopping_list,
)

@pytest.mark.integration
def test_failed_operation_rolls_back_all_changes(
    db_session: Session,
    test_engine,
):
    user = User(
        email="transaction-rollback@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    user_id = user.id
    list_id = None

    with pytest.raises(RuntimeError, match="Simulated failure"):
        with db_session.begin_nested():
            shopping_list = create_shopping_list(
                db_session,
                "Rollback Test List",
                owner_user_id=user_id,
            )
            list_id = shopping_list.id

            add_shopping_list_item(
                db_session,
                list_id,
                product_name="Rice",
                quantity=Decimal("2"),
                unit="kg",
                created_by_user_id=user_id,
            )

            raise RuntimeError("Simulated failure")

    list_count = db_session.scalar(
        select(func.count())
        .select_from(ShoppingList)
        .where(ShoppingList.id == list_id)
    )

    item_count = db_session.scalar(
        select(func.count())
        .select_from(ShoppingListItem)
        .where(ShoppingListItem.list_id == list_id)
    )

    assert list_count == 0
    assert item_count == 0


@pytest.mark.integration
def test_concurrent_personal_list_creation_is_protected_by_database(
    test_engine,
):
    SessionFactory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
    )

    user_id = None
    outcomes = Queue()
    check_barrier = Barrier(2, timeout=15)

    # Create a committed user visible to both independent sessions.
    with SessionFactory() as session:
        user = User(
            email=f"concurrency-{uuid4().hex}@example.com",
            password_hash="test-hash",
        )
        session.add(user)
        session.commit()
        user_id = user.id

    def create_list():
        with SessionFactory() as session:
            original_scalar = session.scalar

            def synchronized_scalar(statement, *args, **kwargs):
                result = original_scalar(statement, *args, **kwargs)
                check_barrier.wait()
                return result

            try:
                # Both requests must finish the existence check
                # before either proceeds to insert.
                with patch.object(
                    session,
                    "scalar",
                    side_effect=synchronized_scalar,
                ):
                    create_shopping_list(
                        session,
                        "Concurrent List",
                        owner_user_id=user_id,
                    )
                    session.commit()

                outcomes.put("created")

            except IntegrityError:
                session.rollback()
                outcomes.put("integrity_error")

            except Exception as exc:
                session.rollback()
                outcomes.put(f"unexpected_error: {exc!r}")

    threads = [
        Thread(target=create_list),
        Thread(target=create_list),
    ]

    try:
        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(timeout=30)

        assert all(not thread.is_alive() for thread in threads), (
            "Concurrent workers did not finish in time."
        )

        results = [outcomes.get(timeout=2) for _ in range(2)]

        assert results.count("created") == 1, results
        assert results.count("integrity_error") == 1, results

        with SessionFactory() as session:
            active_count = session.scalar(
                select(func.count())
                .select_from(ShoppingList)
                .where(
                    ShoppingList.owner_user_id == user_id,
                    ShoppingList.archived_at.is_(None),
                )
            )

        assert active_count == 1

    finally:
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=30)

        # Never clean up while a worker is still using the database.
        if all(not thread.is_alive() for thread in threads):
            with SessionFactory() as session:
                session.execute(
                    delete(ShoppingList).where(
                        ShoppingList.owner_user_id == user_id
                    )
                )
                session.execute(
                    delete(User).where(User.id == user_id)
                )
                session.commit()


@pytest.mark.integration
def test_outer_transaction_rollback_removes_all_changes(test_engine):
    SessionFactory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
    )

    user_id = None
    list_id = None

    # Create a committed user visible to independent sessions.
    with SessionFactory() as session:
        user = User(
            email=f"outer-rollback-{uuid4().hex}@example.com",
            password_hash="test-hash",
        )
        session.add(user)
        session.commit()
        user_id = user.id

    try:
        with SessionFactory() as session:
            try:
                with session.begin():
                    shopping_list = create_shopping_list(
                        session,
                        "Outer Rollback Test",
                        owner_user_id=user_id,
                    )
                    list_id = shopping_list.id

                    add_shopping_list_item(
                        session,
                        list_id,
                        product_name="Rice",
                        quantity=Decimal("2"),
                        unit="kg",
                        created_by_user_id=user_id,
                    )

                    raise RuntimeError("Simulated outer failure")

            except RuntimeError as exc:
                assert str(exc) == "Simulated outer failure"

        # Verify through a new, independent session.
        with SessionFactory() as session:
            list_count = session.scalar(
                select(func.count())
                .select_from(ShoppingList)
                .where(ShoppingList.id == list_id)
            )

            item_count = session.scalar(
                select(func.count())
                .select_from(ShoppingListItem)
                .where(ShoppingListItem.list_id == list_id)
            )

        assert list_count == 0
        assert item_count == 0

    finally:
        # Clean up only this test's data.
        with SessionFactory() as session:
            if list_id is not None:
                session.execute(
                    delete(ShoppingListItem).where(
                        ShoppingListItem.list_id == list_id
                    )
                )
                session.execute(
                    delete(ShoppingList).where(
                        ShoppingList.id == list_id
                    )
                )

            if user_id is not None:
                session.execute(
                    delete(User).where(User.id == user_id)
                )

            session.commit()


@pytest.mark.integration
def test_committed_list_and_items_are_visible_in_new_session(test_engine):
    SessionFactory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
    )

    user_id = None
    list_id = None

    with SessionFactory() as session:
        user = User(
            email=f"commit-test-{uuid4().hex}@example.com",
            password_hash="test-hash",
        )
        session.add(user)
        session.commit()
        user_id = user.id

    try:
        with SessionFactory() as session:
            with session.begin():
                shopping_list = create_shopping_list(
                    session,
                    "Commit Test List",
                    owner_user_id=user_id,
                )
                list_id = shopping_list.id

                add_shopping_list_item(
                    session,
                    list_id,
                    product_name="Rice",
                    quantity=Decimal("2"),
                    unit="kg",
                    created_by_user_id=user_id,
                )

        # The transaction has committed when session.begin() exits.
        # Verify persisted data through an independent session.
        with SessionFactory() as session:
            saved_list = session.get(ShoppingList, list_id)

            saved_items = session.scalars(
                select(ShoppingListItem).where(
                    ShoppingListItem.list_id == list_id
                )
            ).all()

            assert saved_list is not None
            assert saved_list.name == "Commit Test List"
            assert len(saved_items) == 1
            assert saved_items[0].product_name == "Rice"
            assert saved_items[0].quantity == Decimal("2.000")
            assert saved_items[0].unit == "kg"

    finally:
        with SessionFactory() as session:
            if list_id is not None:
                session.execute(
                    delete(ShoppingListItem).where(
                        ShoppingListItem.list_id == list_id
                    )
                )
                session.execute(
                    delete(ShoppingList).where(
                        ShoppingList.id == list_id
                    )
                )

            if user_id is not None:
                session.execute(
                    delete(User).where(User.id == user_id)
                )

            session.commit()


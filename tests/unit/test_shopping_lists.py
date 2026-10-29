from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.models import Base, ShoppingList, User, Household, Product
from backend.app.services.shopping_lists import (
    ShoppingListItemNotFoundError,
    ShoppingListNotFoundError,
    ShoppingListAlreadyExistsError,
    add_shopping_list_item,
    archive_shopping_list,
    archive_shopping_list_item,
    create_shopping_list,
    update_item_status,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def create_test_list(db):
    user = User(
        email="test@example.com",
        password_hash="test-hash",
    )
    db.add(user)
    db.flush()

    shopping_list = create_shopping_list(
        db,
        "Weekly groceries",
        owner_user_id=user.id,
    )
    db.flush()

    return shopping_list


def create_test_product(db, default_unit=None):
    product = Product(
        name="Milk",
        normalized_name="milk",
        default_unit=default_unit,
        category="dairy",
    )
    db.add(product)
    db.flush()
    return product



def create_test_item(db, shopping_list_id):
    return add_shopping_list_item(
        db,
        shopping_list_id,
        product_name="Rice",
        quantity=Decimal("2"),
        unit="kg",
    )


def test_cannot_add_item_to_archived_list(db):
    shopping_list = create_test_list(db)

    archive_shopping_list(db, shopping_list.id)

    with pytest.raises(ShoppingListNotFoundError):
        create_test_item(db, shopping_list.id)


def test_cannot_update_archived_item_status(db):
    shopping_list = create_test_list(db)
    item = create_test_item(db, shopping_list.id)

    archive_shopping_list_item(db, item.id)

    with pytest.raises(ShoppingListItemNotFoundError):
        update_item_status(db, item.id, "purchased")


def test_archiving_list_preserves_previously_archived_item(db):
    shopping_list = create_test_list(db)
    archived_item = create_test_item(db, shopping_list.id)
    active_item = add_shopping_list_item(
        db,
        shopping_list.id,
        product_name="Milk",
        quantity=Decimal("1"),
        unit="liter",
    )

    archive_shopping_list_item(db, archived_item.id)
    original_archive_time = archived_item.archived_at

    archive_shopping_list(db, shopping_list.id)

    assert archived_item.archived_at == original_archive_time
    assert active_item.archived_at is not None
    assert shopping_list.archived_at is not None
    assert active_item.archived_at == shopping_list.archived_at


def test_cannot_archive_already_archived_list_again(db):
    shopping_list = create_test_list(db)

    archive_shopping_list(db, shopping_list.id)

    with pytest.raises(ShoppingListNotFoundError):
        archive_shopping_list(db, shopping_list.id)


def test_create_shopping_list_rejects_empty_name(db):
    user = User(
        email="empty-list-name@example.com",
        password_hash="test-hash",
    )
    db.add(user)
    db.flush()

    with pytest.raises(
        ValueError,
        match="Shopping list name cannot be empty.",
    ):
        create_shopping_list(
            db,
            "   ",
            owner_user_id=user.id,
        )

    shopping_lists = db.scalars(
        select(ShoppingList).where(
            ShoppingList.owner_user_id == user.id
        )
    ).all()

    assert shopping_lists == []


@pytest.mark.parametrize(
    "ownership",
    [
        {},
        {
            "owner_user_id": 1,
            "household_id": 1,
        },
    ],
)
def test_create_shopping_list_requires_exactly_one_owner(
    db,
    ownership,
):
    with pytest.raises(
        ValueError,
        match="Exactly one of owner_user_id or household_id must be provided.",
    ):
        create_shopping_list(
            db,
            "Weekly groceries",
            **ownership,
        )


def test_create_shopping_list_rejects_name_over_100_characters(db):
    user = User(
        email="long-name@example.com",
        password_hash="test-hash",
    )
    db.add(user)
    db.flush()

    with pytest.raises(
        ValueError,
        match="Shopping list name cannot exceed 100 characters.",
    ):
        create_shopping_list(
            db,
            "A" * 101,
            owner_user_id=user.id,
        )


def test_cannot_create_second_active_personal_list(db):
    existing_list = create_test_list(db)

    with pytest.raises(ShoppingListAlreadyExistsError):
        create_shopping_list(
            db,
            "Another grocery list",
            owner_user_id=existing_list.owner_user_id,
        )


def test_cannot_create_second_active_household_list(db):
    household = Household(name="Test Household")
    db.add(household)
    db.flush()

    create_shopping_list(
        db,
        "Weekly groceries",
        household_id=household.id,
    )

    with pytest.raises(ShoppingListAlreadyExistsError):
        create_shopping_list(
            db,
            "Another grocery list",
            household_id=household.id,
        )

def test_add_item_rejects_empty_product_name(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Product name cannot be empty.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="   ",
            quantity=Decimal("1"),
            unit="kg",
        )

def test_add_item_rejects_product_name_over_200_characters(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Product name cannot exceed 200 characters.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="A" * 201,
            quantity=Decimal("1"),
            unit="kg",
        )


def test_add_item_rejects_empty_unit(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Unit is required when the product has no default unit.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("1"),
            unit="   ",
        )


def test_add_item_rejects_zero_quantity(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Quantity must be a finite number greater than zero.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("0"),
            unit="kg",
        )
def test_add_item_rejects_negative_quantity(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Quantity must be a finite number greater than zero.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("-1"),
            unit="kg",
        )
def test_add_item_rejects_nan_quantity(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Quantity must be a finite number greater than zero.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("NaN"),
            unit="kg",
        )
def test_add_item_rejects_quantity_above_supported_range(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Quantity exceeds the supported Numeric\\(12, 3\\) range.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("1000000000"),
            unit="kg",
        )
def test_add_item_rejects_quantity_with_more_than_three_decimal_places(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Quantity cannot have more than 3 decimal places.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("1.0001"),
            unit="kg",
        )
def test_add_item_rejects_invalid_status(db):
    shopping_list = create_test_list(db)

    with pytest.raises(
        ValueError,
        match="Status must be 'needed' or 'purchased'.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("2"),
            unit="kg",
            status="completed",
        )
def test_update_item_status_successfully(db):
    shopping_list = create_test_list(db)
    item = create_test_item(db, shopping_list.id)

    updated_item = update_item_status(
        db,
        item.id,
        status="purchased",
    )

    assert updated_item.status == "purchased"
    assert item.status == "purchased"
def test_update_item_status_rejects_invalid_status(db):
    shopping_list = create_test_list(db)
    item = create_test_item(db, shopping_list.id)

    with pytest.raises(
        ValueError,
        match="Status must be 'needed' or 'purchased'.",
    ):
        update_item_status(
            db,
            item.id,
            status="completed",
        )
def test_archive_item_not_found_raises_error(db):
    with pytest.raises(
        ShoppingListItemNotFoundError,
        match=r"Active shopping-list item 999 was not found\.",
    ):
        archive_shopping_list_item(db, item_id=999)


def test_add_item_uses_product_default_unit(db):
    shopping_list = create_test_list(db)
    product = create_test_product(db, default_unit="liter")

    item = add_shopping_list_item(
        db,
        shopping_list.id,
        product_id=product.id,
        quantity=Decimal("2"),
    )

    assert item.product_id == product.id
    assert item.product_name == "Milk"
    assert item.unit == "liter"


def test_add_item_allows_supported_unit_override(db):
    shopping_list = create_test_list(db)
    product = create_test_product(db, default_unit="liter")

    item = add_shopping_list_item(
        db,
        shopping_list.id,
        product_id=product.id,
        quantity=Decimal("2"),
        unit="ml",
    )

    assert item.product_id == product.id
    assert item.unit == "ml"


def test_add_item_rejects_unsupported_unit(db):
    shopping_list = create_test_list(db)

    with pytest.raises(ValueError, match="Unsupported unit"):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("1"),
            unit="box",
        )


def test_add_item_requires_unit_without_product_default(db):
    shopping_list = create_test_list(db)
    product = create_test_product(db)

    with pytest.raises(
        ValueError,
        match="Unit is required when the product has no default unit.",
    ):
        add_shopping_list_item(
            db,
            shopping_list.id,
            product_id=product.id,
            quantity=Decimal("1"),
        )

from decimal import Decimal
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
#from sqlalchemy.exc import IntegrityError

from backend.app.db.models import (
    Household,
    HouseholdMember,
    ShoppingListItem,
    User,
)
from backend.app.services.shopping_lists import (
    ShoppingListAlreadyExistsError,
    ShoppingListItemNotFoundError,
    ShoppingListNotFoundError,
    add_shopping_list_item,
    archive_shopping_list,
    archive_shopping_list_item,
    create_shopping_list,
    get_shopping_list,
    update_item_status,
)


@pytest.mark.integration
def test_create_personal_shopping_list(db_session: Session):
    user = User(
        email="service-personal@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "My Shopping List",
        owner_user_id=user.id,
    )

    assert shopping_list.id is not None
    assert shopping_list.name == "My Shopping List"
    assert shopping_list.owner_user_id == user.id
    assert shopping_list.household_id is None


@pytest.mark.integration
def test_create_shared_shopping_list(db_session: Session):
    user = User(
        email="service-shared@example.com",
        password_hash="test-hash",
    )

    household = Household(
        name="Service Test Household",
    )

    db_session.add_all([user, household])
    db_session.flush()

    membership = HouseholdMember(
        household_id=household.id,
        user_id=user.id,
        role="owner",
    )

    db_session.add(membership)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Household Shopping List",
        household_id=household.id,
    )

    assert shopping_list.id is not None
    assert shopping_list.name == "Household Shopping List"
    assert shopping_list.household_id == household.id
    assert shopping_list.owner_user_id is None


@pytest.mark.integration
def test_shopping_list_requires_exactly_one_owner(db_session: Session):
    user = User(
        email="service-owner@example.com",
        password_hash="test-hash",
    )

    household = Household(
        name="Owner Constraint Household",
    )

    db_session.add_all([user, household])
    db_session.flush()

    with pytest.raises(ValueError):
        create_shopping_list(
            db_session,
            "Invalid List",
        )

    with pytest.raises(ValueError):
        create_shopping_list(
            db_session,
            "Invalid List",
            owner_user_id=user.id,
            household_id=household.id,
        )


@pytest.mark.integration
def test_personal_shopping_list_cannot_be_duplicated(db_session: Session):
    user = User(
        email="service-unique-personal@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    create_shopping_list(
        db_session,
        "First List",
        owner_user_id=user.id,
    )

    with pytest.raises(ShoppingListAlreadyExistsError):
        create_shopping_list(
            db_session,
            "Second List",
            owner_user_id=user.id,
        )


@pytest.mark.integration
def test_shared_shopping_list_cannot_be_duplicated(db_session: Session):
    household = Household(
        name="Service Unique Household",
    )

    db_session.add(household)
    db_session.flush()

    create_shopping_list(
        db_session,
        "First Shared List",
        household_id=household.id,
    )

    with pytest.raises(ShoppingListAlreadyExistsError):
        create_shopping_list(
            db_session,
            "Second Shared List",
            household_id=household.id,
        )


@pytest.mark.integration
def test_get_shopping_list(db_session: Session):
    user = User(
        email="service-get@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    created_list = create_shopping_list(
        db_session,
        "Get Test List",
        owner_user_id=user.id,
    )

    shopping_list = get_shopping_list(
        db_session,
        created_list.id,
    )

    assert shopping_list.id == created_list.id
    assert shopping_list.name == "Get Test List"


@pytest.mark.integration
def test_get_archived_shopping_list_raises_error(db_session: Session):
    user = User(
        email="service-archived@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Archived List",
        owner_user_id=user.id,
    )

    archive_shopping_list(
        db_session,
        shopping_list.id,
    )

    with pytest.raises(ShoppingListNotFoundError):
        get_shopping_list(
            db_session,
            shopping_list.id,
        )


@pytest.mark.integration
def test_add_shopping_list_item(db_session: Session):
    user = User(
        email="service-item@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Item Test List",
        owner_user_id=user.id,
    )

    item = add_shopping_list_item(
        db_session,
        shopping_list.id,
        product_name="Rice",
        quantity=Decimal("5.000"),
        unit="kg",
        created_by_user_id=user.id,
    )

    assert item.id is not None
    assert item.list_id == shopping_list.id
    assert item.product_name == "Rice"
    assert item.quantity == Decimal("5.000")
    assert item.unit == "kg"
    assert item.status == "needed"
    assert item.archived_at is None


@pytest.mark.integration
def test_item_quantity_must_be_positive(db_session: Session):
    user = User(
        email="service-quantity@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Quantity Test List",
        owner_user_id=user.id,
    )

    with pytest.raises(ValueError):
        add_shopping_list_item(
            db_session,
            shopping_list.id,
            product_name="Milk",
            quantity=Decimal("0"),
            unit="liter",
            created_by_user_id=user.id,
        )

    with pytest.raises(ValueError):
        add_shopping_list_item(
            db_session,
            shopping_list.id,
            product_name="Milk",
            quantity=Decimal("-1"),
            unit="liter",
            created_by_user_id=user.id,
        )


@pytest.mark.integration
def test_item_status_can_be_updated(db_session: Session):
    user = User(
        email="service-status@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Status Test List",
        owner_user_id=user.id,
    )

    item = add_shopping_list_item(
        db_session,
        shopping_list.id,
        product_name="Bread",
        quantity=Decimal("2"),
        unit="piece",
        created_by_user_id=user.id,
    )

    assert item.status == "needed"

    updated_item = update_item_status(
        db_session,
        item.id,
        "purchased",
    )

    assert updated_item.status == "purchased"


@pytest.mark.integration
def test_invalid_item_status_raises_error(db_session: Session):
    user = User(
        email="service-invalid-status@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Invalid Status List",
        owner_user_id=user.id,
    )

    item = add_shopping_list_item(
        db_session,
        shopping_list.id,
        product_name="Bread",
        quantity=Decimal("2"),
        unit="piece",
        created_by_user_id=user.id,
    )

    with pytest.raises(ValueError):
        update_item_status(
            db_session,
            item.id,
            "invalid_status",
        )


@pytest.mark.integration
def test_archive_shopping_list_item(db_session: Session):
    user = User(
        email="service-archive-item@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Archive Item List",
        owner_user_id=user.id,
    )

    item = add_shopping_list_item(
        db_session,
        shopping_list.id,
        product_name="Sugar",
        quantity=Decimal("1"),
        unit="kg",
        created_by_user_id=user.id,
    )

    archived_item = archive_shopping_list_item(
        db_session,
        item.id,
    )

    assert archived_item.archived_at is not None

    with pytest.raises(ShoppingListItemNotFoundError):
        update_item_status(
            db_session,
            item.id,
            "purchased",
        )


@pytest.mark.integration
def test_archive_shopping_list_archives_active_items(db_session: Session):
    user = User(
        email="service-archive-list@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Archive List",
        owner_user_id=user.id,
    )

    item_1 = add_shopping_list_item(
        db_session,
        shopping_list.id,
        product_name="Rice",
        quantity=Decimal("5"),
        unit="kg",
        created_by_user_id=user.id,
    )

    item_2 = add_shopping_list_item(
        db_session,
        shopping_list.id,
        product_name="Milk",
        quantity=Decimal("2"),
        unit="liter",
        created_by_user_id=user.id,
    )

    archived_list = archive_shopping_list(
        db_session,
        shopping_list.id,
    )

    assert archived_list.archived_at is not None
    assert item_1.archived_at is not None
    assert item_2.archived_at is not None

    with pytest.raises(ShoppingListNotFoundError):
        get_shopping_list(
            db_session,
            shopping_list.id,
        )

@pytest.mark.integration
def test_create_new_personal_shopping_list_after_archiving_old_one(
    db_session: Session,
):
    user = User(
        email="service-replacement-list@example.com",
        password_hash="test-hash",
    )

    db_session.add(user)
    db_session.flush()

    # 1. Create the original active list.
    old_list = create_shopping_list(
        db_session,
        "Old Shopping List",
        owner_user_id=user.id,
    )

    # 2. Archive the original list.
    archived_list = archive_shopping_list(
        db_session,
        old_list.id,
    )

    assert archived_list.archived_at is not None

    # 3. Create a replacement list for the same user.
    new_list = create_shopping_list(
        db_session,
        "New Shopping List",
        owner_user_id=user.id,
    )

    assert new_list.id != old_list.id
    assert new_list.owner_user_id == user.id
    assert new_list.archived_at is None

    # 4. The user still cannot have two active lists.
    with pytest.raises(ShoppingListAlreadyExistsError):
        create_shopping_list(
            db_session,
            "Another Active List",
            owner_user_id=user.id,
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("quantity", "should_be_valid"),
    [
        (Decimal("1.2300"), True),
        (Decimal("5.0000"), True),
        (Decimal("1.2345"), False),
    ],
)
def test_quantity_decimal_precision(
    db_session: Session,
    quantity,
    should_be_valid,
):
    user = User(
        email=f"decimal-{str(quantity).replace('.', '-')}"
        "@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Decimal Precision List",
        owner_user_id=user.id,
    )

    if should_be_valid:
        item = add_shopping_list_item(
            db_session,
            shopping_list.id,
            product_name="Test Product",
            quantity=quantity,
            unit="piece",
            created_by_user_id=user.id,
        )
        assert item.quantity == quantity
    else:
        with pytest.raises(ValueError):
            add_shopping_list_item(
                db_session,
                shopping_list.id,
                product_name="Test Product",
                quantity=quantity,
                unit="piece",
                created_by_user_id=user.id,
            )

@pytest.mark.integration
@pytest.mark.parametrize(
    ("quantity", "should_be_valid"),
    [
        (Decimal("0"), False),
        (Decimal("-1"), False),
        (Decimal("NaN"), False),
        (Decimal("Infinity"), False),
        (Decimal("1.2300"), True),
        (Decimal("1.2345"), False),
        (Decimal("999999999.999"), True),
        (Decimal("1000000000"), False),
    ],
)
def test_quantity_boundary_values(
    db_session: Session,
    quantity,
    should_be_valid,
):
    user = User(
        email=f"boundary-{str(quantity).replace('.', '-')}"
        "@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Quantity Boundary List",
        owner_user_id=user.id,
    )

    if should_be_valid:
        item = add_shopping_list_item(
            db_session,
            shopping_list.id,
            product_name="Test Product",
            quantity=quantity,
            unit="piece",
            created_by_user_id=user.id,
        )
        assert item.quantity == quantity
    else:
        with pytest.raises(ValueError):
            add_shopping_list_item(
                db_session,
                shopping_list.id,
                product_name="Test Product",
                quantity=quantity,
                unit="piece",
                created_by_user_id=user.id,
            )

@pytest.mark.integration
def test_invalid_quantity_does_not_add_item(db_session: Session):
    user = User(
        email="invalid-quantity@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = create_shopping_list(
        db_session,
        "Invalid Quantity Test List",
        owner_user_id=user.id,
    )

    with pytest.raises(ValueError):
        add_shopping_list_item(
            db_session,
            shopping_list.id,
            product_name="Rice",
            quantity=Decimal("1.2345"),
            unit="kg",
            created_by_user_id=user.id,
        )

    items = db_session.scalars(
      select(ShoppingListItem).where(
        ShoppingListItem.list_id == shopping_list.id
      )
    ).all()

    assert items == []



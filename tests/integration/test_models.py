
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import (
    Household,
    HouseholdMember,
    ShoppingList,
    ShoppingListItem,
    User,
)


@pytest.mark.integration
def test_create_user_and_household(db_session):
    user = User(
        email="owner@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    household = Household(name="Test Household")
    db_session.add(household)
    db_session.flush()

    membership = HouseholdMember(
        household_id=household.id,
        user_id=user.id,
        role="owner",
    )
    db_session.add(membership)
    db_session.flush()

    assert user.id is not None
    assert household.id is not None
    assert membership.id is not None
    assert membership.role == "owner"


@pytest.mark.integration
def test_user_can_belong_to_only_one_household(db_session):
    user = User(
        email="single-household@example.com",
        password_hash="test-hash",
    )
    household_one = Household(name="Household One")
    household_two = Household(name="Household Two")

    db_session.add_all([user, household_one, household_two])
    db_session.flush()

    db_session.add(
        HouseholdMember(
            household_id=household_one.id,
            user_id=user.id,
            role="owner",
        )
    )
    db_session.flush()

    db_session.add(
        HouseholdMember(
            household_id=household_two.id,
            user_id=user.id,
            role="member",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_household_cannot_have_duplicate_membership(db_session):
    user = User(
        email="duplicate-member@example.com",
        password_hash="test-hash",
    )
    household = Household(name="Duplicate Membership Household")

    db_session.add_all([user, household])
    db_session.flush()

    membership = HouseholdMember(
        household_id=household.id,
        user_id=user.id,
        role="owner",
    )
    db_session.add(membership)
    db_session.flush()

    duplicate_membership = HouseholdMember(
        household_id=household.id,
        user_id=user.id,
        role="member",
    )
    db_session.add(duplicate_membership)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_household_member_role_constraint(db_session):
    user = User(
        email="invalid-role@example.com",
        password_hash="test-hash",
    )
    household = Household(name="Role Constraint Household")

    db_session.add_all([user, household])
    db_session.flush()

    membership = HouseholdMember(
        household_id=household.id,
        user_id=user.id,
        role="superuser",
    )
    db_session.add(membership)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_personal_shopping_list(db_session):
    user = User(
        email="personal-list@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = ShoppingList(
        owner_user_id=user.id,
        household_id=None,
        name="Personal Shopping List",
    )
    db_session.add(shopping_list)
    db_session.flush()

    assert shopping_list.id is not None
    assert shopping_list.owner_user_id == user.id
    assert shopping_list.household_id is None


@pytest.mark.integration
def test_shared_shopping_list(db_session):
    household = Household(name="Shared List Household")
    db_session.add(household)
    db_session.flush()

    shopping_list = ShoppingList(
        owner_user_id=None,
        household_id=household.id,
        name="Shared Shopping List",
    )
    db_session.add(shopping_list)
    db_session.flush()

    assert shopping_list.id is not None
    assert shopping_list.household_id == household.id
    assert shopping_list.owner_user_id is None


@pytest.mark.integration
def test_shopping_list_must_have_exactly_one_owner(db_session):
    user = User(
        email="list-owner@example.com",
        password_hash="test-hash",
    )
    household = Household(name="List Ownership Household")

    db_session.add_all([user, household])
    db_session.flush()

    invalid_list = ShoppingList(
        owner_user_id=user.id,
        household_id=household.id,
        name="Invalid Shopping List",
    )
    db_session.add(invalid_list)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_personal_shopping_list_is_unique_per_user(db_session):
    user = User(
        email="unique-personal-list@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    first_list = ShoppingList(
        owner_user_id=user.id,
        name="First Personal List",
    )
    second_list = ShoppingList(
        owner_user_id=user.id,
        name="Second Personal List",
    )

    db_session.add(first_list)
    db_session.flush()

    db_session.add(second_list)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_shared_shopping_list_is_unique_per_household(db_session):
    household = Household(name="Unique Shared List Household")
    db_session.add(household)
    db_session.flush()

    first_list = ShoppingList(
        household_id=household.id,
        name="First Shared List",
    )
    second_list = ShoppingList(
        household_id=household.id,
        name="Second Shared List",
    )

    db_session.add(first_list)
    db_session.flush()

    db_session.add(second_list)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_shopping_list_item(db_session):
    user = User(
        email="shopping-item@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = ShoppingList(
        owner_user_id=user.id,
        name="Item Test List",
    )
    db_session.add(shopping_list)
    db_session.flush()

    item = ShoppingListItem(
        list_id=shopping_list.id,
        product_name="Rice",
        quantity=Decimal("5"),
        unit="kg",
        created_by_user_id=user.id,
    )
    db_session.add(item)
    db_session.flush()

    assert item.id is not None
    assert item.list_id == shopping_list.id
    assert item.product_name == "Rice"
    assert item.quantity == Decimal("5")
    assert item.unit == "kg"
    assert item.status == "needed"


@pytest.mark.integration
def test_shopping_list_item_quantity_must_be_positive(db_session):
    user = User(
        email="invalid-quantity@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = ShoppingList(
        owner_user_id=user.id,
        name="Quantity Constraint List",
    )
    db_session.add(shopping_list)
    db_session.flush()

    invalid_item = ShoppingListItem(
        list_id=shopping_list.id,
        product_name="Invalid Quantity Item",
        quantity=Decimal("0"),
        unit="kg",
        created_by_user_id=user.id,
    )
    db_session.add(invalid_item)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_shopping_list_item_status_constraint(db_session):
    user = User(
        email="invalid-status@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = ShoppingList(
        owner_user_id=user.id,
        name="Status Constraint List",
    )
    db_session.add(shopping_list)
    db_session.flush()

    invalid_item = ShoppingListItem(
        list_id=shopping_list.id,
        product_name="Invalid Status Item",
        quantity=Decimal("1"),
        unit="piece",
        status="pending",
        created_by_user_id=user.id,
    )
    db_session.add(invalid_item)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


@pytest.mark.integration
def test_shopping_list_item_can_be_archived_without_deletion(
    db_session,
):
    user = User(
        email="archive-item@example.com",
        password_hash="test-hash",
    )
    db_session.add(user)
    db_session.flush()

    shopping_list = ShoppingList(
        owner_user_id=user.id,
        name="Archive Test List",
    )
    db_session.add(shopping_list)
    db_session.flush()

    item = ShoppingListItem(
        list_id=shopping_list.id,
        product_name="Rice",
        quantity=Decimal("5"),
        unit="kg",
        created_by_user_id=user.id,
    )
    db_session.add(item)
    db_session.flush()

    item_id = item.id
    item.archived_at = datetime.now()
    db_session.flush()

    db_session.expire_all()

    archived_item = db_session.get(
        ShoppingListItem,
        item_id,
    )

    assert archived_item is not None
    assert archived_item.archived_at is not None
    assert archived_item.product_name == "Rice"

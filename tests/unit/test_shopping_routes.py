
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
from backend.app.api.routes.auth import get_current_user
from backend.app.db.database import get_db
from backend.app.db.models import (
    Base,
    Household,
    HouseholdMember,
    ShoppingList,
    ShoppingListItem,
    User,
)
from backend.app.main import app


@pytest.fixture
def api_env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    TestingSession = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    db = TestingSession()
    active_user = {"user": None}

    # Preserve overrides installed by other tests.
    previous_overrides = app.dependency_overrides.copy()

    def override_get_db():
        yield db

    def override_current_user():
        return active_user["user"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = (
        override_current_user
    )

    try:
        with TestClient(app) as client:
            yield client, db, active_user
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def make_user(email: str) -> User:
    return User(
        email=email,
        password_hash="test-hash",
    )


def make_item(
    shopping_list_id: int,
    created_by_user_id: int,
) -> ShoppingListItem:
    return ShoppingListItem(
        list_id=shopping_list_id,
        product_name="Milk",
        quantity=1,
        unit="liter",
        status="needed",
        created_by_user_id=created_by_user_id,
    )


def test_personal_list_owner_is_taken_from_authenticated_user(
    api_env,
):
    client, db, active_user = api_env

    user_a = make_user("a@example.com")
    user_b = make_user("b@example.com")
    db.add_all([user_a, user_b])
    db.commit()

    active_user["user"] = user_a

    response = client.post(
        "/api/v1/shopping-lists",
        json={
            "name": "Groceries",
            "owner_user_id": user_b.id,
        },
    )

    assert response.status_code == 201

    created_list = db.scalar(
        select(ShoppingList).where(
            ShoppingList.name == "Groceries"
        )
    )

    assert created_list is not None
    assert created_list.owner_user_id == user_a.id
    assert created_list.owner_user_id != user_b.id


def test_user_cannot_read_another_users_personal_list(
    api_env,
):
    client, db, active_user = api_env

    owner = make_user("owner@example.com")
    outsider = make_user("outsider@example.com")
    db.add_all([owner, outsider])
    db.flush()

    shopping_list = ShoppingList(
        name="Private groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = outsider

    response = client.get(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 404


def test_household_member_can_read_shared_list(api_env):
    client, db, active_user = api_env

    user = make_user("member@example.com")
    household = Household(name="Family")
    db.add_all([user, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=user.id,
            role="owner",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = user

    response = client.get(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Family groceries"


def test_non_member_cannot_read_shared_list(api_env):
    client, db, active_user = api_env

    member = make_user("member@example.com")
    outsider = make_user("outsider@example.com")
    household = Household(name="Family")

    db.add_all([member, outsider, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="owner",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = outsider

    response = client.get(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 404


def test_outsider_cannot_add_item_to_personal_list(api_env):
    client, db, active_user = api_env

    owner = make_user("owner@example.com")
    outsider = make_user("outsider@example.com")
    db.add_all([owner, outsider])
    db.flush()

    shopping_list = ShoppingList(
        name="Private groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = outsider

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": "1.000",
            "unit": "liter",
            "status": "needed",
        },
    )

    assert response.status_code == 404

    items = db.scalars(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == shopping_list.id
        )
    ).all()

    assert items == []


def test_outsider_cannot_update_item_status(api_env):
    client, db, active_user = api_env

    owner = make_user("owner@example.com")
    outsider = make_user("outsider@example.com")
    db.add_all([owner, outsider])
    db.flush()

    shopping_list = ShoppingList(
        name="Private groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    active_user["user"] = outsider

    response = client.patch(
        f"/api/v1/shopping-list-items/{item.id}/status",
        json={"status": "purchased"},
    )

    assert response.status_code == 404

    db.refresh(item)
    assert item.status == "needed"


def test_outsider_cannot_archive_another_users_item(api_env):
    client, db, active_user = api_env

    owner = make_user("owner@example.com")
    outsider = make_user("outsider@example.com")
    db.add_all([owner, outsider])
    db.flush()

    shopping_list = ShoppingList(
        name="Private groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    active_user["user"] = outsider

    response = client.delete(
        f"/api/v1/shopping-list-items/{item.id}"
    )

    assert response.status_code == 404

    db.refresh(item)
    assert item.archived_at is None


def test_outsider_cannot_archive_another_users_list(api_env):
    client, db, active_user = api_env

    owner = make_user("owner@example.com")
    outsider = make_user("outsider@example.com")
    db.add_all([owner, outsider])
    db.flush()

    shopping_list = ShoppingList(
        name="Private groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = outsider

    response = client.delete(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 404

    db.refresh(shopping_list)
    assert shopping_list.archived_at is None


def test_non_member_cannot_add_item_to_shared_list(api_env):
    client, db, active_user = api_env

    member = make_user("member@example.com")
    outsider = make_user("outsider@example.com")
    household = Household(name="Family")

    db.add_all([member, outsider, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="owner",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = outsider

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": "1.000",
            "unit": "liter",
            "status": "needed",
        },
    )

    assert response.status_code == 404

    items = db.scalars(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == shopping_list.id
        )
    ).all()

    assert items == []

def test_owner_can_add_item_to_personal_list(api_env):
    client, db, active_user = api_env

    owner = make_user("add-owner@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = owner

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": "2.000",
            "unit": "liter",
            "status": "needed",
        },
    )

    assert response.status_code == 201
    assert response.json()["product_name"] == "Milk"
    assert response.json()["status"] == "needed"

    item = db.scalar(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == shopping_list.id
        )
    )

    assert item is not None
    assert item.quantity == 2
    assert item.unit == "liter"
    assert item.created_by_user_id == owner.id


def test_owner_can_update_item_status(api_env):
    client, db, active_user = api_env

    owner = make_user("status-owner@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    active_user["user"] = owner

    response = client.patch(
        f"/api/v1/shopping-list-items/{item.id}/status",
        json={"status": "purchased"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "purchased"

    db.refresh(item)
    assert item.status == "purchased"


def test_owner_can_archive_item_without_permanent_deletion(
    api_env,
):
    client, db, active_user = api_env

    owner = make_user("archive-item@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    item_id = item.id
    list_id = shopping_list.id
    active_user["user"] = owner

    response = client.delete(
        f"/api/v1/shopping-list-items/{item_id}"
    )

    assert response.status_code == 204

    db.refresh(item)
    assert item.archived_at is not None

    # The item remains in the database but is hidden from active results.
    stored_item = db.get(ShoppingListItem, item_id)
    assert stored_item is not None

    response = client.get(
        f"/api/v1/shopping-lists/{list_id}/items"
    )

    assert response.status_code == 200
    assert all(
        row["id"] != item_id for row in response.json()
    )


def test_owner_can_archive_list_and_its_active_items(api_env):
    client, db, active_user = api_env

    owner = make_user("archive-list@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    list_id = shopping_list.id
    active_user["user"] = owner

    response = client.delete(
        f"/api/v1/shopping-lists/{list_id}"
    )

    assert response.status_code == 204

    db.refresh(shopping_list)
    db.refresh(item)

    assert shopping_list.archived_at is not None
    assert item.archived_at is not None

    # Both records remain stored.
    assert db.get(ShoppingList, list_id) is not None
    assert db.get(ShoppingListItem, item.id) is not None


def test_archived_list_cannot_be_read_or_modified(api_env):
    client, db, active_user = api_env

    owner = make_user("inactive-list@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Archived groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    list_id = shopping_list.id
    active_user["user"] = owner

    # Archive the list through the public API.
    archive_response = client.delete(
        f"/api/v1/shopping-lists/{list_id}"
    )
    assert archive_response.status_code == 204

    # An archived list must no longer be readable.
    read_response = client.get(
        f"/api/v1/shopping-lists/{list_id}"
    )
    assert read_response.status_code == 404

    # Adding an item to it must also be rejected.
    add_response = client.post(
        f"/api/v1/shopping-lists/{list_id}/items",
        json={
            "product_name": "Bread",
            "quantity": "1.000",
            "unit": "piece",
            "status": "needed",
        },
    )
    assert add_response.status_code == 404

    items = db.scalars(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == list_id
        )
    ).all()
    assert items == []

def test_owner_can_add_item_already_marked_as_purchased(api_env):
    client, db, active_user = api_env

    owner = make_user("purchased-item@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = owner

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Bread",
            "quantity": "1.000",
            "unit": "piece",
            "status": "purchased",
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "purchased"

    item = db.scalar(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == shopping_list.id
        )
    )

    assert item is not None
    assert item.status == "purchased"

def test_member_loses_access_after_membership_is_removed(api_env):
    client, db, active_user = api_env

    member = make_user("former-member@example.com")
    household = Household(name="Family")

    db.add_all([member, household])
    db.flush()

    membership = HouseholdMember(
        household_id=household.id,
        user_id=member.id,
        role="member",
    )
    db.add(membership)

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.commit()
    
    item = make_item(shopping_list.id, member.id)
    db.add(item)
    db.commit()

    active_user["user"] = member

    # The member initially has access.
    response = client.get(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )
    assert response.status_code == 200

    # Revoke membership in the database.
    db.delete(membership)
    db.commit()

    # Access must be denied immediately after membership removal.
    response = client.get(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )
    assert response.status_code == 404

    # The former member must not be able to add items either.
    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": "1.000",
            "unit": "liter",
            "status": "needed",
        },
    )
    assert response.status_code == 404

    items = db.scalars(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == shopping_list.id
        )
    ).all()
    assert len(items) == 1
    assert items[0].id == item.id
    # The former member cannot update an existing item.
    response = client.patch(
    f"/api/v1/shopping-list-items/{item.id}/status",
    json={"status": "purchased"},
    )
    assert response.status_code == 404

    # The former member cannot archive an existing item.
    response = client.delete(
    f"/api/v1/shopping-list-items/{item.id}"
    )
    assert response.status_code == 404

    # The item must remain unchanged.
    db.refresh(item)
    assert item.status == "needed"
    assert item.archived_at is None

def test_member_cannot_archive_shared_shopping_list(api_env):
    client, db, active_user = api_env

    member = make_user("member-archive@example.com")
    household = Household(name="Family")
    db.add_all([member, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="member",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = member

    response = client.delete(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 403

    db.refresh(shopping_list)
    assert shopping_list.archived_at is None


def test_admin_can_archive_shared_shopping_list(api_env):
    client, db, active_user = api_env

    admin = make_user("admin-archive@example.com")
    household = Household(name="Family")
    db.add_all([admin, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=admin.id,
            role="admin",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = admin

    response = client.delete(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 204

    db.refresh(shopping_list)
    assert shopping_list.archived_at is not None

def test_member_cannot_create_household_shopping_list(api_env):
    client, db, active_user = api_env

    member = make_user("member-create-list@example.com")
    household = Household(name="Family")
    db.add_all([member, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="member",
        )
    )
    db.commit()

    active_user["user"] = member

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Family groceries"},
    )

    assert response.status_code == 403

    shopping_lists = db.scalars(
        select(ShoppingList).where(
            ShoppingList.household_id == household.id
        )
    ).all()

    assert shopping_lists == []


def test_admin_can_create_household_shopping_list(api_env):
    client, db, active_user = api_env

    admin = make_user("admin-create-list@example.com")
    household = Household(name="Family")
    db.add_all([admin, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=admin.id,
            role="admin",
        )
    )
    db.commit()

    active_user["user"] = admin

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Family groceries"},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Family groceries"

    shopping_list = db.scalar(
        select(ShoppingList).where(
            ShoppingList.household_id == household.id
        )
    )

    assert shopping_list is not None

def test_member_can_add_item_to_shared_shopping_list(api_env):
    client, db, active_user = api_env

    member = make_user("member-add-item@example.com")
    household = Household(name="Family")
    db.add_all([member, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="member",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = member

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": "2.000",
            "unit": "liter",
            "status": "needed",
        },
    )

    assert response.status_code == 201
    assert response.json()["product_name"] == "Milk"

    item = db.scalar(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == shopping_list.id
        )
    )

    assert item is not None
    assert item.created_by_user_id == member.id
    

def test_member_can_update_item_status_in_shared_list(api_env):
    client, db, active_user = api_env

    member = make_user("member-update-status@example.com")
    household = Household(name="Family")
    db.add_all([member, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="member",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, member.id)
    db.add(item)
    db.commit()

    active_user["user"] = member

    response = client.patch(
        f"/api/v1/shopping-list-items/{item.id}/status",
        json={"status": "purchased"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "purchased"

    db.refresh(item)
    assert item.status == "purchased"

def test_member_can_archive_item_in_shared_list(api_env):
    client, db, active_user = api_env

    member = make_user("member-archive@example.com")
    household = Household(name="Family")
    db.add_all([member, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="member",
        )
    )

    shopping_list = ShoppingList(
        name="Family groceries",
        household_id=household.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, member.id)
    db.add(item)
    db.commit()

    active_user["user"] = member
    response = client.delete(
    f"/api/v1/shopping-list-items/{item.id}"
    )

    assert response.status_code == 204

    db.refresh(item)
    assert item.archived_at is not None

    # The item is archived, not permanently deleted.
    assert db.get(ShoppingListItem, item.id) is not None
    
def test_owner_can_create_household_shopping_list(api_env):
    client, db, active_user = api_env

    owner = make_user("owner-create-list@example.com")
    household = Household(name="Family")
    db.add_all([owner, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=owner.id,
            role="owner",
        )
    )
    db.commit()

    active_user["user"] = owner

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Family groceries"},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Family groceries"

    shopping_list = db.scalar(
        select(ShoppingList).where(
            ShoppingList.household_id == household.id
        )
    )

    assert shopping_list is not None

def test_non_member_cannot_create_household_shopping_list(api_env):
    client, db, active_user = api_env

    member = make_user("member-create-list@example.com")
    household = Household(name="Family")
    db.add_all([member, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=member.id,
            role="member",
        )
    )
    db.commit()

    active_user["user"] = member

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Family groceries"},
    )

    assert response.status_code == 403

    shopping_lists = db.scalars(
        select(ShoppingList).where(
            ShoppingList.household_id == household.id
        )
    ).all()

    assert shopping_lists == []

def test_cannot_create_second_active_household_shopping_list(api_env):
    client, db, active_user = api_env

    owner = make_user("second-list-owner@example.com")
    household = Household(name="Family")

    db.add_all([owner, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=owner.id,
            role="owner",
        )
    )

    existing_list = ShoppingList(
        name="Existing groceries",
        household_id=household.id,
    )
    db.add(existing_list)
    db.commit()

    active_user["user"] = owner

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Another groceries"},
    )

    assert response.status_code == 409

    lists = db.scalars(
        select(ShoppingList).where(
            ShoppingList.household_id == household.id,
            ShoppingList.archived_at.is_(None),
        )
    ).all()

    assert len(lists) == 1
    assert lists[0].name == "Existing groceries"

def test_personal_list_integrity_error_returns_409_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    user = make_user("integrity-error@example.com")
    db.add(user)
    db.commit()

    active_user["user"] = user

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError(
            "INSERT INTO shopping_lists",
            {},
            Exception("Simulated integrity violation"),
        )

    monkeypatch.setattr(
        shopping,
        "create_shopping_list",
        raise_integrity_error,
    )

    response = client.post(
        "/api/v1/shopping-lists",
        json={"name": "Groceries"},
    )

    assert response.status_code == 409
    assert rollback_called is True

    # The session must remain usable after rollback.
    assert db.get(User, user.id) is not None

def test_add_item_integrity_error_returns_409_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    user = make_user("item-integrity@example.com")
    db.add(user)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=user.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = user

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError(
            "INSERT INTO shopping_list_items",
            {},
            Exception("Simulated integrity violation"),
        )

    monkeypatch.setattr(
        shopping,
        "add_shopping_list_item",
        raise_integrity_error,
    )

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": "1.000",
            "unit": "liter",
            "status": "needed",
        },
    )

    assert response.status_code == 409
    assert rollback_called is True
    assert db.get(User, user.id) is not None
    
def test_archive_item_integrity_error_returns_409_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    user = make_user("archive-integrity@example.com")
    db.add(user)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=user.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, user.id)
    db.add(item)
    db.commit()

    active_user["user"] = user

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError(
            "UPDATE shopping_list_items",
            {},
            Exception("Simulated integrity violation"),
        )

    monkeypatch.setattr(
        shopping,
        "archive_shopping_list_item",
        raise_integrity_error,
    )

    response = client.delete(
        f"/api/v1/shopping-list-items/{item.id}"
    )

    assert response.status_code == 409
    assert rollback_called is True
    assert db.get(ShoppingListItem, item.id) is not None

def test_commit_integrity_error_returns_409_and_rolls_back(
    api_env,
    monkeypatch,
):
    client, db, active_user = api_env

    user = make_user("commit-integrity@example.com")
    db.add(user)
    db.commit()

    active_user["user"] = user

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    def raise_integrity_error():
        raise IntegrityError(
            "INSERT INTO shopping_lists",
            {},
            Exception("Simulated commit failure"),
        )

    monkeypatch.setattr(db, "rollback", tracked_rollback)
    monkeypatch.setattr(db, "commit", raise_integrity_error)

    response = client.post(
        "/api/v1/shopping-lists",
        json={"name": "Groceries"},
    )

    assert response.status_code == 409
    assert rollback_called is True

    # The session must remain usable after rollback.
    assert db.get(User, user.id) is not None

def test_cannot_create_second_active_personal_list(
    api_env,
    monkeypatch,
):
    client, db, active_user = api_env

    user = make_user("second-personal-list@example.com")
    db.add(user)
    db.flush()

    existing_list = ShoppingList(
        name="Existing groceries",
        owner_user_id=user.id,
    )
    db.add(existing_list)
    db.commit()

    active_user["user"] = user

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.post(
        "/api/v1/shopping-lists",
        json={"name": "Another groceries"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "An active shopping list already exists."
    )
    assert rollback_called is True

    active_lists = db.scalars(
        select(ShoppingList).where(
            ShoppingList.owner_user_id == user.id,
            ShoppingList.archived_at.is_(None),
        )
    ).all()

    assert len(active_lists) == 1
    assert active_lists[0].name == "Existing groceries"


def test_personal_list_value_error_returns_422_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    user = make_user("empty-list-name@example.com")
    db.add(user)
    db.commit()

    active_user["user"] = user

    def raise_value_error(*args, **kwargs):
        raise ValueError("Shopping list name cannot be empty.")

    monkeypatch.setattr(
        shopping,
        "create_shopping_list",
        raise_value_error,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.post(
        "/api/v1/shopping-lists",
        json={"name": "Valid name"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Shopping list name cannot be empty."
    )
    assert rollback_called is True

    assert db.scalars(
        select(ShoppingList).where(
            ShoppingList.owner_user_id == user.id
        )
    ).all() == []


def test_household_list_value_error_returns_422_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    owner = make_user("household-value-error@example.com")
    household = Household(name="Family")
    db.add_all([owner, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=owner.id,
            role="owner",
        )
    )
    db.commit()

    active_user["user"] = owner

    def raise_value_error(*args, **kwargs):
        raise ValueError("Invalid household shopping list name.")

    monkeypatch.setattr(
        shopping,
        "create_shopping_list",
        raise_value_error,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Family groceries"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Invalid household shopping list name."
    )
    assert rollback_called is True

    shopping_lists = db.scalars(
        select(ShoppingList).where(
            ShoppingList.household_id == household.id
        )
    ).all()

    assert shopping_lists == []
def test_household_list_integrity_error_returns_409_and_rolls_back(
    api_env,
    monkeypatch,
):
    from sqlalchemy.exc import IntegrityError
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    owner = make_user("household-integrity-error@example.com")
    household = Household(name="Family")
    db.add_all([owner, household])
    db.flush()

    db.add(
        HouseholdMember(
            household_id=household.id,
            user_id=owner.id,
            role="owner",
        )
    )
    db.commit()

    active_user["user"] = owner

    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError(
            "INSERT",
            {},
            Exception("Simulated integrity error"),
        )

    monkeypatch.setattr(
        shopping,
        "create_shopping_list",
        raise_integrity_error,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Family groceries"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The operation conflicts with existing data."
    )
    assert rollback_called is True
def test_user_without_household_membership_cannot_create_list(
    api_env,
):
    client, db, active_user = api_env

    user = make_user("non-member-create-list@example.com")
    household = Household(name="Family")

    db.add_all([user, household])
    db.commit()

    active_user["user"] = user

    response = client.post(
        f"/api/v1/households/{household.id}/shopping-list",
        json={"name": "Family groceries"},
    )

    assert response.status_code == 404

    shopping_lists = db.scalars(
        select(ShoppingList).where(
            ShoppingList.household_id == household.id
        )
    ).all()

    assert shopping_lists == []
def test_user_cannot_read_items_from_another_users_list(api_env):
    client, db, active_user = api_env

    owner = make_user("items-owner@example.com")
    outsider = make_user("items-outsider@example.com")
    db.add_all([owner, outsider])
    db.flush()

    shopping_list = ShoppingList(
        name="Private groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    active_user["user"] = outsider

    response = client.get(
        f"/api/v1/shopping-lists/{shopping_list.id}/items"
    )

    assert response.status_code == 404

    db.refresh(item)
    assert item.product_name == "Milk"
    assert item.archived_at is None

def test_create_shopping_list_item_value_error_returns_422_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    owner = make_user("item-value-error@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = owner

    def raise_value_error(*args, **kwargs):
        raise ValueError("Invalid shopping list item.")

    monkeypatch.setattr(
        shopping,
        "add_shopping_list_item",
        raise_value_error,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": 1,
            "unit": "piece",
            "status": "needed",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid shopping list item."
    assert rollback_called is True

def test_create_shopping_list_item_not_found_returns_404_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping
    from backend.app.services.shopping_lists import (
        ShoppingListNotFoundError,
    )

    client, db, active_user = api_env

    user = make_user("item-not-found@example.com")
    db.add(user)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=user.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = user

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    def raise_not_found(*args, **kwargs):
        raise ShoppingListNotFoundError()

    monkeypatch.setattr(
        shopping,
        "add_shopping_list_item",
        raise_not_found,
    )

    response = client.post(
        f"/api/v1/shopping-lists/{shopping_list.id}/items",
        json={
            "product_name": "Milk",
            "quantity": "1.000",
            "unit": "liter",
            "status": "needed",
        },
    )

    assert response.status_code == 404
    assert rollback_called is True

    assert db.get(User, user.id) is not None
    assert db.get(ShoppingList, shopping_list.id) is not None

    items = db.scalars(
        select(ShoppingListItem).where(
            ShoppingListItem.list_id == shopping_list.id
        )
    ).all()

    assert items == []


def test_change_item_status_integrity_error_returns_409_and_rolls_back(
    api_env,
    monkeypatch,
):
    from sqlalchemy.exc import IntegrityError
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    owner = make_user("status-integrity-error@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    active_user["user"] = owner

    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError(
            "UPDATE",
            {},
            Exception("Simulated integrity error"),
        )

    monkeypatch.setattr(
        shopping,
        "update_item_status",
        raise_integrity_error,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.patch(
    f"/api/v1/shopping-list-items/{item.id}/status",
    json={"status": "purchased"},
    )
    
    assert response.status_code == 409, response.json()
    assert response.json()["detail"] == (
        "The operation conflicts with existing data."
    )
    assert rollback_called is True


def test_change_item_status_not_found_returns_404_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping
    from backend.app.services.shopping_lists import (
        ShoppingListItemNotFoundError,
    )

    client, db, active_user = api_env

    owner = make_user("status-not-found@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    active_user["user"] = owner

    def raise_not_found(*args, **kwargs):
        raise ShoppingListItemNotFoundError()

    monkeypatch.setattr(
        shopping,
        "update_item_status",
        raise_not_found,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.patch(
        f"/api/v1/shopping-list-items/{item.id}/status",
        json={"status": "purchased"},
    )

    assert response.status_code == 404
    assert rollback_called is True

    db.refresh(item)
    assert item.status == "needed"

def test_archive_item_not_found_returns_404_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping
    from backend.app.services.shopping_lists import (
        ShoppingListItemNotFoundError,
    )

    client, db, active_user = api_env

    owner = make_user("archive-item-not-found@example.com")
    db.add(owner)
    db.flush()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.flush()

    item = make_item(shopping_list.id, owner.id)
    db.add(item)
    db.commit()

    active_user["user"] = owner

    def raise_not_found(*args, **kwargs):
        raise ShoppingListItemNotFoundError()

    monkeypatch.setattr(
        shopping,
        "archive_shopping_list_item",
        raise_not_found,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.delete(
        f"/api/v1/shopping-list-items/{item.id}"
    )

    assert response.status_code == 404
    assert rollback_called is True
    

def test_archive_shopping_list_not_found_returns_404_and_rolls_back(
    api_env,
    monkeypatch,
):
    from backend.app.api.routes import shopping
    from backend.app.services.shopping_lists import (
        ShoppingListNotFoundError,
    )

    client, db, active_user = api_env

    owner = make_user("archive-list-not-found@example.com")
    db.add(owner)
    db.commit()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = owner

    def raise_not_found(*args, **kwargs):
        raise ShoppingListNotFoundError()

    monkeypatch.setattr(
        shopping,
        "archive_shopping_list",
        raise_not_found,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.delete(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 404
    assert rollback_called is True


def test_archive_shopping_list_integrity_error_returns_409_and_rolls_back(
    api_env,
    monkeypatch,
):
    from sqlalchemy.exc import IntegrityError
    from backend.app.api.routes import shopping

    client, db, active_user = api_env

    owner = make_user("archive-list-integrity-error@example.com")
    db.add(owner)
    db.commit()

    shopping_list = ShoppingList(
        name="Groceries",
        owner_user_id=owner.id,
    )
    db.add(shopping_list)
    db.commit()

    active_user["user"] = owner

    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError(
            "UPDATE",
            {},
            Exception("Simulated integrity error"),
        )

    monkeypatch.setattr(
        shopping,
        "archive_shopping_list",
        raise_integrity_error,
    )

    rollback_called = False
    original_rollback = db.rollback

    def tracked_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "rollback", tracked_rollback)

    response = client.delete(
        f"/api/v1/shopping-lists/{shopping_list.id}"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The operation conflicts with existing data."
    )
    assert rollback_called is True





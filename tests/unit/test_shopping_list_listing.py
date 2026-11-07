"""GET /shopping-lists: "which lists can I open?" (a screen needs this first)."""

from datetime import datetime, timezone

from backend.app.db.models import Household, HouseholdMember, ShoppingList, User

from tests.unit.test_shopping_routes import api_env, make_user  # noqa: F401  (fixture)


def _household(db, *members):
    """members: (user, role) pairs. Returns the household."""
    household = Household(name="Home")
    db.add(household)
    db.flush()
    for user, role in members:
        db.add(HouseholdMember(household_id=household.id, user_id=user.id, role=role))
    db.flush()
    return household


def test_user_without_lists_gets_an_empty_list(api_env):
    client, db, active_user = api_env
    user = make_user("a@example.com")
    db.add(user); db.commit()
    active_user["user"] = user

    response = client.get("/api/v1/shopping-lists")

    assert response.status_code == 200
    assert response.json() == []


def test_personal_list_is_listed_and_manageable(api_env):
    client, db, active_user = api_env
    user = make_user("a@example.com")
    db.add(user); db.flush()
    db.add(ShoppingList(name="Mine", owner_user_id=user.id)); db.commit()
    active_user["user"] = user

    body = client.get("/api/v1/shopping-lists").json()

    assert len(body) == 1
    assert body[0]["name"] == "Mine"
    assert (body[0]["scope_type"], body[0]["scope_id"]) == ("user", user.id)
    assert body[0]["can_manage"] is True


def test_member_sees_household_list_but_cannot_manage_it(api_env):
    client, db, active_user = api_env
    owner, member = make_user("o@example.com"), make_user("m@example.com")
    db.add_all([owner, member]); db.flush()
    household = _household(db, (owner, "owner"), (member, "member"))
    db.add(ShoppingList(name="Shared", household_id=household.id)); db.commit()

    active_user["user"] = member
    member_view = client.get("/api/v1/shopping-lists").json()
    active_user["user"] = owner
    owner_view = client.get("/api/v1/shopping-lists").json()

    assert [(x["scope_type"], x["scope_id"], x["can_manage"]) for x in member_view] == [
        ("household", household.id, False)]
    assert [(x["scope_type"], x["scope_id"], x["can_manage"]) for x in owner_view] == [
        ("household", household.id, True)]


def test_admin_can_manage_household_list(api_env):
    client, db, active_user = api_env
    owner, admin = make_user("o@example.com"), make_user("ad@example.com")
    db.add_all([owner, admin]); db.flush()
    household = _household(db, (owner, "owner"), (admin, "admin"))
    db.add(ShoppingList(name="Shared", household_id=household.id)); db.commit()
    active_user["user"] = admin

    assert client.get("/api/v1/shopping-lists").json()[0]["can_manage"] is True


def test_personal_and_household_lists_come_together_in_a_stable_order(api_env):
    client, db, active_user = api_env
    user = make_user("a@example.com")
    db.add(user); db.flush()
    household = _household(db, (user, "member"))
    db.add(ShoppingList(name="Mine", owner_user_id=user.id))
    db.add(ShoppingList(name="Shared", household_id=household.id)); db.commit()
    active_user["user"] = user

    body = client.get("/api/v1/shopping-lists").json()

    assert [x["name"] for x in body] == ["Mine", "Shared"]


def test_other_peoples_lists_and_archived_lists_are_never_listed(api_env):
    client, db, active_user = api_env
    me, other = make_user("me@example.com"), make_user("other@example.com")
    db.add_all([me, other]); db.flush()
    other_household = _household(db, (other, "owner"))
    db.add(ShoppingList(name="Other personal", owner_user_id=other.id))
    db.add(ShoppingList(name="Other shared", household_id=other_household.id))
    db.add(ShoppingList(name="Old", owner_user_id=me.id, archived_at=datetime.now(timezone.utc)))
    db.commit()
    active_user["user"] = me

    assert client.get("/api/v1/shopping-lists").json() == []


def test_listing_requires_authentication():
    from fastapi.testclient import TestClient
    from backend.app.main import app

    assert TestClient(app).get("/api/v1/shopping-lists").status_code == 401
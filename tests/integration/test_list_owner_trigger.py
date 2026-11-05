"""The database itself must refuse to change a list's owner.

Runs the exact SQL the migration installs (installed inside the test's
transaction, which is rolled back), so the test passes whether or not the
migration was already applied to the test database.
"""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.app.db.models import Household, ShoppingList, User

MIGRATION = (Path(__file__).resolve().parents[2] / "backend" / "migrations" / "versions"
             / "b7e2d41c9a05_forbid_changing_list_owner.py")


def _install_trigger(db_session):
    spec = importlib.util.spec_from_file_location("owner_trigger_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db_session.execute(text(module.FUNCTION_SQL))
    db_session.execute(text(module.TRIGGER_SQL))


def _two_users_and_a_list(db_session):
    mine = User(email="owner-a@example.com", password_hash="x")
    other = User(email="owner-b@example.com", password_hash="x")
    family = Household(name="Family")
    db_session.add_all([mine, other, family])
    db_session.flush()
    shopping_list = ShoppingList(owner_user_id=mine.id, name="Mine")
    db_session.add(shopping_list)
    db_session.flush()
    return mine, other, family, shopping_list


@pytest.mark.integration
def test_the_owner_user_cannot_be_changed(db_session):
    _install_trigger(db_session)
    _, other, _, shopping_list = _two_users_and_a_list(db_session)

    with pytest.raises(DBAPIError, match="cannot be changed"):
        db_session.execute(
            text("UPDATE shopping_lists SET owner_user_id = :u WHERE id = :i"),
            {"u": other.id, "i": shopping_list.id})


@pytest.mark.integration
def test_a_personal_list_cannot_become_a_household_list(db_session):
    _install_trigger(db_session)
    _, _, family, shopping_list = _two_users_and_a_list(db_session)

    with pytest.raises(DBAPIError):
        db_session.execute(
            text("UPDATE shopping_lists SET owner_user_id = NULL, household_id = :h WHERE id = :i"),
            {"h": family.id, "i": shopping_list.id})


@pytest.mark.integration
def test_renaming_and_archiving_stay_allowed(db_session):
    _install_trigger(db_session)
    _, _, _, shopping_list = _two_users_and_a_list(db_session)

    db_session.execute(
        text("UPDATE shopping_lists SET name = 'Renamed', archived_at = now() WHERE id = :i"),
        {"i": shopping_list.id})
    assert db_session.execute(
        text("SELECT name FROM shopping_lists WHERE id = :i"), {"i": shopping_list.id}
    ).scalar_one() == "Renamed"


@pytest.mark.integration
def test_updating_the_owner_to_the_same_value_is_allowed(db_session):
    _install_trigger(db_session)
    mine, _, _, shopping_list = _two_users_and_a_list(db_session)

    db_session.execute(
        text("UPDATE shopping_lists SET owner_user_id = :u WHERE id = :i"),
        {"u": mine.id, "i": shopping_list.id})

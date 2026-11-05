"""Which scopes does the user belong to, and what is waiting on their lists?

This is the backend's OWN application data (the lists people edit all day),
not the data or ML layers' source: the suggestions themselves still come only
from the published file. It must be asked live, because it changes with every
tap, which is why it is not part of that file.

SCOPES
------
The published suggestions are per scope (the owner of a list):
    ("user", <id>)       the user's personal lists
    ("household", <id>)  the lists of a household
A user may see only their own personal scope and the household they belong to
(each user belongs to at most one). Everything else in the file is another
family's data and is never returned.

"WAITING" means: an item with status 'needed', not archived, on a list that is
not archived and that belongs to one of the user's scopes. Free-text items (no
product) can never match a suggestion and are ignored. This is the same access
rule the list endpoints use in api/routes/shopping.py.
"""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.db.models import HouseholdMember, ShoppingList, ShoppingListItem

ScopeKey = tuple[str, int]          # ("household" | "user", id)


def scope_of_list(shopping_list_owner_user_id: int | None, household_id: int | None) -> ScopeKey:
    """The scope a list belongs to (the database guarantees exactly one owner)."""
    if household_id is not None:
        return ("household", household_id)
    return ("user", shopping_list_owner_user_id)


def user_scopes(db: Session, user_id: int) -> set[ScopeKey]:
    """The scopes this user is allowed to see suggestions for."""
    scopes: set[ScopeKey] = {("user", user_id)}
    for (household_id,) in db.execute(
        select(HouseholdMember.household_id).where(HouseholdMember.user_id == user_id)
    ):
        scopes.add(("household", household_id))
    return scopes


def products_waiting_on_lists(db: Session, user_id: int) -> dict[ScopeKey, dict[int, int]]:
    """scope -> {product id -> id of the list holding its oldest waiting item}."""
    household_ids = select(HouseholdMember.household_id).where(
        HouseholdMember.user_id == user_id
    )
    rows = db.execute(
        select(
            ShoppingListItem.product_id, ShoppingListItem.list_id,
            ShoppingList.owner_user_id, ShoppingList.household_id,
        )
        .join(ShoppingList, ShoppingList.id == ShoppingListItem.list_id)
        .where(
            ShoppingListItem.product_id.is_not(None),
            ShoppingListItem.status == "needed",
            ShoppingListItem.archived_at.is_(None),
            ShoppingList.archived_at.is_(None),
            or_(
                ShoppingList.owner_user_id == user_id,
                ShoppingList.household_id.in_(household_ids),
            ),
        )
        .order_by(ShoppingListItem.id)       # oldest first, so the answer is deterministic
    ).all()

    waiting: dict[ScopeKey, dict[int, int]] = {}
    for product_id, list_id, owner_user_id, household_id in rows:
        scope = scope_of_list(owner_user_id, household_id)
        waiting.setdefault(scope, {}).setdefault(product_id, list_id)   # first (oldest) wins
    return waiting

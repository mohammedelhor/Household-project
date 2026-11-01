"""Which products are already waiting on the user's shopping lists?

This is the backend's OWN application data (the lists people edit all day),
not the data or ML layers' source: the suggestions themselves still come only
from the published file. It must be asked live, because it changes with every
tap, which is why it is not part of that file.

"WAITING" means: an item with status 'needed', not archived, on a list that is
not archived and that the user can access (their personal list, or a list of a
household they belong to). Free-text items (no product) can never match a
suggestion and are ignored. This is the same access rule the list endpoints
use in api/routes/shopping.py.
"""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.db.models import HouseholdMember, ShoppingList, ShoppingListItem


def products_waiting_on_lists(db: Session, user_id: int) -> dict[int, int]:
    """product id -> id of the list holding its oldest waiting item."""
    household_ids = select(HouseholdMember.household_id).where(
        HouseholdMember.user_id == user_id
    )
    rows = db.execute(
        select(ShoppingListItem.product_id, ShoppingListItem.list_id)
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

    waiting: dict[int, int] = {}
    for product_id, list_id in rows:
        waiting.setdefault(product_id, list_id)    # first (oldest) wins
    return waiting
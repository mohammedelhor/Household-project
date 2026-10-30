
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    ItemEvent,
    Product,
    ShoppingList,
    ShoppingListItem
)
from backend.app.core.units import normalize_unit

class ShoppingListNotFoundError(Exception):
    """Raised when a shopping list does not exist or is archived."""


class ShoppingListItemNotFoundError(Exception):
    """Raised when an active shopping-list item does not exist."""


class ShoppingListAlreadyExistsError(Exception):
    """Raised when a personal or household list already exists."""

class ProductNotFoundError(Exception):
    """Raised when a product does not exist or is inactive."""


def _now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)
def _record_event(
    db: Session,
    item: ShoppingListItem,
    event_type: Literal["added", "purchased", "reverted", "archived"],
    actor_user_id: int | None,
    *,
    occurred_at: datetime | None = None,
) -> None:
    """Append one row to the item's history (item_events).

    Why a single helper: every state change must leave a trace, and doing it
    in ONE place means no code path can forget it. The event is only added
    to the session (flush, not commit): it is saved or rolled back together
    with the change that caused it, in the same transaction, so history can
    never disagree with the current state.

    The item's quantity/unit/product are COPIED into the event, so later edits
    to the item cannot rewrite what happened.
    """
    db.add(
        ItemEvent(
            item_id=item.id,
            product_id=item.product_id,
            list_id=item.list_id,
            event_type=event_type,
            quantity=item.quantity,
            unit=item.unit,
            actor_user_id=actor_user_id,
            # None => the database clock (server_default now()).
            # A shared timestamp is passed when many items are archived
            # together, so they all carry the exact same moment.
            occurred_at=occurred_at,
        )
    )

def create_shopping_list(
    db: Session,
    name: str,
    *,
    owner_user_id: int | None = None,
    household_id: int | None = None,
) -> ShoppingList:
    """Create a personal or household shopping list.

    This is an internal service, not an authorization boundary.
    Callers must only supply ownership IDs from trusted server-side logic.
    """

    if (owner_user_id is None) == (household_id is None):
        raise ValueError(
            "Exactly one of owner_user_id or household_id must be provided."
        )

    name = name.strip()
    if not name:
        raise ValueError("Shopping list name cannot be empty.")

    if len(name) > 100:
        raise ValueError("Shopping list name cannot exceed 100 characters.")

    if owner_user_id is not None:
        existing = db.scalar(
            select(ShoppingList.id).where(
                ShoppingList.owner_user_id == owner_user_id,
                ShoppingList.archived_at.is_(None),
            )
        )
    else:
        existing = db.scalar(
            select(ShoppingList.id).where(
                ShoppingList.household_id == household_id,
                ShoppingList.archived_at.is_(None),
            )
        )

    if existing is not None:
        raise ShoppingListAlreadyExistsError(
            "An active shopping list already exists for this owner."
        )

    shopping_list = ShoppingList(
        name=name,
        owner_user_id=owner_user_id,
        household_id=household_id,
    )

    db.add(shopping_list)
    db.flush()
    return shopping_list


def get_shopping_list(
    db: Session,
    list_id: int,
) -> ShoppingList:
    """Return an active shopping list by ID."""

    shopping_list = db.scalar(
        select(ShoppingList).where(
            ShoppingList.id == list_id,
            ShoppingList.archived_at.is_(None),
        )
    )

    if shopping_list is None:
        raise ShoppingListNotFoundError(
            f"Active shopping list {list_id} was not found."
        )

    return shopping_list



def add_shopping_list_item(
    db: Session,
    list_id: int,
    *,
    quantity: Decimal,
    product_id: int | None = None,
    product_name: str | None = None,
    unit: str | None = None,
    created_by_user_id: int | None = None,
    status: Literal["needed", "purchased"] = "needed",
) -> ShoppingListItem:
    """Add a catalog product or a custom item to an active list."""

    if status not in ("needed", "purchased"):
        raise ValueError("Status must be 'needed' or 'purchased'.")

    shopping_list = get_shopping_list(db, list_id)

    product = None

    if product_id is not None:
        product = db.scalar(
            select(Product).where(
                Product.id == product_id,
                Product.is_active.is_(True),
            )
        )

        if product is None:
            raise ProductNotFoundError(
                f"Active product {product_id} was not found."
            )

        # The catalog is the source of truth for product identity.
        product_name = product.name

        # Use the default unit only when the caller omits the unit.
        if unit is None or not unit.strip():
            unit = product.default_unit

    if product_name is None:
        raise ValueError(
            "A product_id or product_name must be provided."
        )

    product_name = product_name.strip()

    if not product_name:
        raise ValueError("Product name cannot be empty.")

    if len(product_name) > 200:
        raise ValueError("Product name cannot exceed 200 characters.")

    if unit is None or not unit.strip():
        raise ValueError(
        "Unit is required when the product has no default unit."
    )

    unit = normalize_unit(unit)
    
    if not quantity.is_finite() or quantity <= Decimal("0"):
        raise ValueError(
            "Quantity must be a finite number greater than zero."
        )

    if quantity >= Decimal("1000000000"):
        raise ValueError(
            "Quantity exceeds the supported Numeric(12, 3) range."
        )

    if quantity != quantity.quantize(Decimal("0.001")):
        raise ValueError(
            "Quantity cannot have more than 3 decimal places."
        )

    item = ShoppingListItem(
        list_id=shopping_list.id,
        product_id=product.id if product is not None else None,
        product_name=product_name,
        quantity=quantity,
        unit=unit,
        status=status,
        created_by_user_id=created_by_user_id,
    )

    db.add(item)
    db.flush()
    
    _record_event(db, item, "added", created_by_user_id)
    db.flush()
    return item



def update_item_status(
    db: Session,
    item_id: int,
    status: Literal["needed", "purchased"],
    *,
    actor_user_id: int | None = None,
) -> ShoppingListItem:
    """Update the status of an active item."""

    if status not in ("needed", "purchased"):
        raise ValueError("Status must be 'needed' or 'purchased'.")

    item = db.scalar(
        select(ShoppingListItem)
        .join(
            ShoppingList,
            ShoppingList.id == ShoppingListItem.list_id,
        )
        .where(
            ShoppingListItem.id == item_id,
            ShoppingListItem.archived_at.is_(None),
            ShoppingList.archived_at.is_(None),
        )
    )

    if item is None:
        raise ShoppingListItemNotFoundError(
            f"Active shopping-list item {item_id} was not found."
        )

    previous_status = item.status

    if previous_status != status:
        item.status = status
        event_type = "purchased" if status == "purchased" else "reverted"
        _record_event(db, item, event_type, actor_user_id)

    db.flush()
    return item


def archive_shopping_list_item(
    db: Session,
    item_id: int,
    *,
    actor_user_id: int | None = None,
) -> ShoppingListItem:
    """Archive an item without deleting its database record."""

    item = db.scalar(
        select(ShoppingListItem)
        .join(
            ShoppingList,
            ShoppingList.id == ShoppingListItem.list_id,
        )
        .where(
            ShoppingListItem.id == item_id,
            ShoppingListItem.archived_at.is_(None),
            ShoppingList.archived_at.is_(None),
        )
    )

    if item is None:
        raise ShoppingListItemNotFoundError(
            f"Active shopping-list item {item_id} was not found."
        )

    item.archived_at = _now()
    _record_event(db, item, "archived", actor_user_id)
    db.flush()
    return item


def archive_shopping_list(
    db: Session,
    list_id: int,
    *,
    actor_user_id: int | None = None,
) -> ShoppingList:
    """Archive a list and its active items without deleting records."""

    shopping_list = get_shopping_list(db, list_id)
    archived_at = _now()

    shopping_list.archived_at = archived_at

    for item in shopping_list.items:
        if item.archived_at is None:
           item.archived_at = archived_at
           # Same timestamp for the item and its event.
           _record_event(
                db, item, "archived", actor_user_id,
                occurred_at=archived_at,
            )
    db.flush()
    return shopping_list

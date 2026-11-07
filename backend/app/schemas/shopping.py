
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from backend.app.core.units import normalize_unit

class ShoppingListCreate(BaseModel):
    """Validated input for creating a shopping list."""

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Shopping list name cannot be empty.")
        return value


class ShoppingListRead(BaseModel):
    """Public representation of a shopping list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
class ShoppingListSummary(BaseModel):
    """One list in "my lists": enough for a screen to show it, open it and
    decide which buttons to offer.

    `scope_type` / `scope_id` use the same vocabulary as /suggestions
    ("user" or "household"), so a client can match a suggestion to its list.
    `can_manage` answers "may I create/archive this list?": always true for
    a personal list, true for a household list only for its owner and admins.
    """

    id: int
    name: str
    scope_type: Literal["user", "household"]
    scope_id: int
    can_manage: bool


class ShoppingListItemCreate(BaseModel):
    """Validated input for creating a shopping-list item."""

    product_id: int | None = Field(default=None, gt=0)
    product_name: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    unit: str | None = Field(default=None, max_length=30)
    status: Literal["needed", "purchased"] = "needed"

    @field_validator("product_name")
    @classmethod
    def clean_product_name(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return value.strip() or None


    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str | None) -> str | None:
        if value is None:
            return None

        value = value.strip()
        if not value:
            return None

        return normalize_unit(value)

        # value = value.strip()
        # return value or None

    @field_validator("quantity")
    @classmethod
    def validate_finite_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("Quantity must be a finite number.")
        return value


class ShoppingListItemRead(BaseModel):
    """Representation of a shopping-list item."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    list_id: int
    product_name: str
    quantity: Decimal
    unit: str
    product_id: int | None
    status: Literal["needed", "purchased"]

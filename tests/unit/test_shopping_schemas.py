
import pytest
from pydantic import ValidationError

from backend.app.schemas.shopping import (
    ShoppingListCreate,
    ShoppingListItemCreate,
)


def test_valid_shopping_list_name():
    shopping_list = ShoppingListCreate(name="Weekly groceries")

    assert shopping_list.name == "Weekly groceries"


def test_shopping_list_name_cannot_be_empty():
    with pytest.raises(ValidationError):
        ShoppingListCreate(name="")


def test_valid_shopping_list_item():
    item = ShoppingListItemCreate(
        product_name="Rice",
        quantity="2.5",
        unit="kg",
    )

    assert item.product_name == "Rice"
    assert item.quantity == 2.5
    assert item.status == "needed"


def test_item_quantity_must_be_positive():
    with pytest.raises(ValidationError):
        ShoppingListItemCreate(
            product_name="Rice",
            quantity="0",
            unit="kg",
        )


def test_item_status_must_be_supported():
    with pytest.raises(ValidationError):
        ShoppingListItemCreate(
            product_name="Rice",
            quantity="2",
            unit="kg",
            status="unknown",
        )

def test_shopping_list_name_cannot_contain_only_spaces():
    with pytest.raises(ValidationError):
        ShoppingListCreate(name="   ")


def test_shopping_list_name_cannot_exceed_database_limit():
    with pytest.raises(ValidationError):
        ShoppingListCreate(name="a" * 101)


def test_item_unit_with_only_spaces_is_treated_as_omitted():
    # By design a blank unit becomes None, so a catalog product's default
    # unit can be used. If there is no default, the SERVICE rejects it
    # (see test_add_item_requires_unit_without_product_default in
    # test_shopping_lists).
    item = ShoppingListItemCreate(
        product_name="Rice",
        quantity="2",
        unit="   ",
    )

    assert item.unit is None

def test_item_quantity_cannot_exceed_three_decimal_places():
    with pytest.raises(ValidationError):
        ShoppingListItemCreate(
            product_name="Rice",
            quantity="1.2345",
            unit="kg",
        )
        

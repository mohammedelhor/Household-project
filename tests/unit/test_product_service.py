
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.db.models import Product
from backend.app.services.product_service import (
    ProductAlreadyExistsError,
    create_product,
    get_product_by_id,
    get_product_by_name,
    normalize_product_name,
)


def test_normalize_product_name():
    assert normalize_product_name("  Milk   Powder  ") == "milk powder"
    assert normalize_product_name("MILK POWDER") == "milk powder"


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_normalize_product_name_rejects_empty(name):
    with pytest.raises(ValueError, match="cannot be empty"):
        normalize_product_name(name)


def test_create_product():
    db = MagicMock()
    db.scalar.return_value = None
    db.flush.return_value = None

    product = create_product(
        db,
        name="  Milk Powder  ",
        default_unit="kg",
        category="Dairy",
    )

    assert isinstance(product, Product)
    assert product.name == "Milk Powder"
    assert product.normalized_name == "milk powder"
    assert product.default_unit == "kg"
    assert product.category == "Dairy"
    db.add.assert_called_once_with(product)
    db.flush.assert_called_once_with()

def test_create_product_normalizes_unit():
    db = MagicMock()
    db.scalar.return_value = None
    db.flush.return_value = None

    product = create_product(
        db,
        name="Milk",
        default_unit=" KG ",
    )

    assert product.default_unit == "kg"


def test_create_product_rejects_unsupported_unit():
    db = MagicMock()
    db.scalar.return_value = None

    with pytest.raises(ValueError, match="Unsupported unit"):
        create_product(
            db,
            name="Milk",
            default_unit="box",
        )

    db.add.assert_not_called()


def test_create_product_allows_no_default_unit():
    db = MagicMock()
    db.scalar.return_value = None
    db.flush.return_value = None

    product = create_product(
        db,
        name="Custom Product",
    )

    assert product.default_unit is None

def test_create_product_rejects_duplicate():
    db = MagicMock()
    db.scalar.return_value = Product(
        id=1,
        name="Milk Powder",
        normalized_name="milk powder",
    )

    with pytest.raises(ValueError, match="already exists"):
        create_product(db, name="MILK POWDER")

    db.add.assert_not_called()


def test_create_product_race_duplicate_raises_already_exists_without_rollback():
    # Simulates two requests racing: the pre-check passes, but the UNIQUE
    # constraint rejects the INSERT. It must be reported as a duplicate
    # (-> HTTP 409) and must NOT roll back the caller's whole transaction.
    db = MagicMock()
    db.scalar.return_value = None
    db.flush.side_effect = IntegrityError(
        "INSERT",
        {},
        Exception("unique constraint"),
    )

    with pytest.raises(ProductAlreadyExistsError, match="already exists"):
        create_product(db, name="Milk Powder")

    db.begin_nested.assert_called_once_with()
    db.rollback.assert_not_called()


def test_get_product_by_id():
    db = MagicMock()
    expected = Product(id=1, name="Milk", normalized_name="milk")
    db.get.return_value = expected

    result = get_product_by_id(db, 1)

    assert result is expected
    db.get.assert_called_once_with(Product, 1)


def test_get_product_by_name():
    db = MagicMock()
    expected = Product(id=1, name="Milk", normalized_name="milk")
    db.scalar.return_value = expected

    result = get_product_by_name(db, " MILK ")

    assert result is expected
    db.scalar.assert_called_once()

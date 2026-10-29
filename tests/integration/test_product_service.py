
import pytest

from backend.app.db.models import Product
from backend.app.services.product_service import (
    create_product,
    get_product_by_id,
    get_product_by_name,
    normalize_product_name,
)


@pytest.mark.integration
def test_create_product_persists_to_database(db_session):
    product = create_product(
        db_session,
        name="  Milk Powder  ",
        default_unit="kg",
        category="Dairy",
    )
    db_session.commit()

    saved_product = db_session.get(Product, product.id)

    assert saved_product is not None
    assert saved_product.name == "Milk Powder"
    assert saved_product.normalized_name == "milk powder"
    assert saved_product.default_unit == "kg"
    assert saved_product.category == "Dairy"


@pytest.mark.integration
def test_get_product_by_id(db_session):
    product = create_product(db_session, "Rice")
    db_session.flush()

    result = get_product_by_id(db_session, product.id)

    assert result is not None
    assert result.id == product.id
    assert result.name == "Rice"


@pytest.mark.integration
def test_get_product_by_id_returns_none_when_missing(db_session):
    assert get_product_by_id(db_session, 999999999) is None


@pytest.mark.integration
def test_get_product_by_name_uses_normalized_name(db_session):
    product = create_product(db_session, "Milk Powder")
    db_session.flush()

    result = get_product_by_name(db_session, "  MILK   POWDER ")

    assert result is not None
    assert result.id == product.id


@pytest.mark.integration
def test_create_product_rejects_duplicate_normalized_name(db_session):
    create_product(db_session, "Milk Powder")
    db_session.flush()

    with pytest.raises(ValueError, match="already exists"):
        create_product(db_session, "  MILK   POWDER  ")


@pytest.mark.integration
def test_create_product_rejects_empty_name(db_session):
    with pytest.raises(ValueError, match="cannot be empty"):
        create_product(db_session, "   ")


@pytest.mark.integration
def test_normalize_product_name_handles_unicode():
    assert normalize_product_name("  Ｒｉｃｅ  ") == "rice"

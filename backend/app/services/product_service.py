
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from backend.app.core.units import normalize_unit

from backend.app.db.models import Product


def normalize_product_name(name: str) -> str:
    """Normalize a product name for consistent matching."""
    if not isinstance(name, str):
        raise TypeError("Product name must be a string.")

    normalized = unicodedata.normalize("NFKC", name)
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()

    if not normalized:
        raise ValueError("Product name cannot be empty.")

    return normalized



def create_product(
    db: Session,
    name: str,
    default_unit: str | None = None,
    category: str | None = None,
) -> Product:
    """Create a product without committing the caller's transaction."""
    normalized_name = normalize_product_name(name)

    clean_name = name.strip()
    if len(clean_name) > 200:
        raise ValueError("Product name cannot exceed 200 characters.")

    if default_unit is not None:
        default_unit = default_unit.strip() or None

        if default_unit is not None:
            default_unit = normalize_unit(default_unit)
    
    if category is not None:
        category = category.strip() or None
        if category is not None and len(category) > 100:
            raise ValueError("Category cannot exceed 100 characters.")

    existing_product = db.scalar(
        select(Product).where(
            Product.normalized_name == normalized_name
        )
    )

    if existing_product is not None:
        raise ValueError("A product with this name already exists.")

    product = Product(
        name=clean_name,
        normalized_name=normalized_name,
        default_unit=default_unit,
        category=category,
    )

    db.add(product)

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            "Product could not be created because of a database constraint."
        ) from exc

    return product

def get_product_by_id(db: Session, product_id: int) -> Product | None:
    """Return a product by its ID, or None if it does not exist."""
    return db.get(Product, product_id)


def get_product_by_name(db: Session, name: str) -> Product | None:
    """Find a product using its normalized name."""
    normalized_name = normalize_product_name(name)

    return db.scalar(
        select(Product).where(
            Product.normalized_name == normalized_name
        )
    )

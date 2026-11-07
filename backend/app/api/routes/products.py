
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api.routes.auth import get_current_user
from backend.app.db.database import get_db
from backend.app.db.models import Product, User
from backend.app.schemas.products import ProductCreate, ProductRead
from backend.app.services.product_service import create_product, ProductAlreadyExistsError, search_products

router = APIRouter()


@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
)
def create_product_endpoint(
    data: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Product:
    try:
        product = create_product(
            db,
            name=data.name,
            default_unit=data.default_unit,
            category=data.category,
        )
        db.commit()
        db.refresh(product)
        return product

    # Order matters: ProductAlreadyExistsError is a ValueError, so it must
    # be caught first or it would be reported as 422.
    except ProductAlreadyExistsError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The product conflicts with existing data.",
        ) from exc
@router.get(
    "/products",
    response_model=list[ProductRead],
)
def search_products_endpoint(
    # Search before you create: a client looks for "milk" here and creates a
    # product only if nothing matches, so the catalog does not fill with
    # duplicates (a duplicate splits one product's purchase history in two).
    search: str | None = Query(default=None, max_length=100),
    # The catalog is shared by everyone, so it must never be returned whole:
    # `limit` is capped and `offset` pages through the rest.
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Product]:
    try:
        return search_products(db, search, limit=limit, offset=offset)
    except ValueError as exc:
        # The text was only spaces: nothing to search for.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

@router.get(
    "/products/{product_id}",
    response_model=ProductRead,
)
def read_product_endpoint(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Product:
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.is_active.is_(True),
        )
    )

    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found.",
        )

    return product

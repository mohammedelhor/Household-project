
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Literal

from backend.app.api.routes.auth import get_current_user
from backend.app.db.database import get_db
from backend.app.db.models import (
    HouseholdMember,
    ShoppingList,
    ShoppingListItem,
    User,
)
from backend.app.schemas.shopping import (
    ShoppingListCreate,
    ShoppingListSummary,
    ShoppingListRead,
    ShoppingListItemCreate,
    ShoppingListItemRead,
)
from backend.app.services.shopping_lists import (
    ProductNotFoundError,
    ShoppingListAlreadyExistsError,
    ShoppingListItemNotFoundError,
    ShoppingListNotFoundError,
    add_shopping_list_item,
    archive_shopping_list,
    archive_shopping_list_item,
    create_shopping_list,
    update_item_status,
)

router = APIRouter()


class ShoppingListItemStatusUpdate(BaseModel):
    status: Literal["needed", "purchased"]


def _accessible_list(
    db: Session,
    list_id: int,
    user_id: int,
) -> ShoppingList | None:
    """Return an active list only if the user can access it."""

    household_ids = select(
        HouseholdMember.household_id
    ).where(
        HouseholdMember.user_id == user_id
    )

    return db.scalar(
        select(ShoppingList).where(
            ShoppingList.id == list_id,
            ShoppingList.archived_at.is_(None),
            or_(
                ShoppingList.owner_user_id == user_id,
                ShoppingList.household_id.in_(household_ids),
            ),
        )
    )


def _accessible_item(
    db: Session,
    item_id: int,
    user_id: int,
) -> ShoppingListItem | None:
    """Return an active item only if its list is accessible."""

    household_ids = select(
        HouseholdMember.household_id
    ).where(
        HouseholdMember.user_id == user_id
    )

    return db.scalar(
        select(ShoppingListItem)
        .join(
            ShoppingList,
            ShoppingList.id == ShoppingListItem.list_id,
        )
        .where(
            ShoppingListItem.id == item_id,
            ShoppingListItem.archived_at.is_(None),
            ShoppingList.archived_at.is_(None),
            or_(
                ShoppingList.owner_user_id == user_id,
                ShoppingList.household_id.in_(household_ids),
            ),
        )
    )


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The operation conflicts with existing data.",
        )


def _not_found() -> HTTPException:
    # Return the same response for nonexistent and inaccessible resources.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Shopping list or item not found.",
    )


@router.post(
    "/shopping-lists",
    response_model=ShoppingListRead,
    status_code=status.HTTP_201_CREATED,
)
def create_personal_list(
    data: ShoppingListCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShoppingList:
    try:
        shopping_list = create_shopping_list(
            db,
            data.name,
            owner_user_id=current_user.id,
        )
        _commit_or_conflict(db)
        return shopping_list
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The operation conflicts with existing data.",
        )
    except ShoppingListAlreadyExistsError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active shopping list already exists.",
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.post(
    "/households/{household_id}/shopping-list",
    response_model=ShoppingListRead,
    status_code=status.HTTP_201_CREATED,
)
def create_household_list(
    household_id: int,
    data: ShoppingListCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShoppingList:
    membership = db.scalar(
        select(HouseholdMember).where(
           HouseholdMember.household_id == household_id,
           HouseholdMember.user_id == current_user.id,
        )
    )

    if membership is None:
        raise _not_found()

    if membership.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only household owners and admins can create shopping lists.",
       )

    try:
        shopping_list = create_shopping_list(
            db,
            data.name,
            household_id=household_id,
        )
        _commit_or_conflict(db)
        return shopping_list
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The operation conflicts with existing data.",
        )
    
    except ShoppingListAlreadyExistsError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active shopping list already exists.",
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
@router.get(
    "/shopping-lists",
    response_model=list[ShoppingListSummary],
)
def read_my_shopping_lists(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ShoppingListSummary]:
    """The active lists this user can open: their personal list and the
    list of the household they belong to.

    The database allows at most ONE active personal list per user and ONE
    active list per household, and a user belongs to at most one household,
    so this returns at most two rows: no pagination is needed.
    """

    # household_id -> my role there (at most one row, see the note above).
    roles = dict(
        db.execute(
            select(HouseholdMember.household_id, HouseholdMember.role).where(
                HouseholdMember.user_id == current_user.id
            )
        ).all()
    )

    shopping_lists = db.scalars(
        select(ShoppingList)
        .where(
            ShoppingList.archived_at.is_(None),
            or_(
                ShoppingList.owner_user_id == current_user.id,
                ShoppingList.household_id.in_(list(roles)),
            ),
        )
        .order_by(ShoppingList.id)
    ).all()

    summaries = []
    for shopping_list in shopping_lists:
        if shopping_list.household_id is None:
            summaries.append(
                ShoppingListSummary(
                    id=shopping_list.id,
                    name=shopping_list.name,
                    scope_type="user",
                    scope_id=current_user.id,
                    can_manage=True,  # it is my own list
                )
            )
        else:
            summaries.append(
                ShoppingListSummary(
                    id=shopping_list.id,
                    name=shopping_list.name,
                    scope_type="household",
                    scope_id=shopping_list.household_id,
                    # Same rule as creating/archiving a household list below.
                    can_manage=roles[shopping_list.household_id] in ("owner", "admin"),
                )
            )
    return summaries

@router.get(
    "/shopping-lists/{list_id}",
    response_model=ShoppingListRead,
)
def read_shopping_list(
    list_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShoppingList:
    shopping_list = _accessible_list(
        db, list_id, current_user.id
    )

    if shopping_list is None:
        raise _not_found()

    return shopping_list


@router.get(
    "/shopping-lists/{list_id}/items",
    response_model=list[ShoppingListItemRead],
)
def read_shopping_list_items(
    list_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ShoppingListItem]:
    shopping_list = _accessible_list(
        db, list_id, current_user.id
    )

    if shopping_list is None:
        raise _not_found()

    return list(
        db.scalars(
            select(ShoppingListItem).where(
                ShoppingListItem.list_id == list_id,
                ShoppingListItem.archived_at.is_(None),
            )
        ).all()
    )



@router.post(
    "/shopping-lists/{list_id}/items",
    response_model=ShoppingListItemRead,
    status_code=status.HTTP_201_CREATED,
)
def create_shopping_list_item(
    list_id: int,
    data: ShoppingListItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShoppingListItem:
    if _accessible_list(db, list_id, current_user.id) is None:
        raise _not_found()

    try:
        item = add_shopping_list_item(
            db,
            list_id,
            product_id=data.product_id,
            product_name=data.product_name,
            quantity=data.quantity,
            unit=data.unit,
            created_by_user_id=current_user.id,
            status=data.status,
        )
        _commit_or_conflict(db)
        return item

    except ShoppingListNotFoundError:
        db.rollback()
        raise _not_found()

    except ProductNotFoundError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Active product not found.",
        )

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The operation conflicts with existing data.",
        )

    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )



@router.patch(
    "/shopping-list-items/{item_id}/status",
    response_model=ShoppingListItemRead,
)
def change_item_status(
    item_id: int,
    data: ShoppingListItemStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ShoppingListItem:
    if _accessible_item(db, item_id, current_user.id) is None:
        raise _not_found()

    try:
        item = update_item_status(
            db,
            item_id,
            data.status,
            actor_user_id=current_user.id,
        )
        _commit_or_conflict(db)
        return item
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The operation conflicts with existing data.",
        )
    except ShoppingListItemNotFoundError:
        db.rollback()
        raise _not_found()


@router.delete(
    "/shopping-list-items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_shopping_list_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    if _accessible_item(db, item_id, current_user.id) is None:
        raise _not_found()

    try:
        archive_shopping_list_item(
            db, item_id, actor_user_id=current_user.id
        )
        _commit_or_conflict(db)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The operation conflicts with existing data.",
        )
    except ShoppingListItemNotFoundError:
        db.rollback()
        raise _not_found()

    return Response(status_code=status.HTTP_204_NO_CONTENT)



@router.delete(
    "/shopping-lists/{list_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_shopping_list(
    list_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    shopping_list = _accessible_list(
        db, list_id, current_user.id
    )

    if shopping_list is None:
        raise _not_found()

    # Personal lists can only be archived by their owner.
    # Shared household lists require owner or admin role.
    if shopping_list.household_id is not None:
        role = db.scalar(
            select(HouseholdMember.role).where(
                HouseholdMember.household_id
                == shopping_list.household_id,
                HouseholdMember.user_id == current_user.id,
            )
        )

        if role not in ("owner", "admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only household owners and admins can archive shared lists.",
            )

    try:
        archive_shopping_list(
            db, list_id, actor_user_id=current_user.id
        )
        _commit_or_conflict(db)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The operation conflicts with existing data.",
        )
    except ShoppingListNotFoundError:
        db.rollback()
        raise _not_found()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


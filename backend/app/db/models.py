
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from backend.app.core.units import SUPPORTED_UNITS

def _unit_check_sql(column: str) -> str:
    """Build `column IN ('g', 'kg', ...)` from the single list of supported units.

    Why: the app already validates units (core/units.py), but the data
    engineering layer reads the database directly. A CHECK constraint makes
    the database itself refuse a bad unit, whoever the writer is.
    NOTE: migrations must keep a frozen literal copy of this list so old
    migrations never change when this list grows.
    """
    quoted = ", ".join(f"'{u}'" for u in sorted(SUPPORTED_UNITS))
    return f"{column} IN ({quoted})"

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


    memberships: Mapped[list["HouseholdMember"]] = relationship(
        back_populates="household",
    )

    shopping_lists: Mapped[list["ShoppingList"]] = relationship(
        back_populates="household",
    )


class HouseholdMember(Base):
    __tablename__ = "household_members"

    __table_args__ = (
        UniqueConstraint(
            "household_id",
            "user_id",
            name="uq_household_member",
        ),
        UniqueConstraint(
            "user_id",
            name="uq_household_member_user",
        ),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_household_member_role",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="CASCADE"),
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="member",
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    household: Mapped["Household"] = relationship(
        back_populates="memberships",
    )

    user: Mapped["User"] = relationship()

class Product(Base):
    __tablename__ = "products"

    __table_args__ = (
        CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_product_name_not_empty",
        ),
        CheckConstraint(
            "length(trim(normalized_name)) > 0",
            name="ck_product_normalized_name_not_empty",
        ),
        UniqueConstraint(
            "normalized_name",
            name="uq_product_normalized_name",
        ),
        CheckConstraint(
            f"default_unit IS NULL OR {_unit_check_sql('default_unit')}",
            name="ck_product_default_unit_supported",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    normalized_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    default_unit: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )

    category: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    shopping_list_items: Mapped[list["ShoppingListItem"]] = relationship(
        back_populates="product",
    )


class ShoppingList(Base):
    __tablename__ = "shopping_lists"

    __table_args__ = (
        CheckConstraint(
            """
            (owner_user_id IS NOT NULL AND household_id IS NULL)
            OR
            (owner_user_id IS NULL AND household_id IS NOT NULL)
            """,
            name="ck_shopping_list_exactly_one_owner",
        ),
        
        Index(
            "uq_personal_shopping_list_per_user",
            "owner_user_id",
            unique=True,
            postgresql_where=text(
                "owner_user_id IS NOT NULL AND archived_at IS NULL"
            ),
        ),
        Index(
            "uq_shared_shopping_list_per_household",
            "household_id",
            unique=True,
            postgresql_where=text(
                "household_id IS NOT NULL AND archived_at IS NULL"
            ),
        ),

    
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )

    household_id: Mapped[int | None] = mapped_column(
        ForeignKey("households.id"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    owner: Mapped["User | None"] = relationship()

    household: Mapped["Household | None"] = relationship(
        back_populates="shopping_lists",
    )

    items: Mapped[list["ShoppingListItem"]] = relationship(
        back_populates="shopping_list",
    )


class ShoppingListItem(Base):
    __tablename__ = "shopping_list_items"

    __table_args__ = (
        CheckConstraint(
            "quantity > 0",
            name="ck_shopping_list_item_quantity_positive",
        ),
        CheckConstraint(
            "status IN ('needed', 'purchased')",
            name="ck_shopping_list_item_status",
        ),
        CheckConstraint(
            _unit_check_sql("unit"),
            name="ck_shopping_list_item_unit_supported",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    list_id: Mapped[int] = mapped_column(
        ForeignKey("shopping_lists.id"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[int | None] = mapped_column(
    ForeignKey(
        "products.id",
        ondelete="SET NULL",
        name="fk_shopping_list_items_product_id_products",
    ),
    nullable=True,
    index=True,
    )
    product_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3),
        nullable=False,
    )

    unit: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="needed",
    )
    
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    shopping_list: Mapped["ShoppingList"] = relationship(
        back_populates="items",
    )

    product: Mapped["Product | None"] = relationship(
        back_populates="shopping_list_items",
    )
    created_by: Mapped["User | None"] = relationship()
    events: Mapped[list["ItemEvent"]] = relationship(
        back_populates="item",
    )
class ItemEvent(Base):
    """Append-only history of what happened to a shopping-list item.

    WHY THIS TABLE EXISTS
    ShoppingListItem only stores the *current* state (status = needed /
    purchased). The data engineering layer needs the *history*: "rice was
    bought on the 3rd, again on the 19th" => consumption rate. A status
    column overwrites itself, so history would be lost. Rows here are only
    ever INSERTED, never updated or deleted.
    This is enforced by a database trigger (migration c4d8f2a61b73); the only exception is
    the ON DELETE SET NULL of product_id / actor_user_id. See backend/app/db/event_admin.py
    for the explicit correction path.

    Denormalized on purpose (product_id, quantity, unit, list_id are copied
    from the item at event time): analytics should not need to join back to
    mutable rows, and the values must stay as they were when it happened.
    """

    __tablename__ = "item_events"

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('added', 'purchased', 'reverted', 'archived')",
            name="ck_item_event_type",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_item_event_quantity_positive",
        ),
        CheckConstraint(
            _unit_check_sql("unit"),
            name="ck_item_event_unit_supported",
        ),
        # Typical DE query: "all events of this product over time".
        Index(
            "ix_item_events_product_id_occurred_at",
            "product_id",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    item_id: Mapped[int] = mapped_column(
        ForeignKey("shopping_list_items.id"),
        nullable=False,
        index=True,
    )

    # Copied from the item (see class docstring). Nullable because custom
    # items have no catalog product, and a deleted product must not delete
    # history.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )

    list_id: Mapped[int] = mapped_column(
        ForeignKey("shopping_lists.id"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3),
        nullable=False,
    )

    unit: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    item: Mapped["ShoppingListItem"] = relationship(
        back_populates="events",
    )

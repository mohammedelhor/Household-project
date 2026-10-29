
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

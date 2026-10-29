
"""allow replacement of archived shopping lists

Revision ID: 2fd91b525ebe
Revises: da86230a65d7
"""

from alembic import op


revision = "2fd91b525ebe"
down_revision = "da86230a65d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "uq_personal_shopping_list_per_user",
        table_name="shopping_lists",
    )
    op.drop_index(
        "uq_shared_shopping_list_per_household",
        table_name="shopping_lists",
    )

    op.create_index(
        "uq_personal_shopping_list_per_user",
        "shopping_lists",
        ["owner_user_id"],
        unique=True,
        postgresql_where=(
            "owner_user_id IS NOT NULL AND archived_at IS NULL"
        ),
    )
    op.create_index(
        "uq_shared_shopping_list_per_household",
        "shopping_lists",
        ["household_id"],
        unique=True,
        postgresql_where=(
            "household_id IS NOT NULL AND archived_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_personal_shopping_list_per_user",
        table_name="shopping_lists",
    )
    op.drop_index(
        "uq_shared_shopping_list_per_household",
        table_name="shopping_lists",
    )

    op.create_index(
        "uq_personal_shopping_list_per_user",
        "shopping_lists",
        ["owner_user_id"],
        unique=True,
        postgresql_where="owner_user_id IS NOT NULL",
    )
    op.create_index(
        "uq_shared_shopping_list_per_household",
        "shopping_lists",
        ["household_id"],
        unique=True,
        postgresql_where="household_id IS NOT NULL",
    )

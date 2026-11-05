"""forbid changing the owner of a shopping list

Revision ID: b7e2d41c9a05
Revises: 6c182d4961d7
Create Date: 2026-11-02 12:00:00.000000

WHY
---
The data engineering layer works out whose purchases are whose from the
OWNER of the shopping list (owner_user_id or household_id): the prediction
unit is (owner, product). That is only sound if a list never changes owner.
Today no endpoint changes it, but "no endpoint does it today" is a promise of
the application code; this trigger makes it a promise of the DATABASE, so a
future endpoint, a script or a manual UPDATE cannot silently re-assign a
list's history to another family.

The pipeline also fails its quality gate if it ever sees an owner change
between two lists snapshots, so a change that somehow got through (for
example before this migration) is caught, not trusted.

WHAT IT FORBIDS
---------------
UPDATE of shopping_lists.owner_user_id or shopping_lists.household_id on an
existing row. Everything else (renaming, archiving) stays allowed. To "move"
a list, create a new one: lists are archived, never deleted or re-assigned.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b7e2d41c9a05"
down_revision: Union[str, Sequence[str], None] = "6c182d4961d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Plain SQL strings, importable by the integration test (so the test checks
# exactly what the migration installs).
FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION forbid_shopping_list_owner_change() RETURNS trigger AS $$
BEGIN
    IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
       OR NEW.household_id IS DISTINCT FROM OLD.household_id THEN
        RAISE EXCEPTION 'the owner of shopping list % cannot be changed', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_shopping_list_owner_is_fixed ON shopping_lists;
CREATE TRIGGER trg_shopping_list_owner_is_fixed
BEFORE UPDATE OF owner_user_id, household_id ON shopping_lists
FOR EACH ROW EXECUTE FUNCTION forbid_shopping_list_owner_change();
"""


def upgrade() -> None:
    op.execute(FUNCTION_SQL)
    op.execute(TRIGGER_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_shopping_list_owner_is_fixed ON shopping_lists")
    op.execute("DROP FUNCTION IF EXISTS forbid_shopping_list_owner_change()")

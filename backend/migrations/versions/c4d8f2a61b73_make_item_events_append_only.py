"""make item_events append-only in the database

Revision ID: c4d8f2a61b73
Revises: b7e2d41c9a05
Create Date: 2026-11-05 12:00:00.000000

WHY
---
`item_events` is the history every analysis and prediction is built on. The
models and the README call it append-only, but until now that was only a habit
of the application code: a manual UPDATE or DELETE, or another script, could
rewrite the past without anyone noticing. This migration makes the DATABASE
refuse it.

WHAT IS FORBIDDEN
-----------------
* DELETE and TRUNCATE of any event.
* UPDATE of any event, with ONE exception: the two foreign keys declared
  `ON DELETE SET NULL` (`product_id`, `actor_user_id`) must still be able to
  turn into NULL. When a product or a user is deleted, PostgreSQL itself runs
  an UPDATE that sets those columns to NULL; forbidding it would make it
  impossible to delete a user or a product. Everything else on the row
  (item, list, type, quantity, unit, time) must stay identical, and a NULL
  never turns back into a value.

HOW TO CORRECT A WRONG EVENT (the administrative path)
------------------------------------------------------
Preferred: do NOT edit history. Append a new event that corrects the state
(for example a `reverted` event), because the past stays explainable.

If a row really must be changed or removed (a test database, a bad import),
do it in ONE explicit transaction that says so:

    BEGIN;
    SELECT set_config('household.allow_event_correction', 'on', true);  -- this transaction only
    -- the UPDATE / DELETE here
    COMMIT;

The flag lives only until the transaction ends, so it can never be left on by
mistake, and the statement that uses it is visible in code review and logs.

HONEST LIMIT
------------
This protects against mistakes, buggy code and casual scripts. Anyone who
owns the table (or is a superuser) can still drop the trigger or set the flag;
that cannot be prevented from inside the same database, and is why the data
pipeline connects with a read-only role (see data_engineering/check_source_access.py).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c4d8f2a61b73"
down_revision: Union[str, Sequence[str], None] = "b7e2d41c9a05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Plain SQL strings, importable by the integration tests (so they test exactly
# what this migration installs).
FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION forbid_item_event_change() RETURNS trigger AS $$
BEGIN
    -- The explicit, transaction-local administrative path.
    IF COALESCE(current_setting('household.allow_event_correction', true), '') = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        -- Only the ON DELETE SET NULL references may change, and only to NULL.
        IF NEW.id = OLD.id
           AND NEW.item_id = OLD.item_id
           AND NEW.list_id = OLD.list_id
           AND NEW.event_type = OLD.event_type
           AND NEW.quantity = OLD.quantity
           AND NEW.unit = OLD.unit
           AND NEW.occurred_at = OLD.occurred_at
           AND (NEW.product_id IS NOT DISTINCT FROM OLD.product_id OR NEW.product_id IS NULL)
           AND (NEW.actor_user_id IS NOT DISTINCT FROM OLD.actor_user_id OR NEW.actor_user_id IS NULL)
        THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'item_events is append-only: event % cannot be updated', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'item_events is append-only: event % cannot be deleted', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- TRUNCATE
    RAISE EXCEPTION 'item_events is append-only: it cannot be truncated'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_item_events_append_only ON item_events;
CREATE TRIGGER trg_item_events_append_only
BEFORE UPDATE OR DELETE ON item_events
FOR EACH ROW EXECUTE FUNCTION forbid_item_event_change();

DROP TRIGGER IF EXISTS trg_item_events_no_truncate ON item_events;
CREATE TRIGGER trg_item_events_no_truncate
BEFORE TRUNCATE ON item_events
FOR EACH STATEMENT EXECUTE FUNCTION forbid_item_event_change();
"""


def upgrade() -> None:
    op.execute(FUNCTION_SQL)
    # asyncpg-style drivers refuse several statements in one execute: send them one by one.
    for statement in [s.strip() for s in TRIGGER_SQL.split(";") if s.strip()]:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_item_events_no_truncate ON item_events")
    op.execute("DROP TRIGGER IF EXISTS trg_item_events_append_only ON item_events")
    op.execute("DROP FUNCTION IF EXISTS forbid_item_event_change()")
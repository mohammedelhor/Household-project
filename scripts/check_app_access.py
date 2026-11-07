"""Proves that the API's database role cannot do what it must never do.

WHY THIS EXISTS
---------------
A permission we only *believe* we set is not a safety measure (same idea as
data_engineering/check_source_access.py). This script connects with the API's
URL and tries things that must work and things that must be refused.

A "must be refused" check passes ONLY if PostgreSQL answers "permission
denied" (SQLSTATE 42501). Any other failure (wrong password, no network)
counts as a failure, otherwise a broken connection would look like success.

The most important check: even after switching ON the append-only trigger's
correction flag (`household.allow_event_correction`), the API role still
cannot UPDATE or DELETE item_events. That is the difference between a
seatbelt (trigger) and a wall (privileges).

Every statement is rolled back; the ids used (-1) match no row anyway.

USAGE (PowerShell, project root)
--------------------------------
    uv run python scripts/check_app_access.py                  # uses DATABASE_URL
    uv run python scripts/check_app_access.py --var SOME_URL   # any other variable
    uv run python scripts/check_app_access.py --expect dev     # also verify the database's label
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))          # so `backend` can be imported from scripts/

from backend.app.core.db_safety import ENVIRONMENTS, check_environment  # noqa: E402
PERMISSION_DENIED = "42501"

FLAG = "SELECT set_config('household.allow_event_correction', 'on', true)"

# (description, [statements run in ONE transaction], must the LAST one succeed?)
CHECKS: list[tuple[str, list[str], bool]] = [
    # --- things the API must be able to do -------------------------------
    ("can read users", ["SELECT count(*) FROM users"], True),
    ("can read item_events", ["SELECT count(*) FROM item_events"], True),
    ("can update shopping_list_items", ["UPDATE shopping_list_items SET quantity = quantity WHERE id = -1"], True),
    ("can delete shopping_list_items", ["DELETE FROM shopping_list_items WHERE id = -1"], True),
    ("can update products", ["UPDATE products SET name = name WHERE id = -1"], True),
    # --- the wall around item_events -------------------------------------
    ("has INSERT on item_events",
     ["DO $$ BEGIN IF NOT has_table_privilege(current_user, 'item_events', 'INSERT') "
      "THEN RAISE EXCEPTION 'no INSERT'; END IF; END $$"], True),
    ("cannot UPDATE item_events", ["UPDATE item_events SET event_type = event_type WHERE id = -1"], False),
    ("cannot DELETE from item_events", ["DELETE FROM item_events WHERE id = -1"], False),
    ("cannot TRUNCATE item_events", ["TRUNCATE item_events"], False),
    ("cannot UPDATE item_events even with the correction flag ON",
     [FLAG, "UPDATE item_events SET event_type = event_type WHERE id = -1"], False),
    ("cannot DELETE from item_events even with the correction flag ON",
     [FLAG, "DELETE FROM item_events WHERE id = -1"], False),
    # --- no schema changes, no migration bookkeeping ----------------------
    ("cannot create tables", ["CREATE TABLE app_runtime_should_not_exist (id int)"], False),
    ("cannot drop item_events", ["DROP TABLE item_events"], False),
    ("cannot read alembic_version", ["SELECT version_num FROM alembic_version"], False),
    ("cannot create roles", ["CREATE ROLE app_runtime_should_not_exist"], False),
    # --- no hidden shortcuts ----------------------------------------------
    # A role can be limited table by table and still be all-powerful through
    # an attribute or a group membership. On Neon, a role made in the web
    # console joins `neon_superuser`, which can read and write every table;
    # a role made with SQL (as setup_roles.py does) does not. These two
    # checks make that difference visible instead of assumed.
    ("has no powerful attribute (superuser, createrole, createdb, bypassrls, replication)",
     ["DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND "
      "(rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls OR rolreplication)) "
      "THEN RAISE EXCEPTION 'powerful attribute'; END IF; END $$"], True),
    ("is not a member of a group that bypasses table privileges",
     ["DO $$ DECLARE g text; BEGIN FOREACH g IN ARRAY ARRAY['pg_write_all_data', 'pg_read_all_data', "
      "'pg_database_owner', 'neon_superuser'] LOOP IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = g) "
      "AND pg_has_role(current_user, g, 'member') THEN RAISE EXCEPTION 'member of %', g; END IF; END LOOP; "
      "END $$"], True),
]


def run_check(engine, statements: list[str]) -> tuple[bool, str | None]:
    """Run the statements in one transaction. Returns (last one succeeded, sqlstate)."""
    with engine.connect() as connection:
        try:
            for statement in statements:
                connection.execute(text(statement))
            return True, None
        except DBAPIError as exc:
            return False, getattr(exc.orig, "sqlstate", None)
        finally:
            connection.rollback()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--var", default="DATABASE_URL", help="variable holding the API's URL")
    parser.add_argument("--expect", choices=ENVIRONMENTS,
                        help="stop unless the database's label is this environment (recommended)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv(args.var)
    if not url:
        print(f"{args.var} is not set.")
        return 1

    target = make_url(url)
    print(f"user={target.username} host={target.host} database={target.database}\n")
    if target.username in ("neondb_owner", "postgres"):
        print("STOP: this is an owner URL, not the API's. The checks below would fail by design.")
        return 1

    if args.expect:
        ok, message = check_environment(url, args.expect)
        print(f"label: {message}\n")
        if not ok:
            print("STOP: wrong database for this check.")
            return 1

    engine = create_engine(url, pool_pre_ping=True)
    try:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except DBAPIError as exc:
            print(f"FAIL  cannot connect: {exc.orig}")
            return 1

        failures = 0
        for description, statements, must_succeed in CHECKS:
            succeeded, sqlstate = run_check(engine, statements)
            passed = succeeded if must_succeed else ((not succeeded) and sqlstate == PERMISSION_DENIED)
            print(f"{'PASS' if passed else 'FAIL'}  {description}")
            failures += not passed

        print("\nAll checks passed." if failures == 0 else f"\n{failures} check(s) FAILED.")
        return 0 if failures == 0 else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

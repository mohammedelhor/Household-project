"""Checks that SOURCE_DATABASE_URL is a least-privilege, read-only user.

WHY THIS EXISTS
---------------
The data engineering layer reads the application's data. It must be unable to
change that data, even by accident (a bug, a wrong command). We give it a
database user that can only SELECT from the tables it needs (item_events, products, and three owner columns of shopping_lists). But a
permission we only *believe* we set is not a safety measure, so this script
proves it: it tries things that must work and things that must be refused.

A "refused" check passes ONLY if PostgreSQL answers "permission denied"
(error 42501). Any other failure (wrong password, no network) is reported as
a failure too, otherwise a broken connection would look like a success.

USAGE (from the project root)
-----------------------------
    uv run python -m data_engineering.check_source_access
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# PostgreSQL's SQLSTATE code for "insufficient privilege".
PERMISSION_DENIED = "42501"

# (description, SQL, must it succeed?)
# The write statements use "id = -1": even if they were wrongly allowed they
# would change nothing, and every check is rolled back anyway.
CHECKS: list[tuple[str, str, bool]] = [
    ("can read item_events", "SELECT count(*) FROM item_events", True),
    ("can read products", "SELECT count(*) FROM products", True),
    # Only the three columns that say WHOSE a list is (scope of the predictions).
    # The list's name and the rest stay unreadable: the pipeline does not need them.
    ("can read the owner columns of shopping_lists",
     "SELECT id, owner_user_id, household_id FROM shopping_lists LIMIT 1", True),
    ("cannot read other shopping_lists columns", "SELECT name FROM shopping_lists LIMIT 1", False),
    ("cannot update shopping_lists", "UPDATE shopping_lists SET owner_user_id = owner_user_id WHERE id = -1", False),
    ("cannot read users (the pipeline does not need emails)", "SELECT count(*) FROM users", False),
    ("cannot delete from item_events", "DELETE FROM item_events WHERE id = -1", False),
    ("cannot update products", "UPDATE products SET name = name WHERE id = -1", False),
]


def run_check(engine, sql: str) -> tuple[bool, str | None]:
    """Run one statement on its own connection. Returns (succeeded, sqlstate)."""
    with engine.connect() as connection:
        try:
            connection.execute(text(sql))
            return True, None
        except DBAPIError as exc:
            return False, getattr(exc.orig, "sqlstate", None)
        finally:
            connection.rollback()  # nothing is ever kept


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    url = os.getenv("SOURCE_DATABASE_URL")
    if not url:
        print("SOURCE_DATABASE_URL is not set. Add it to .env.")
        return 1

    target = make_url(url)
    print(f"user={target.username} host={target.host} database={target.database}\n")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        # First prove we can connect at all, so a bad password cannot be
        # mistaken for "permission correctly denied".
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except DBAPIError as exc:
            print(f"FAIL  cannot connect: {exc.orig}")
            return 1

        failures = 0
        for description, sql, must_succeed in CHECKS:
            succeeded, sqlstate = run_check(engine, sql)

            if must_succeed:
                passed = succeeded
            else:
                passed = (not succeeded) and sqlstate == PERMISSION_DENIED

            print(f"{'PASS' if passed else 'FAIL'}  {description}")
            if not passed:
                failures += 1

        print("\nAll checks passed." if failures == 0 else f"\n{failures} check(s) FAILED.")
        return 0 if failures == 0 else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

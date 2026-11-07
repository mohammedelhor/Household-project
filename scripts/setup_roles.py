"""Create the database roles and give each ONLY the privileges it needs.

WHY THIS EXISTS
---------------
Until now every part of the project connected as the owner role
(`neondb_owner`). The owner can do anything, including UPDATE/DELETE on
`item_events`. The trigger that makes `item_events` append-only is a seatbelt,
but a role that may update the table can also switch the trigger's exception
flag on, so the trigger alone is not a security boundary. PRIVILEGES are:
PostgreSQL refuses an UPDATE from a role that was never granted UPDATE,
whatever flags it sets.

So there are three kinds of role, and each connection uses the weakest one
that can do its job:

    owner (neondb_owner)  migrations, this script, tests, the generator.
                          Never used by the running API.
    app_runtime           the API. Normal tables: read/write.
                          item_events: SELECT and INSERT only (append-only).
                          No DDL, cannot touch alembic_version.
    analytics_reader      the data pipeline. SELECT on item_events, products
                          and three owner columns of shopping_lists. Nothing else.

HOW IT BEHAVES
--------------
* Idempotent: running it twice leaves the same result (grants are reset to
  exactly what is listed here, then re-applied).
* Passwords are NEVER in files or arguments. A role that does not exist yet
  is created and its password is asked for with a hidden prompt (or read
  from APP_RUNTIME_PASSWORD / ANALYTICS_READER_PASSWORD in this terminal).
  An existing role keeps its password.
* Without --apply it only reports what exists and what it would do.

USAGE (PowerShell, project root)
--------------------------------
    uv run python scripts/setup_roles.py dev               # look only
    uv run python scripts/setup_roles.py dev --apply
    uv run python scripts/setup_roles.py production --apply   # asks for the host name

Which URL is used (it must be an OWNER URL, because only an owner can grant):
    dev         OWNER_DATABASE_URL, else DATABASE_URL
    test        TEST_DATABASE_URL
    synthetic   SYNTHETIC_DATABASE_URL
    production  PRODUCTION_URL (set in this terminal only)
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))          # so `backend` can be imported from scripts/

from backend.app.core.db_safety import check_environment  # noqa: E402

APP_ROLE = "app_runtime"
READER_ROLE = "analytics_reader"

# environment -> variables to try, in order
ENVIRONMENTS = {
    "dev": ["OWNER_DATABASE_URL", "DATABASE_URL"],
    "test": ["TEST_DATABASE_URL"],
    "synthetic": ["SYNTHETIC_DATABASE_URL"],
    "production": ["PRODUCTION_URL"],
}

# Tables the app may NOT update or delete. Add a table here when a later
# migration makes it append-only too.
APPEND_ONLY = ["item_events"]
# Tables the app must not touch at all (migration bookkeeping).
HIDDEN_FROM_APP = ["alembic_version"]


def owner_url(environment: str) -> tuple[str, str] | None:
    for variable in ENVIRONMENTS[environment]:
        if os.getenv(variable):
            return variable, os.environ[variable]
    return None


def password_for(role: str, variable: str) -> str:
    """Hidden prompt, or the environment variable when running unattended."""
    value = os.getenv(variable) or getpass.getpass(f"Password for the NEW role {role} (hidden): ")
    if len(value) < 16:
        raise SystemExit("The password must be at least 16 characters. Nothing changed for this role.")
    return value


def role_exists(cursor, role: str) -> bool:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return cursor.fetchone() is not None


def table_names(cursor) -> list[str]:
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
    return [row[0] for row in cursor.fetchall()]


def ident(name: str) -> sql.Identifier:
    return sql.Identifier(name)


def apply_roles(cursor, database: str) -> None:
    """All the CREATE/GRANT statements. Runs inside ONE transaction: all or nothing."""
    tables = table_names(cursor)

    # ---- create missing roles (passwords asked only now) -----------------
    for role, variable in ((APP_ROLE, "APP_RUNTIME_PASSWORD"), (READER_ROLE, "ANALYTICS_READER_PASSWORD")):
        if not role_exists(cursor, role):
            cursor.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                ident(role), sql.Literal(password_for(role, variable))))
            print(f"created role {role}")
        else:
            print(f"role {role} already exists (password untouched)")

    # ---- app_runtime ------------------------------------------------------
    # Start from nothing so the result is exactly what is written below, even
    # if someone granted extra rights by hand earlier.
    cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ident(APP_ROLE)))
    cursor.execute(sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(ident(APP_ROLE)))
    cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(ident(database), ident(APP_ROLE)))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident(APP_ROLE)))
    for table in tables:
        if table in HIDDEN_FROM_APP:
            continue
        # SELECT + INSERT everywhere; UPDATE and DELETE only on tables that are not append-only.
        privileges = "SELECT, INSERT" if table in APPEND_ONLY else "SELECT, INSERT, UPDATE, DELETE"
        cursor.execute(sql.SQL("GRANT {} ON {} TO {}").format(
            sql.SQL(privileges), ident(table), ident(APP_ROLE)))
    # Needed so INSERT can take the next id from its sequence.
    cursor.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(ident(APP_ROLE)))
    # Tables created by FUTURE migrations (run by the owner) start with normal
    # read/write rights, so the API keeps working. A new append-only table must
    # be added to APPEND_ONLY above and this script re-run.
    cursor.execute(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
    ).format(ident(APP_ROLE)))
    cursor.execute(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}"
    ).format(ident(APP_ROLE)))

    # ---- analytics_reader -------------------------------------------------
    cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(ident(READER_ROLE)))
    cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(ident(database), ident(READER_ROLE)))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident(READER_ROLE)))
    cursor.execute(sql.SQL("GRANT SELECT ON item_events, products TO {}").format(ident(READER_ROLE)))
    # Only the columns that say WHOSE a list is (the prediction scope).
    cursor.execute(sql.SQL("GRANT SELECT (id, owner_user_id, household_id) ON shopping_lists TO {}").format(
        ident(READER_ROLE)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("environment", choices=ENVIRONMENTS)
    parser.add_argument("--apply", action="store_true", help="really create/grant (default: only look)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    found = owner_url(args.environment)
    if not found:
        print(f"None of {ENVIRONMENTS[args.environment]} is set.")
        return 1
    variable, url = found
    target = make_url(url)
    print(f"environment : {args.environment}   (from {variable})")
    print(f"database    : user={target.username}  host={target.host}  db={target.database}")

    # Refuse to run as one of the limited roles: it could not grant anything,
    # and it would mean the wrong URL is in this variable.
    if target.username in (APP_ROLE, READER_ROLE):
        print(f"STOP: {variable} uses the limited role {target.username}. This script needs the owner URL.")
        return 1

    # Ask the DATABASE which environment it is: the URL may be the wrong one.
    ok, message = check_environment(url, args.environment)
    print(f"label       : {message}")
    if not ok:
        print("STOP: nothing was changed.")
        return 1

    engine = create_engine(url, pool_pre_ping=True)
    raw = engine.raw_connection()          # psycopg connection: needed for sql.Literal/Identifier
    try:
        with raw.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            (database,) = cursor.fetchone()
            for role in (APP_ROLE, READER_ROLE):
                print(f"role {role:17}: {'exists' if role_exists(cursor, role) else 'MISSING (would be created)'}")
        raw.rollback()

        if not args.apply:
            print("(looking only: add --apply to create roles and grants)")
            return 0

        if args.environment == "production":
            typed = input(f"Production: type the host name ({target.host}) to confirm: ").strip()
            if typed != target.host:
                print("Not confirmed. Nothing changed.")
                return 1

        try:
            with raw.cursor() as cursor:
                apply_roles(cursor, database)
            raw.commit()                    # everything or nothing
        except BaseException:
            raw.rollback()
            raise
        print("\nDone. Next: build the connection URL of each role with the SAME host and database name,")
        print("put the new URLs in .env / your hosting settings, then run scripts/check_app_access.py.")
        return 0
    finally:
        raw.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

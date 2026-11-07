"""READ-ONLY inspection: what is in each database the project can reach?

WHY
---
Four environment variables point to databases that all have the same name
(`neondb`) and the same user (`neondb_owner`), so the names say nothing about
which one is production, test or synthetic. This script asks each database
the same questions and prints the answers, so the decisions about roles,
backups and migrations are made from facts, not from names.

SAFETY
------
* Every query runs in its own transaction that starts with
  `SET TRANSACTION READ ONLY`: PostgreSQL itself refuses any write.
* Passwords are never printed (only user, host and database name).
* Nothing is read from tables other than counts, dates and the names of
  triggers; no emails or other personal data are shown.

USAGE (PowerShell, from the project root)
-----------------------------------------
    uv run python scripts/inspect_databases.py

To include the production branch, whose URL is NOT in .env, give it for this
terminal only (copy it from Neon: Branch "production" > Connect). Closing the
terminal forgets it:

    $env:PRODUCTION_URL = "postgresql://..."
    uv run python scripts/inspect_databases.py
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VARIABLES = ["DATABASE_URL", "TEST_DATABASE_URL", "SYNTHETIC_DATABASE_URL",
             "SOURCE_DATABASE_URL", "PRODUCTION_URL"]

QUESTIONS = [
    ("environment label",
     "SELECT coalesce(shobj_description(oid, 'pg_database'), '(none)') FROM pg_database "
     "WHERE datname = current_database()"),
    ("migration level", "SELECT string_agg(version_num, ', ') FROM alembic_version"),
    ("users (all / synthetic)",
     "SELECT count(*) || ' / ' || count(*) FILTER (WHERE email LIKE '%@synthetic.invalid') FROM users"),
    ("households", "SELECT count(*) FROM households"),
    ("shopping lists", "SELECT count(*) FROM shopping_lists"),
    ("item_events (count, first, last)",
     "SELECT count(*) || ', ' || coalesce(min(occurred_at)::date::text, '-') || ', ' "
     "|| coalesce(max(occurred_at)::date::text, '-') FROM item_events"),
    ("triggers on item_events / shopping_lists",
     "SELECT coalesce(string_agg(tgname, ', '), '(none)') FROM pg_trigger "
     "WHERE NOT tgisinternal AND tgrelid IN (to_regclass('item_events'), to_regclass('shopping_lists'))"),
    ("this role can UPDATE / DELETE item_events",
     "SELECT has_table_privilege(current_user, 'item_events', 'UPDATE') || ' / ' "
     "|| has_table_privilege(current_user, 'item_events', 'DELETE')"),
    ("this role owns item_events",
     "SELECT pg_get_userbyid(relowner) = current_user FROM pg_class WHERE relname = 'item_events'"),
]


def ask(engine, sql: str) -> str:
    """One question, one read-only transaction. Errors are answers too."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))      # must be the first statement
            value = connection.execute(text(sql)).scalar()
            return "-" if value is None else str(value)
    except Exception as error:                                         # noqa: BLE001 - report, never crash
        first = str(getattr(error, "orig", error)).strip().splitlines()[0]
        return f"({first[:70]})"


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    targets = [(name, os.getenv(name)) for name in VARIABLES if os.getenv(name)]
    seen: dict[str, str] = {}
    for name, url in targets:
        parsed = make_url(url)
        key = f"{parsed.host}/{parsed.database}"
        print("=" * 78)
        print(f"{name}")
        print(f"  user={parsed.username}  host={parsed.host}  db={parsed.database}")
        if key in seen:
            print(f"  SAME DATABASE AS {seen[key]}")
        seen.setdefault(key, name)
        engine = create_engine(url, pool_pre_ping=True)
        try:
            for label, sql in QUESTIONS:
                print(f"  {label:44} {ask(engine, sql)}")
        finally:
            engine.dispose()
    if not any(n == "PRODUCTION_URL" for n, _ in targets):
        print("\n(PRODUCTION_URL is not set: the production branch was not inspected.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

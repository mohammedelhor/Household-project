"""Run Alembic on ONE named database, showing exactly which one first.

WHY
---
The project has several databases that all look alike (same database name,
same user). Running a bare `alembic upgrade head` applies the migration to
whatever DATABASE_URL happens to hold in that terminal. This script removes
the guesswork: you name the environment, it prints where that is, and it
changes nothing unless you add --apply.

    uv run python scripts/migrate.py test               # look only
    uv run python scripts/migrate.py test --apply       # upgrade to head

ENVIRONMENTS
------------
    dev         OWNER_DATABASE_URL, else DATABASE_URL
                                        (once the API runs as the limited role
                                         app_runtime, DATABASE_URL is THAT role's
                                         URL, so the owner URL lives in OWNER_DATABASE_URL)
    test        TEST_DATABASE_URL       (integration tests)
    synthetic   SYNTHETIC_DATABASE_URL  (generated data)
    production  PRODUCTION_URL          (NOT in .env: set it in this terminal only,
                                         and you must type the host name to confirm)

The migration runs with the owner role of that URL. Passwords are never
printed. Rolling back one step (if something looks wrong):

    $env:DATABASE_URL = <that environment's URL>; uv run alembic -c backend/alembic.ini downgrade -1
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))          # so `backend` can be imported from scripts/

from backend.app.core.db_safety import check_environment  # noqa: E402
# environment -> variables to try, in order (the first one that is set wins)
ENVIRONMENTS = {
    "dev": ["OWNER_DATABASE_URL", "DATABASE_URL"],
    "test": ["TEST_DATABASE_URL"],
    "synthetic": ["SYNTHETIC_DATABASE_URL"],
    "production": ["PRODUCTION_URL"],
}
# Roles that must never run migrations: they have no right to change the schema.
LIMITED_ROLES = ("app_runtime", "analytics_reader")


def find_url(environment: str) -> tuple[str, str] | None:
    """(variable name, URL) of the first variable that is set, or None."""
    for variable in ENVIRONMENTS[environment]:
        if os.getenv(variable):
            return variable, os.environ[variable]
    return None


def alembic(url: str, *args: str) -> str:
    """Run one alembic command against `url`; return its last output line."""
    # Both names are set for this subprocess only: backend/migrations/env.py
    # prefers ALEMBIC_DATABASE_URL, so a stale value of it in the terminal
    # must not be able to send the migration to a different database.
    env = {**os.environ, "DATABASE_URL": url, "ALEMBIC_DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", *args],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep="\n")
        raise SystemExit(f"alembic {' '.join(args)} failed")
    lines = [line for line in result.stdout.splitlines() if line and not line.startswith("INFO")]
    return lines[-1].split()[0] if lines else "(none)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("environment", choices=ENVIRONMENTS)
    parser.add_argument("--apply", action="store_true", help="really upgrade (default: only look)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    found = find_url(args.environment)
    if not found:
        print(f"None of {ENVIRONMENTS[args.environment]} is set.")
        return 1
    variable, url = found

    parsed = make_url(url)
    print(f"environment : {args.environment}   (from {variable})")
    print(f"database    : user={parsed.username}  host={parsed.host}  db={parsed.database}")

    if parsed.username in LIMITED_ROLES:
        print(f"STOP: {variable} uses the limited role {parsed.username}, which cannot change the schema.")
        print("Put the OWNER's URL in OWNER_DATABASE_URL (dev) or the environment's own variable.")
        return 1

    others = {n: make_url(found_other[1]).host for n in ENVIRONMENTS
              if n != args.environment and (found_other := find_url(n))}
    clash = [n for n, host in others.items() if host == parsed.host]
    if clash:
        print(f"STOP: the same host is also used by {clash}. Fix the URLs first.")
        return 1

    # Ask the DATABASE which environment it is: the URL may be the wrong one.
    ok, message = check_environment(url, args.environment)
    print(f"label       : {message}")
    if not ok:
        print("STOP: nothing was changed.")
        return 1

    current, head = alembic(url, "current"), alembic(url, "heads")
    print(f"current     : {current}")
    print(f"head (code) : {head}")
    if current == head:
        print("Already up to date. Nothing to do.")
        return 0
    print("This database is BEHIND.")
    if not args.apply:
        print("(looking only: add --apply to upgrade)")
        return 0

    if args.environment == "production":
        typed = input(f"Production: type the host name ({parsed.host}) to confirm: ").strip()
        if typed != parsed.host:
            print("Not confirmed. Nothing changed.")
            return 1

    alembic(url, "upgrade", "head")
    print(f"upgraded    : now at {alembic(url, 'current')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

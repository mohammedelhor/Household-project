"""Write (or read) the environment label inside ONE database.

WHY
---
See backend/app/core/db_safety.py: migrations, role setup, the tests, the
generator and (optionally) the API ask the DATABASE which environment it is,
because URLs can be mixed up and all our databases look alike. This script
puts that label there, once per database.

    uv run python scripts/mark_environment.py dev                 # look only
    uv run python scripts/mark_environment.py dev --apply         # write the label

The environment you name picks the variable to read (the OWNER's URL):
    dev OWNER_DATABASE_URL (else DATABASE_URL) | test TEST_DATABASE_URL
    synthetic SYNTHETIC_DATABASE_URL | production PRODUCTION_URL (this terminal only)

BEFORE you --apply, compare the host it prints with the branch in the Neon
console (Branch > Connect shows the same ep-... host). The label is only as
right as the URL you mark it with, so this is the one moment to check by eye.

It refuses to replace a DIFFERENT existing label unless you add --force.
Production asks you to type the host name, like migrate.py.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))          # so `backend` can be imported from scripts/

from backend.app.core.db_safety import (  # noqa: E402
    ENVIRONMENT_VARIABLES, ENVIRONMENTS, read_marker, set_marker)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("environment", choices=ENVIRONMENTS)
    parser.add_argument("--apply", action="store_true", help="really write the label (default: only look)")
    parser.add_argument("--force", action="store_true", help="replace a different existing label")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    found = next(((v, os.environ[v]) for v in ENVIRONMENT_VARIABLES[args.environment] if os.getenv(v)), None)
    if not found:
        print(f"None of {ENVIRONMENT_VARIABLES[args.environment]} is set.")
        return 1
    variable, url = found
    target = make_url(url)
    print(f"environment : {args.environment}   (from {variable})")
    print(f"database    : user={target.username}  host={target.host}  db={target.database}")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            current = read_marker(connection)
        print(f"label now   : {current or '(none)'}")
        if current == args.environment:
            print("Already marked. Nothing to do.")
            return 0
        if current and not args.force:
            print(f"STOP: this database is already marked '{current}'. Use --force only if that was a mistake.")
            return 1
        if not args.apply:
            print(f"(looking only: add --apply to mark it '{args.environment}')")
            return 0
        if args.environment == "production":
            typed = input(f"Production: type the host name ({target.host}) to confirm: ").strip()
            if typed != target.host:
                print("Not confirmed. Nothing changed.")
                return 1
        with engine.begin() as connection:             # one transaction, committed at the end
            set_marker(connection, args.environment)
        with engine.connect() as connection:
            print(f"label now   : {read_marker(connection)}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())

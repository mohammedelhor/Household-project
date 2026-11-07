"""Every database states WHICH environment it is, and tools check it.

THE PROBLEM THIS SOLVES
-----------------------
Our databases all look alike: same database name (`neondb`), same user
(`neondb_owner`), and a host name like `ep-young-butterfly-...` that says
nothing about its purpose. A single wrong line in `.env` (or a variable left
over in a terminal) made DATABASE_URL point at production without any sign.
Comparing URLs with each other cannot fix this, because the URLs themselves
are what may be wrong.

THE FIX: A LABEL STORED INSIDE THE DATABASE
-------------------------------------------
PostgreSQL lets a database carry a comment. We store

    household-environment=<dev | test | synthetic | production>

on each database (`scripts/mark_environment.py`). Anything dangerous then
asks the DATABASE who it is, not the URL:

* migrations and role setup  -> expect the environment they were asked to touch
* the test-suite             -> expects "test"
* the synthetic generator    -> expects "synthetic"
* the API (optional)         -> EXPECTED_DB_ENVIRONMENT in .env, e.g. "dev"

If the label differs, the tool stops BEFORE changing anything. Reading the
label needs no special right (every role can), so the API's limited role can
check it too; writing it needs the owner.

THE ONE EXCEPTION
-----------------
An UNMARKED database on this same computer (localhost, or the `postgres`
service in docker/CI) is accepted: there is no way to confuse it with Neon
production, and CI creates a fresh database every run. An unmarked database
anywhere else is refused until it is marked: "unknown" is treated as unsafe.

This module imports nothing from the application's settings, so scripts and
tests can use it without a full configuration.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

MARKER_PREFIX = "household-environment="
ENVIRONMENTS = ("dev", "test", "synthetic", "production")

# environment -> the variables that may hold its OWNER url, first one that is set wins.
ENVIRONMENT_VARIABLES: dict[str, list[str]] = {
    "dev": ["OWNER_DATABASE_URL", "DATABASE_URL"],
    "test": ["TEST_DATABASE_URL"],
    "synthetic": ["SYNTHETIC_DATABASE_URL"],
    "production": ["PRODUCTION_URL"],
}

LOCAL_HOSTS = {None, "", "localhost", "127.0.0.1", "::1", "postgres", "db"}


class EnvironmentMismatch(RuntimeError):
    """The database is not the environment the caller expected."""


def parse_marker(comment: str | None) -> str | None:
    """'household-environment=test' -> 'test'. Anything else -> None (unmarked)."""
    if not comment:
        return None
    comment = comment.strip()
    if not comment.startswith(MARKER_PREFIX):
        return None
    return comment[len(MARKER_PREFIX):].strip() or None


def read_marker(connection: Connection) -> str | None:
    """Ask the connected database which environment it says it is."""
    comment = connection.execute(text(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
        "WHERE datname = current_database()"
    )).scalar()
    return parse_marker(comment)


def set_marker(connection: Connection, environment: str) -> None:
    """Write the label. Needs to own the database. `environment` is validated
    against a fixed list because COMMENT cannot take a bound parameter."""
    if environment not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS}, got {environment!r}")
    name = connection.execute(text("SELECT current_database()")).scalar()
    quoted = connection.dialect.identifier_preparer.quote(name)
    connection.execute(text(f"COMMENT ON DATABASE {quoted} IS '{MARKER_PREFIX}{environment}'"))


def verdict(marker: str | None, expected: str, host: str | None) -> tuple[bool, str]:
    """The decision itself, with no database involved (so it is easy to test)."""
    if marker == expected:
        return True, f"database is marked '{marker}' (expected)"
    if marker is None:
        if host in LOCAL_HOSTS:
            return True, "database is unmarked but local (accepted)"
        return False, (f"this database ({host}) is not marked. If it really is '{expected}', "
                       f"run: uv run python scripts/mark_environment.py {expected} --apply")
    return False, (f"WRONG DATABASE: expected '{expected}' but this database says it is '{marker}' "
                   f"(host {host}). Check the URL you are using.")


def check_environment(url: str, expected: str) -> tuple[bool, str]:
    """Connect (read-only question) and return (ok, message)."""
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            marker = read_marker(connection)
        return verdict(marker, expected, make_url(url).host)
    finally:
        engine.dispose()


def require_environment(url: str, expected: str) -> str:
    """Like check_environment, but raises EnvironmentMismatch when not ok."""
    ok, message = check_environment(url, expected)
    if not ok:
        raise EnvironmentMismatch(message)
    return message

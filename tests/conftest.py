import os
from pathlib import Path
from dotenv import load_dotenv

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@pytest.fixture(scope="session")
def test_engine():
    database_url = os.getenv("TEST_DATABASE_URL")

    if not database_url:
        pytest.fail(
            "TEST_DATABASE_URL is required for database model tests."
        )

    url = make_url(database_url)
    # Safety guard: these tests write and delete real rows. Refuse to run if
    # TEST_DATABASE_URL points at the same database as the application
    # (DATABASE_URL). In CI both are intentionally the same throwaway
    # database, so the check is skipped there (GitHub sets CI=true).
    app_url = os.getenv("DATABASE_URL")
    if app_url and not os.getenv("CI"):
        app = make_url(app_url)
        if (app.host, app.database) == (url.host, url.database):
            pytest.fail(
                "TEST_DATABASE_URL points to the same database as "
                "DATABASE_URL; refusing to run tests that write data."
            )

    if url.get_backend_name() != "postgresql":
        pytest.fail("Model tests require PostgreSQL.")

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
    )

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(test_engine):
    connection = test_engine.connect()
    transaction = connection.begin()

    TestingSessionLocal = sessionmaker(
        bind=connection,
        autoflush=False,
        autocommit=False,
        join_transaction_mode="create_savepoint",
    )

    session = TestingSessionLocal()

    try:
        yield session
    finally:
        session.close()

        if transaction.is_active:
            transaction.rollback()

        connection.close()
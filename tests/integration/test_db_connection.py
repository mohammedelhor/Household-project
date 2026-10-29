import pytest
from sqlalchemy import text

from backend.app.db.database import engine

@pytest.mark.integration
def test_database_connection():
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        assert result.scalar() == 1


if __name__ == "__main__":
    test_database_connection()
    print("Database connection successful!")

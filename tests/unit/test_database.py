from unittest.mock import MagicMock, patch

from backend.app.db.database import get_db

def test_get_db_closes_session():
    mock_session = MagicMock()

    with patch(
        "backend.app.db.database.SessionLocal",
        return_value=mock_session,
    ):
        db_generator = get_db()

        db = next(db_generator)

        assert db is mock_session
        mock_session.close.assert_not_called()

        db_generator.close()

        mock_session.close.assert_called_once()
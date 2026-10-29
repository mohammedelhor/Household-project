
import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings


def test_settings_rejects_short_jwt_secret():
    with pytest.raises(
        ValidationError,
        match="JWT_SECRET_KEY must contain at least 32 characters",
    ):
        Settings(
            database_url="sqlite://",
            jwt_secret_key="short-secret",
        )

def test_settings_rejects_placeholder_jwt_secret():
    with pytest.raises(
        ValidationError,
        match="JWT_SECRET_KEY must not use a placeholder value",
    ):
        Settings(
            database_url="sqlite://",
            jwt_secret_key="                        change-me",
        )

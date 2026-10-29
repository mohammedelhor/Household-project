
import pytest
from pydantic import ValidationError

from backend.app.schemas.auth import UserRegister, UserLogin


VALID_EMAIL = "Test@Example.com"
VALID_PASSWORD = "StrongPassword123!"


def test_register_normalizes_email():
    data = UserRegister(
        email=f"  {VALID_EMAIL}  ",
        password=VALID_PASSWORD,
    )

    assert data.email == "test@example.com"


def test_register_rejects_invalid_email():
    with pytest.raises(ValidationError):
        UserRegister(
            email="not-an-email",
            password=VALID_PASSWORD,
        )


@pytest.mark.parametrize("password", ["Short123!", "a" * 11])
def test_register_rejects_password_shorter_than_12(password):
    with pytest.raises(ValidationError):
        UserRegister(email=VALID_EMAIL, password=password)


def test_register_accepts_password_of_12_characters():
    data = UserRegister(
        email=VALID_EMAIL,
        password="a" * 12,
    )

    assert len(data.password) == 12


def test_register_rejects_password_longer_than_128():
    with pytest.raises(ValidationError):
        UserRegister(
            email=VALID_EMAIL,
            password="a" * 129,
        )


def test_login_accepts_one_character_password():
    data = UserLogin(
        email=VALID_EMAIL,
        password="x",
    )

    assert data.password == "x"


def test_login_rejects_password_longer_than_128():
    with pytest.raises(ValidationError):
        UserLogin(
            email=VALID_EMAIL,
            password="a" * 129,
        )


def test_login_normalizes_email():
    data = UserLogin(
        email=f"  {VALID_EMAIL}  ",
        password=VALID_PASSWORD,
    )

    assert data.email == "test@example.com"


from unittest.mock import MagicMock
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from backend.app.core.security import (
    create_access_token,
    hash_password,
)
from backend.app.db.database import get_db
from backend.app.db.models import User
from backend.app.main import app


@pytest.fixture
def fake_db():
    return MagicMock()


@pytest.fixture
def client(fake_db):
    def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def make_user(
    user_id=1,
    email="test@example.com",
    password="StrongPassword123!",
):
    return User(
        id=user_id,
        email=email,
        password_hash=hash_password(password),
    )


def test_register_success(client, fake_db):
    fake_db.refresh.side_effect = (
        lambda user: setattr(user, "id", 1)
    )

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "Test@Example.com",
            "password": "StrongPassword123!",
        },
    )

    assert response.status_code == 201
    assert response.json()["email"] == "test@example.com"
    assert "password_hash" not in response.json()

    created_user = fake_db.add.call_args.args[0]

    assert created_user.password_hash != "StrongPassword123!"
    assert created_user.password_hash.startswith("$argon2")


def test_register_duplicate_email(client, fake_db):
    fake_db.commit.side_effect = IntegrityError(
        "INSERT",
        {},
        Exception("duplicate email"),
    )

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "test@example.com",
            "password": "StrongPassword123!",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Email is already registered."
    fake_db.rollback.assert_called_once()


def test_login_success(client, fake_db):
    fake_db.scalar.return_value = make_user()

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "test@example.com",
            "password": "StrongPassword123!",
        },
    )

    assert response.status_code == 200

    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str)
    assert body["access_token"]


def test_login_wrong_password(client, fake_db):
    fake_db.scalar.return_value = make_user()

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "test@example.com",
            "password": "WrongPassword123!",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


def test_login_unknown_email(client, fake_db):
    fake_db.scalar.return_value = None

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": "unknown@example.com",
            "password": "StrongPassword123!",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


def test_me_without_token(client):
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401


def test_me_with_valid_token(client, fake_db):
    user = make_user()
    fake_db.get.return_value = user

    token = create_access_token(subject=str(user.id))

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == user.id
    assert response.json()["email"] == user.email
    assert "password_hash" not in response.json()


def test_me_with_invalid_token(client):
    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.token.value"},
    )

    assert response.status_code == 401
    

def test_me_with_expired_token(client):
    from backend.app.core.config import settings

    now = datetime.now(timezone.utc)

    token = jwt.encode(
        {
            "sub": "1",
            "iat": now - timedelta(minutes=10),
            "exp": now - timedelta(minutes=5),
            "jti": "expired-test-token",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_me_with_invalid_signature_token(client):
    from backend.app.core.config import settings

    token = jwt.encode(
        {
            "sub": "1",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "jti": "invalid-signature-test",
        },
        "incorrect-signing-secret-for-testing-only",
        algorithm=settings.jwt_algorithm,
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_me_with_missing_subject_claim(client):
    from backend.app.core.config import settings

    now = datetime.now(timezone.utc)

    token = jwt.encode(
        {
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": "missing-sub-test",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_me_with_deleted_user(client, fake_db):
    from backend.app.core.security import create_access_token

    token = create_access_token(subject="999999")

    fake_db.get.return_value = None

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    fake_db.get.assert_called_once()

def test_me_with_non_numeric_subject(client, fake_db):
    token = create_access_token(subject="abc")

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    fake_db.get.assert_not_called()

def test_me_with_non_positive_subject(client, fake_db):
    token = create_access_token(subject="0")

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    fake_db.get.assert_not_called()

def test_me_with_null_subject(client, fake_db):
    from backend.app.core.config import settings

    now = datetime.now(timezone.utc)

    token = jwt.encode(
        {
            "sub": None,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": "null-sub-test",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    fake_db.get.assert_not_called()

def test_decode_access_token_rejects_non_string_subject(monkeypatch):
    from backend.app.core.security import decode_access_token

    monkeypatch.setattr(
        "backend.app.core.security.jwt.decode",
        lambda *args, **kwargs: {
            "sub": 123,
            "jti": "valid-token-id",
        },
    )

    assert decode_access_token("dummy-token") is None

def test_decode_access_token_rejects_blank_subject(monkeypatch):
    from backend.app.core.security import decode_access_token

    monkeypatch.setattr(
        "backend.app.core.security.jwt.decode",
        lambda *args, **kwargs: {
            "sub": "   ",
            "jti": "valid-token-id",
        },
    )

    assert decode_access_token("dummy-token") is None

def test_decode_access_token_rejects_non_string_jti(monkeypatch):
    from backend.app.core.security import decode_access_token

    monkeypatch.setattr(
        "backend.app.core.security.jwt.decode",
        lambda *args, **kwargs: {
            "sub": "1",
            "jti": 123,
        },
    )

    assert decode_access_token("dummy-token") is None

def test_decode_access_token_rejects_blank_jti(monkeypatch):
    from backend.app.core.security import decode_access_token

    monkeypatch.setattr(
        "backend.app.core.security.jwt.decode",
        lambda *args, **kwargs: {
            "sub": "1",
            "jti": "   ",
        },
    )

    assert decode_access_token("dummy-token") is None

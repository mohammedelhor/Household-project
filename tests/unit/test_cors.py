"""CORS: only the origins we list may call the API from a browser."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.main import configure_cors

SECRET = "a-long-enough-secret-for-the-validator-1234567890"


def make_settings(origins: str) -> Settings:
    # _env_file=None: ignore the developer's real .env while testing.
    return Settings(_env_file=None, database_url="sqlite://", jwt_secret_key=SECRET, cors_origins=origins)


def make_app(origins: list[str]) -> TestClient:
    application = FastAPI()

    @application.get("/ping")
    def ping():
        return {"ok": True}

    configure_cors(application, origins)
    return TestClient(application)


def preflight(client: TestClient, origin: str, method: str = "GET"):
    return client.options(
        "/ping",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization",
        },
    )


# ---- configuration ---------------------------------------------------------

def test_origins_are_split_and_trimmed():
    settings = make_settings(" http://localhost:5173 , https://app.example.com ,,")
    assert settings.cors_origin_list == ["http://localhost:5173", "https://app.example.com"]


def test_default_is_no_origins():
    assert make_settings("").cors_origin_list == []


@pytest.mark.parametrize("bad", [
    "*",                          # every website
    "http://*.example.com",       # wildcard inside
    "localhost:5173",             # no scheme
    "http://localhost:5173/",     # trailing slash never matches in a browser
    "https://example.com/app",    # an origin has no path
])
def test_bad_origins_are_rejected(bad):
    with pytest.raises(ValidationError):
        make_settings(bad)


# ---- behaviour -------------------------------------------------------------

def test_listed_origin_is_allowed():
    client = make_app(["http://localhost:5173"])

    response = preflight(client, "http://localhost:5173")

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in response.headers


def test_unlisted_origin_gets_no_permission():
    client = make_app(["http://localhost:5173"])

    response = preflight(client, "https://evil.example.com")

    assert "access-control-allow-origin" not in response.headers


def test_unlisted_method_is_refused():
    client = make_app(["http://localhost:5173"])

    response = preflight(client, "http://localhost:5173", method="PUT")

    assert response.status_code == 400


def test_without_origins_nothing_is_added():
    client = make_app([])

    response = client.get("/ping", headers={"Origin": "http://localhost:5173"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
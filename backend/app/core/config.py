
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str

    # Optional safety net: the environment this API is MEANT to talk to
    # ("dev" on your computer, "production" on the host). When set, the API
    # asks the database for its label at startup and refuses to start if they
    # differ, so a wrong DATABASE_URL cannot go unnoticed. Unset = no check.
    expected_db_environment: Literal["dev", "test", "synthetic", "production"] | None = None

    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(
        default=15,
        gt=0,
        le=60,
    )
        # Browser origins allowed to call this API from JavaScript, comma-separated,
    # e.g. "http://localhost:5173,https://app.example.com". EMPTY (the default)
    # means no cross-origin access at all. Kept as plain text here and split in
    # `cors_origin_list`, because a comma list is easier to write in .env than JSON.
    cors_origins: str = ""

    # Where the data pipeline publishes the suggestions the API serves
    # (written by `python -m ml.suggest --publish`). The API only reads this
    # file: it never queries the data/ML layers or their sources.
    suggestions_path: Path = PROJECT_ROOT / "data" / "serving" / "suggestions.json"
    # After this many days without a new file the API still answers, but
    # flags the answer as stale so a client can say so.
    suggestions_stale_after_days: int = Field(default=7, gt=0)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    @property
    def cors_origin_list(self) -> list[str]:
        """The allowed origins as a clean list (empty entries dropped)."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def validate_cors_origins(self):
        # An origin is exactly scheme + host (+ port): no path, no trailing "/",
        # and never "*". A browser compares origins as exact text, so
        # "http://localhost:5173/" would silently never match, and "*" would
        # let every website on the internet read answers meant for our users.
        for origin in self.cors_origin_list:
            if origin == "*" or "*" in origin:
                raise ValueError("CORS_ORIGINS must list explicit origins, not '*'.")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"CORS origin must start with http:// or https://: {origin!r}")
            if origin.endswith("/") or origin.count("/") != 2:
                raise ValueError(f"CORS origin must be scheme://host[:port] with no path or trailing '/': {origin!r}")
        return self

    @model_validator(mode="after")
    def validate_jwt_secret(self):
        secret = self.jwt_secret_key.get_secret_value()

        if len(secret) < 32:
            raise ValueError(
                "JWT_SECRET_KEY must contain at least 32 characters."
            )

        placeholders = {
            "change-me",
            "replace-me",
            "your-secret-key",
        }

        if secret.strip().lower() in placeholders:
            raise ValueError(
                "JWT_SECRET_KEY must not use a placeholder value."
            )

        return self


settings = Settings()

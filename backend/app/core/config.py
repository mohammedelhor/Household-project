
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str

    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(
        default=15,
        gt=0,
        le=60,
    )

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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

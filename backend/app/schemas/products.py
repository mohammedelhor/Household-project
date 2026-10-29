
from pydantic import BaseModel, ConfigDict, Field, field_validator
from backend.app.core.units import normalize_unit

class ProductCreate(BaseModel):
    """Validated input for creating a product."""

    name: str = Field(min_length=1, max_length=200)
    default_unit: str | None = Field(default=None, max_length=30)
    category: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Product name cannot be empty.")
        return value

    @field_validator("default_unit")
    @classmethod
    def validate_default_unit(cls, value: str | None) -> str | None:
        if value is None:
           return None

        value = value.strip()
        if not value:
            return None

        return normalize_unit(value)


    @field_validator("category")
    @classmethod
    def clean_category(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return value.strip() or None


class ProductRead(BaseModel):
    """Public representation of a product."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    normalized_name: str
    default_unit: str | None
    category: str | None
    is_active: bool

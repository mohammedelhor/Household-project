import pytest

from backend.app.core.units import SUPPORTED_UNITS, normalize_unit


@pytest.mark.parametrize(
    "unit",
    ["kg", "g", "liter", "ml", "piece"],
)
def test_normalize_unit_accepts_supported_units(unit: str) -> None:
    assert normalize_unit(unit) == unit


def test_normalize_unit_is_case_insensitive() -> None:
    assert normalize_unit("KG") == "kg"
    assert normalize_unit(" Liter ") == "liter"


def test_normalize_unit_rejects_unsupported_unit() -> None:
    with pytest.raises(ValueError, match="Unsupported unit"):
        normalize_unit("box")


def test_normalize_unit_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="Unsupported unit"):
        normalize_unit("   ")


def test_supported_units_contains_exactly_approved_units() -> None:
    assert SUPPORTED_UNITS == {"kg", "g", "liter", "ml", "piece"}
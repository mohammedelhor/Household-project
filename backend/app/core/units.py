
"""Canonical units supported by Household Intelligence."""

from typing import Final

SUPPORTED_UNITS: Final[frozenset[str]] = frozenset(
    {
        "kg",
        "g",
        "liter",
        "ml",
        "piece",
    }
)


def normalize_unit(value: str) -> str:
    """Normalize and validate a supported unit."""

    if not isinstance(value, str):
        raise TypeError("Unit must be a string.")

    unit = value.strip().casefold()

    if unit not in SUPPORTED_UNITS:
        allowed = ", ".join(sorted(SUPPORTED_UNITS))
        raise ValueError(
            f"Unsupported unit '{value}'. Supported units: {allowed}."
        )

    return unit

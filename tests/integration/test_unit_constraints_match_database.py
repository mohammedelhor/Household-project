"""The unit CHECK constraints in PostgreSQL must allow exactly the units the code supports.

The models generate their constraint text from SUPPORTED_UNITS, while each
migration keeps a FROZEN copy of the list (so old migrations never change).
The two can drift: add a unit to core/units.py without a new migration and the
application accepts a unit that the database refuses. This test reads the
constraints that really exist in the database and compares the sets of units.

PostgreSQL rewrites the expression when it stores it (for example
`unit = ANY (ARRAY['g'::text, ...])`), so the text is not compared directly:
only the quoted unit names are extracted and compared as a set.
"""
import re

import pytest
from sqlalchemy import text

from backend.app.core.units import SUPPORTED_UNITS

CONSTRAINTS = [
    "ck_shopping_list_item_unit_supported",
    "ck_product_default_unit_supported",
    "ck_item_event_unit_supported",
]


@pytest.mark.integration
@pytest.mark.parametrize("name", CONSTRAINTS)
def test_the_database_allows_exactly_the_supported_units(db_session, name):
    definition = db_session.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
        {"n": name},
    ).scalar_one_or_none()

    assert definition is not None, f"{name} does not exist in the database"
    assert set(re.findall(r"'([^']+)'", definition)) == set(SUPPORTED_UNITS)
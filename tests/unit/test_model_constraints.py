"""The unit CHECK constraints must be part of the MODELS, not only of the migrations.

WHY THIS TEST EXISTS
--------------------
A CheckConstraint only becomes part of a table if it is passed to the table:
inside `__table_args__` (or as a Column argument). Written as a loose statement
in the class body it creates an object that is thrown away, so SQLAlchemy's
metadata never learns about it. The migration still adds the constraint to
PostgreSQL, so the real database stays protected and nothing visibly breaks,
but the model no longer describes the database (and `alembic check` cannot
notice: it does not compare CHECK constraints). This test looks at the metadata
itself, so a misplaced constraint fails here.
"""
import pytest

from backend.app.core.units import SUPPORTED_UNITS
from backend.app.db.models import Base, ItemEvent, Product, ShoppingListItem

# table -> the unit constraint it must carry
UNIT_CONSTRAINTS = [
    (ShoppingListItem, "ck_shopping_list_item_unit_supported"),
    (Product, "ck_product_default_unit_supported"),
    (ItemEvent, "ck_item_event_unit_supported"),
]


def _check_texts(model) -> dict[str, str]:
    return {c.name: str(c.sqltext) for c in model.__table__.constraints
            if c.name and c.__class__.__name__ == "CheckConstraint"}


@pytest.mark.parametrize("model, name", UNIT_CONSTRAINTS)
def test_the_unit_constraint_is_on_the_table(model, name):
    assert name in _check_texts(model)


@pytest.mark.parametrize("model, name", UNIT_CONSTRAINTS)
def test_the_unit_constraint_lists_every_supported_unit(model, name):
    text = _check_texts(model)[name]
    for unit in SUPPORTED_UNITS:
        assert f"'{unit}'" in text


def test_every_named_check_in_the_models_belongs_to_a_table():
    """No constraint is registered twice or on the wrong table: each name is unique."""
    names = [c.name for table in Base.metadata.tables.values()
             for c in table.constraints if c.name]
    assert len(names) == len(set(names))
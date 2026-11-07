"""GET /products?search=: look in the catalog before creating a product."""

import pytest

from backend.app.db.models import Product
from backend.app.services.product_service import normalize_product_name

from tests.unit.test_shopping_routes import api_env, make_user  # noqa: F401  (fixture)


def _login(db, active_user):
    user = make_user("a@example.com")
    db.add(user)
    db.commit()
    active_user["user"] = user


def _add_products(db, *names, inactive=()):
    for name in names:
        db.add(Product(
            name=name,
            normalized_name=normalize_product_name(name),
            is_active=name not in inactive,
        ))
    db.commit()


def _names(response):
    return [item["name"] for item in response.json()]


def test_without_search_everything_active_is_listed_alphabetically(api_env):
    client, db, active_user = api_env
    _login(db, active_user)
    _add_products(db, "Rice", "Milk", "Old thing", inactive=("Old thing",))

    response = client.get("/api/v1/products")

    assert response.status_code == 200
    assert _names(response) == ["Milk", "Rice"]


def test_search_ignores_case_and_extra_spaces(api_env):
    client, db, active_user = api_env
    _login(db, active_user)
    _add_products(db, "Whole Milk", "Rice")

    assert _names(client.get("/api/v1/products", params={"search": "  MILK "})) == ["Whole Milk"]


def test_names_starting_with_the_text_come_first(api_env):
    client, db, active_user = api_env
    _login(db, active_user)
    _add_products(db, "Buttermilk", "Milk powder", "Milk")

    # "Buttermilk" is alphabetically first but only CONTAINS "milk".
    assert _names(client.get("/api/v1/products", params={"search": "milk"})) == [
        "Milk", "Milk powder", "Buttermilk"]


def test_wildcards_in_the_search_text_are_plain_characters(api_env):
    client, db, active_user = api_env
    _login(db, active_user)
    _add_products(db, "Milk", "100% juice", "snake_case")

    assert _names(client.get("/api/v1/products", params={"search": "%"})) == ["100% juice"]
    assert _names(client.get("/api/v1/products", params={"search": "_"})) == ["snake_case"]


def test_arabic_names_are_searchable(api_env):
    client, db, active_user = api_env
    _login(db, active_user)
    _add_products(db, "حليب", "أرز")

    assert _names(client.get("/api/v1/products", params={"search": "حلي"})) == ["حليب"]


def test_inactive_products_are_never_returned(api_env):
    client, db, active_user = api_env
    _login(db, active_user)
    _add_products(db, "Milk", inactive=("Milk",))

    assert client.get("/api/v1/products", params={"search": "milk"}).json() == []


def test_limit_and_offset_page_without_gaps_or_repeats(api_env):
    client, db, active_user = api_env
    _login(db, active_user)
    _add_products(db, *[f"Item {n:02d}" for n in range(5)])

    first = _names(client.get("/api/v1/products", params={"limit": 2}))
    second = _names(client.get("/api/v1/products", params={"limit": 2, "offset": 2}))
    third = _names(client.get("/api/v1/products", params={"limit": 2, "offset": 4}))

    assert first + second + third == [f"Item {n:02d}" for n in range(5)]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"offset": -1}, {"search": "x" * 101}])
def test_out_of_range_parameters_are_rejected(api_env, params):
    client, db, active_user = api_env
    _login(db, active_user)

    assert client.get("/api/v1/products", params=params).status_code == 422


def test_search_of_only_spaces_is_rejected(api_env):
    client, db, active_user = api_env
    _login(db, active_user)

    assert client.get("/api/v1/products", params={"search": "   "}).status_code == 422


def test_search_requires_authentication():
    from fastapi.testclient import TestClient
    from backend.app.main import app

    assert TestClient(app).get("/api/v1/products").status_code == 401
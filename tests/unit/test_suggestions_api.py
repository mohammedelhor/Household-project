"""Tests for GET /api/v1/suggestions and the file it serves.

The API reads ONE json file. These tests write that file by hand (the shape
is the contract documented in ml/suggest.py) and check what the API makes of
it, so no database, pipeline or ML code is needed to run them.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.api.routes.auth import get_current_user
from backend.app.api.routes.suggestions import get_now
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.services import suggestions as service

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)      # today = Nov 1


def entry(product_id, name, expected, gap=5, confidence="high", gaps_used=8):
    return {"product_id": product_id, "product_name": name,
            "last_purchase_date": "2026-10-20" if expected else None,
            "typical_gap_days": gap if expected else None,
            "expected_date": expected, "confidence": confidence, "gaps_used": gaps_used}


def file_payload(*entries, generated_at=NOW, schema_version=1):
    return {"schema_version": schema_version, "generated_at": generated_at.isoformat(),
            "as_of_date": generated_at.date().isoformat(),
            "dataset": {"run_id": "986445dca14a", "input_fingerprint": "f" * 64},
            "suggestions": list(entries)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "suggestions.json"
    monkeypatch.setattr(settings, "suggestions_path", path)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = lambda: object()   # any logged-in user
    app.dependency_overrides[get_now] = lambda: NOW
    try:
        yield TestClient(app), path
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def publish(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_status_and_days_are_worked_out_from_today(client):
    http, path = client
    publish(path, file_payload(
        entry(1, "Milk", "2026-10-30"),          # 2 days ago     -> overdue
        entry(2, "Rice", "2026-11-01"),          # today          -> due_soon
        entry(3, "Eggs", "2026-11-03"),          # in 2 days      -> due_soon
        entry(4, "Sugar", "2026-11-10"),         # in 9 days      -> not_yet
    ))
    body = http.get("/api/v1/suggestions").json()
    got = {s["product_name"]: (s["days_until"], s["status"]) for s in body["suggestions"]}

    assert got == {"Milk": (-2, "overdue"), "Rice": (0, "due_soon"),
                   "Eggs": (2, "due_soon"), "Sugar": (9, "not_yet")}
    assert body["dataset_run_id"] == "986445dca14a"
    assert body["stale"] is False


def test_most_urgent_first_and_unknown_last(client):
    http, path = client
    publish(path, file_payload(
        entry(1, "Sugar", "2026-11-10"), entry(2, "Olive oil", None, confidence="none", gaps_used=0),
        entry(3, "Milk", "2026-10-30"), entry(4, "Eggs", "2026-11-03")))
    names = [s["product_name"] for s in http.get("/api/v1/suggestions").json()["suggestions"]]
    assert names == ["Milk", "Eggs", "Sugar", "Olive oil"]


def test_a_product_without_rhythm_is_unknown(client):
    http, path = client
    publish(path, file_payload(entry(1, "Olive oil", None, confidence="none", gaps_used=0)))
    s = http.get("/api/v1/suggestions").json()["suggestions"][0]
    assert (s["status"], s["days_until"], s["expected_date"]) == ("unknown", None, None)


def test_the_same_file_gives_different_answers_on_different_days(client):
    """The reason `status` is not stored in the file."""
    http, path = client
    publish(path, file_payload(entry(1, "Milk", "2026-11-03")))
    assert http.get("/api/v1/suggestions").json()["suggestions"][0]["status"] == "due_soon"

    app.dependency_overrides[get_now] = lambda: NOW + timedelta(days=5)
    later = http.get("/api/v1/suggestions").json()["suggestions"][0]
    assert (later["status"], later["days_until"]) == ("overdue", -3)


def test_an_old_file_is_served_but_flagged_stale(client):
    http, path = client
    publish(path, file_payload(entry(1, "Milk", "2026-11-03"),
                               generated_at=NOW - timedelta(days=8)))
    response = http.get("/api/v1/suggestions")
    assert response.status_code == 200 and response.json()["stale"] is True


def test_exactly_at_the_limit_is_not_stale(client):
    http, path = client
    publish(path, file_payload(generated_at=NOW - timedelta(days=settings.suggestions_stale_after_days)))
    assert http.get("/api/v1/suggestions").json()["stale"] is False


def test_missing_file_gives_503_without_leaking_the_path(client):
    http, path = client
    response = http.get("/api/v1/suggestions")
    assert response.status_code == 503
    assert response.json() == {"detail": "Suggestions are not available yet."}
    assert str(path) not in response.text


@pytest.mark.parametrize("content", [
    "not json at all",
    json.dumps({"schema_version": 1}),                                       # wrong shape
    json.dumps(file_payload(schema_version=2)),                              # unknown version
    json.dumps({**file_payload(), "suggestions": [{"product_id": "x"}]}),    # bad entry
])
def test_unreadable_or_unknown_files_give_503(client, content):
    http, path = client
    path.write_text(content, encoding="utf-8")
    assert http.get("/api/v1/suggestions").status_code == 503


def test_login_is_required(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "suggestions_path", tmp_path / "suggestions.json")
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.pop(get_current_user, None)
    try:
        assert TestClient(app).get("/api/v1/suggestions").status_code == 401
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_backend_rule_matches_the_ml_rule():
    """The API repeats the status rule on purpose; this keeps the copies equal."""
    from ml import suggest as ml_suggest
    assert service.SOON_DAYS == ml_suggest.SOON_DAYS
    assert all(service.status_for(d) == ml_suggest.status_for(d) for d in range(-10, 15))


def test_service_does_not_import_the_ml_or_data_layers():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(service))
    imported = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) and n.module
                else a.name.split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert not imported & {"ml", "data_engineering"}
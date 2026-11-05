"""End-to-end test of the report: pipeline output -> baselines -> record.

Uses the same SQLite setup as test_pipeline.py, so no external service.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.models import Base
from data_engineering.pipeline import run_pipeline
from data_engineering.scopes import Scope
from data_engineering.synthetic_data import (PRODUCT_PROFILES, TWO_HOUSEHOLDS, generate_history,
                                             generate_households, write_to_database)
from data_engineering.transform import Event
from ml import report
from ml.report import (REFERENCE_NAME, StaleInputError, build_record, drop_rejected_items,
                       format_report, load_published_dataset, run_experiment, sample_sufficiency,
                       write_record)

END = datetime(2026, 10, 31, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture
def published(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        write_to_database(session, generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42))
        session.commit()
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    assert run_pipeline(engine=engine, raw_dir=raw, clean_dir=clean, now=NOW) == 0
    yield raw, clean, engine
    engine.dispose()


def test_report_runs_on_a_published_dataset(published):
    raw, clean, _ = published
    events, manifest, names, scopes = load_published_dataset(raw, clean)
    experiment = run_experiment(events, names, scopes)

    assert set(names.values()) == {p.name for p in PRODUCT_PROFILES}
    assert experiment["samples"]["scored"] > 50
    assert REFERENCE_NAME in experiment["results"]
    text = format_report(experiment, min_intervals=2)
    assert "median of intervals" in text and "Rice" in text
    assert "Retrospective Oracle Reference" in text and "UNSTABLE" in text or "1 interval" in text
    assert experiment["sufficiency"]["flags"] == ["single_scope"]      # one personal scope


def test_report_refuses_when_raw_files_changed_after_the_run(published):
    raw, clean, _ = published
    batch = sorted((raw / "item_events").glob("*.csv"))[0]
    batch.write_text(batch.read_text(encoding="utf-8") + "\n", encoding="utf-8")   # any byte change

    with pytest.raises(StaleInputError):
        load_published_dataset(raw, clean)


def test_report_needs_a_published_run(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_published_dataset(tmp_path / "raw", tmp_path / "clean")


def test_record_traces_back_to_dataset_and_code(published, tmp_path):
    raw, clean, _ = published
    events, manifest, names, scopes = load_published_dataset(raw, clean)
    record = build_record(run_experiment(events, names, scopes), manifest, min_intervals=2,
                          created_at=NOW, code={"commit": "abc123", "uncommitted_changes": False})
    path = write_record(record, tmp_path / "experiments")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["dataset"]["run_id"] == manifest["run_id"]
    assert saved["dataset"]["input_fingerprint"] == manifest["inputs"]["fingerprint"]
    assert saved["code"]["commit"] == "abc123"
    assert path.name.startswith("baseline_20261101T000000Z_")
    assert saved["config"]["min_intervals"] == 2
    assert saved["sample_definition"] == "settled_purchases_v1"
    assert saved["data_origin"] == "unknown"           # the fixture did not declare an origin
    assert saved["sufficiency"]["total"] == saved["samples"]["scored"]


def test_empty_scores_are_saved_as_null_not_nan():
    experiment = run_experiment([], {}, {})
    assert json.loads(json.dumps(experiment))["results"]["median of intervals"]["overall"]["mae"] is None
    assert "n/a" in format_report(experiment, min_intervals=2)


def test_events_of_rejected_items_are_dropped():
    t = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    mk = lambda i, item, kind, hours, q="1": Event(  # noqa: E731
        i, item, 1, 1, kind, Decimal(q), "kg", 1, t + timedelta(hours=hours))
    good = [mk(1, 10, "added", 0), mk(2, 10, "purchased", 1)]
    # same item, different quantity -> inconsistent -> rejected as a whole
    bad = [mk(3, 20, "added", 0), mk(4, 20, "purchased", 1, q="5")]

    assert [e.id for e in drop_rejected_items(good + bad, {1: Scope('household', 1)})] == [1, 2]


# -------------------------- scopes, origin, sufficiency --------------------------

@pytest.fixture
def two_households_published(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        items = generate_households(TWO_HOUSEHOLDS, end=END, days=180, seed=42)
        write_to_database(session, items, {h.key: h.name for h in TWO_HOUSEHOLDS})
        session.commit()
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    assert run_pipeline(engine=engine, raw_dir=raw, clean_dir=clean, now=NOW,
                        data_origin="synthetic") == 0
    yield raw, clean
    engine.dispose()


def test_the_report_keeps_the_two_households_apart(two_households_published):
    raw, clean = two_households_published
    events, manifest, names, scopes = load_published_dataset(raw, clean)
    experiment = run_experiment(events, names, scopes)
    series = experiment["results"]["median of intervals"]["per_series"]

    milk = {k: v for k, v in series.items() if k.endswith("Milk")}
    assert len(milk) == 2                              # one series per household
    assert experiment["sufficiency"]["n_scopes"] == 2
    assert "single_scope" not in experiment["sufficiency"]["flags"]


def test_synthetic_data_is_announced_in_the_report_and_the_record(two_households_published):
    raw, clean = two_households_published
    events, manifest, names, scopes = load_published_dataset(raw, clean)
    experiment = run_experiment(events, names, scopes)

    assert manifest["data_origin"] == "synthetic"
    assert "SYNTHETIC DATA" in format_report(experiment, min_intervals=2, data_origin="synthetic")
    assert "SYNTHETIC" not in format_report(experiment, min_intervals=2, data_origin="real")
    record = build_record(experiment, manifest, min_intervals=2, created_at=NOW, code={})
    assert record["data_origin"] == "synthetic"


def test_the_per_scope_error_is_small_for_a_regular_household(two_households_published):
    raw, clean = two_households_published
    events, manifest, names, scopes = load_published_dataset(raw, clean)
    series = run_experiment(events, names, scopes)["results"]["median of intervals"]["per_series"]
    # Family A buys milk every 7 days +-1: a pooled rule (about 18 days) would miss by ~11.
    assert min(v["mae"] for k, v in series.items() if k.endswith("Milk")) < 2


def test_sufficiency_flags_concentration():
    a, b = Scope("household", 1), Scope("household", 2)
    from ml.target import Sample
    t = datetime(2026, 9, 1, tzinfo=timezone.utc)
    mk = lambda scope, product: Sample(scope, product, t, t.date(), (5, 5), 5)  # noqa: E731
    flags = sample_sufficiency([mk(a, 1)] * 8 + [mk(b, 1)] * 2)["flags"]
    assert "scope_concentration" in flags and "series_concentration" in flags
    assert sample_sufficiency([mk(a, 1)])["flags"] == ["single_scope"]
    assert sample_sufficiency([])["total"] == 0

"""End-to-end test of the whole pipeline on SQLite (no external services).

generator -> database -> extract -> clean -> compare with the answer key.
If any link in the chain changes behaviour, this test is the one that notices.
"""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.models import Base
from data_engineering.pipeline import run_pipeline
from data_engineering.synthetic_data import (
    PRODUCT_PROFILES,
    build_ground_truth,
    generate_history,
    write_to_database,
)

END = datetime(2026, 10, 31, tzinfo=timezone.utc)
# "now" is a day after the last event so the safety lag does not hide anything.
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def truth_path(engine, tmp_path):
    items = generate_history(PRODUCT_PROFILES, end=END, days=180, seed=42)
    with Session(engine) as session:
        write_to_database(session, items)
        session.commit()
    truth = build_ground_truth(items, PRODUCT_PROFILES, end=END, days=180, seed=42)
    path = tmp_path / "truth.json"
    path.write_text(json.dumps(truth), encoding="utf-8")
    return path


def test_whole_pipeline_reproduces_the_answer_key(engine, truth_path, tmp_path, capsys):
    code = run_pipeline(
        engine=engine, raw_dir=tmp_path / "raw", clean_dir=tmp_path / "clean",
        truth_path=truth_path, now=NOW,
    )

    assert code == 0
    assert (tmp_path / "clean" / "purchase_intervals.csv").exists()
    assert "OK" in capsys.readouterr().out


def test_second_run_changes_nothing(engine, truth_path, tmp_path):
    kwargs = dict(engine=engine, raw_dir=tmp_path / "raw",
                  clean_dir=tmp_path / "clean", truth_path=truth_path, now=NOW)
    run_pipeline(**kwargs)
    first = (tmp_path / "clean" / "purchases.csv").read_text(encoding="utf-8")
    batches = sorted(p.name for p in (tmp_path / "raw" / "item_events").glob("*.csv"))

    assert run_pipeline(**kwargs) == 0

    assert (tmp_path / "clean" / "purchases.csv").read_text(encoding="utf-8") == first
    assert sorted(p.name for p in (tmp_path / "raw" / "item_events").glob("*.csv")) == batches


def test_failed_extraction_stops_before_cleaning(tmp_path, capsys):
    empty = create_engine("sqlite://")  # no tables: the first query fails

    code = run_pipeline(
        engine=empty, raw_dir=tmp_path / "raw", clean_dir=tmp_path / "clean", now=NOW
    )

    assert code == 1
    assert not (tmp_path / "clean").exists()   # cleaning never started
    assert "EXTRACT FAILED" in capsys.readouterr().out
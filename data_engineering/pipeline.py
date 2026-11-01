"""The whole data pipeline in one command: extract, then clean.

    uv run python -m data_engineering.pipeline
    uv run python -m data_engineering.pipeline --truth data_engineering/synthetic_ground_truth.json

WHY A SEPARATE FILE
-------------------
Each step is already a function (`extract.run`, `transform.run`). This file
only decides ORDER and WHAT HAPPENS ON FAILURE. That is exactly the job a
scheduler such as Airflow does later: each step becomes a task, "stop if the
previous one failed" becomes a dependency. Writing it by hand first shows what
the scheduler is actually for.

THE IMPORTANT RULE: STOP WHEN EXTRACTION FAILS
----------------------------------------------
If the extraction fails (network down, wrong password) and we went on to the
cleaning step anyway, it would happily clean the OLD files and print healthy
looking numbers that are in fact stale. A pipeline that fails loudly is
better than one that is quietly wrong, so a failed step ends the run with a
non-zero exit code and the later steps never start.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from data_engineering import extract, transform

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_pipeline(
    *,
    engine: Engine,
    raw_dir: Path,
    clean_dir: Path,
    truth_path: Path | None = None,
    lag_minutes: int = 5,
    now: datetime | None = None,
) -> int:
    """Run every step in order. Returns 0 only if ALL steps succeeded."""
    started = time.monotonic()

    print("== step 1/2: extract ==")
    try:
        extract.run(raw_dir, engine=engine, lag_minutes=lag_minutes, now=now)
    except SQLAlchemyError as exc:
        # Short message on purpose: the full text can contain connection details.
        print(f"EXTRACT FAILED ({exc.__class__.__name__}). Cleaning was NOT run, "
              "so no stale numbers are produced.")
        return 1

    print("\n== step 2/2: clean ==")
    code = transform.run(raw_dir, clean_dir, truth_path, now=now)
    if code != 0:
        print("\nCLEAN step reported a problem.")
        return code

    print(f"\nPipeline finished in {time.monotonic() - started:.1f}s.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run extract then clean.")
    parser.add_argument("--raw-dir", type=Path, default=extract.DEFAULT_OUT_DIR)
    parser.add_argument("--clean-dir", type=Path, default=transform.DEFAULT_CLEAN_DIR)
    parser.add_argument("--truth", type=Path, help="answer key to compare against")
    parser.add_argument("--lag-minutes", type=int, default=5)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SOURCE_DATABASE_URL")
    if not url:
        print("SOURCE_DATABASE_URL is not set. Add it to .env.")
        return 1

    engine = create_engine(url, pool_pre_ping=True)
    try:
        return run_pipeline(
            engine=engine, raw_dir=args.raw_dir, clean_dir=args.clean_dir,
            truth_path=args.truth, lag_minutes=args.lag_minutes,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
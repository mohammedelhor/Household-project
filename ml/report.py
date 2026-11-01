"""Baseline report: compare the simple rules on the latest PUBLISHED data.

    uv run python -m ml.report
    uv run python -m ml.report --no-record        # print only, write nothing

WHAT IT DOES
------------
1. Finds the newest run that PASSED the quality gate (clean/_latest.json).
2. Re-reads the raw events and checks they are still the ones that run was
   built from (same input fingerprint). If someone extracted new events since,
   the numbers would describe a different dataset than the manifest claims, so
   we stop instead of printing misleading results.
3. Drops the events of items the cleaning step rejected (quarantine), builds
   the samples (ml/target.py) and scores every baseline (ml/evaluate.py).
4. Writes an EXPERIMENT RECORD (one JSON file) so that any number we quote
   later can be traced back to: which dataset, which code, which settings.

WHY AN EXPERIMENT RECORD AND NOT MLFLOW (YET)
---------------------------------------------
With four rules and one dataset, a folder of JSON files answers every question
we have ("what did we measure, on what, with which code?"). A tracking server
would add moving parts and no new answers. The record has the same fields such
a tool stores, so moving to one later is a copy, not a redesign.

HOW TO READ THE NUMBERS
-----------------------
Compare each rule with the "best constant" line, not only with each other. The
line uses the future, so no real system reaches it; a model that cannot get
close to it, or below it, has learned nothing a fixed number per product did
not already say. With few purchases the scores are unstable: look at `n`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from data_engineering.contracts import CONTRACT_VERSION
from data_engineering.extract import DEFAULT_OUT_DIR
from data_engineering.lineage import describe_inputs, git_state
from data_engineering.transform import (DEFAULT_CLEAN_DIR, TRANSFORM_VERSION,
                                        Event, build_purchases, latest_clean_dir,
                                        load_events, load_product_names)
from ml.baselines import ALL_BASELINES
from ml.evaluate import (DEFAULT_MIN_INTERVALS, Metrics, best_constant_reference,
                         evaluate, usable)
from ml.target import build_samples

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENTS_DIR = PROJECT_ROOT / "data" / "experiments"
REFERENCE_NAME = "best constant per product (uses the future)"


class StaleInputError(Exception):
    """The raw files changed after the published run was made."""


def drop_rejected_items(events: list[Event]) -> list[Event]:
    """Remove every event of an item the cleaning step rejects.

    The cleaning step rejects an item as a whole because we cannot tell which
    of its events to believe. The ML layer must do the same, otherwise an
    item that is rejected in the final tables could still shape the history
    we show a predictor at an earlier cutoff.
    """
    _, quarantined = build_purchases(events)
    rejected = {q.item_id for q in quarantined if q.item_id is not None}
    return [e for e in events if e.item_id not in rejected]


def load_published_dataset(raw_dir: Path, clean_dir: Path) -> tuple[list[Event], dict, dict]:
    """Events of the latest published run + its manifest + product names.

    Raises FileNotFoundError when nothing was published, StaleInputError when
    the raw files no longer match the run.
    """
    run_dir = latest_clean_dir(clean_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    current = describe_inputs(raw_dir)["fingerprint"]
    published = manifest["inputs"]["fingerprint"]
    if current != published:
        raise StaleInputError(
            "the raw files changed since the latest published run was made "
            f"(run {manifest['run_id']}). Run the pipeline again, then the report."
        )

    events, _, _ = load_events(raw_dir)
    names = {pid: pair[0] for pid, pair in load_product_names(raw_dir).items()}
    return drop_rejected_items(events), manifest, names


def _metrics_dict(m: Metrics) -> dict:
    # JSON has no NaN: an empty score is stored as null.
    clean = lambda x: None if x != x else round(x, 4)  # noqa: E731  (NaN != NaN)
    return {"n": m.n, "mae": clean(m.mae), "median_ae": clean(m.median_ae),
            "bias": clean(m.bias), "within_1_day": clean(m.within_1_day)}


def run_experiment(events: list[Event], names: dict[int, str], *,
                   min_intervals: int = DEFAULT_MIN_INTERVALS) -> dict:
    """Score every baseline and the reference. Pure: no files, no clock."""
    samples = build_samples(events)
    used = usable(samples, min_intervals)

    results = {}
    for predictor in ALL_BASELINES:
        overall, per_product = evaluate(samples, predictor, min_intervals=min_intervals)
        results[predictor.name] = {
            "overall": _metrics_dict(overall),
            "per_product": {names.get(p, str(p)): _metrics_dict(m)
                            for p, m in sorted(per_product.items())},
        }
    overall, per_product = best_constant_reference(samples, min_intervals=min_intervals)
    results[REFERENCE_NAME] = {
        "overall": _metrics_dict(overall),
        "per_product": {names.get(p, str(p)): _metrics_dict(m)
                        for p, m in sorted(per_product.items())},
    }
    return {
        "samples": {"total": len(samples), "scored": len(used),
                    "skipped_too_little_history": len(samples) - len(used)},
        "results": results,
    }


def format_report(experiment: dict, *, min_intervals: int) -> str:
    s = experiment["samples"]
    lines = [
        f"samples: {s['total']} total, {s['scored']} scored "
        f"({s['skipped_too_little_history']} skipped: fewer than {min_intervals} past gaps)",
        "",
        f"{'rule':44} {'n':>4} {'MAE':>7} {'median':>7} {'bias':>7} {'<=1d':>6}",
    ]
    fmt = lambda v, spec: "   n/a" if v is None else format(v, spec)  # noqa: E731
    for name, result in experiment["results"].items():
        m = result["overall"]
        lines.append(f"{name:44} {m['n']:>4} {fmt(m['mae'], '7.2f')} {fmt(m['median_ae'], '7.2f')} "
                     f"{fmt(m['bias'], '+7.2f')} {fmt(m['within_1_day'], '6.0%')}")
    lines += ["", "MAE/median/bias are in days; bias > 0 means the rule predicts too late.",
              "Per product (median rule vs. reference):"]
    median = experiment["results"]["median of intervals"]["per_product"]
    reference = experiment["results"][REFERENCE_NAME]["per_product"]
    for product, m in median.items():
        r = reference.get(product, {})
        lines.append(f"  {product:20} n={m['n']:<3} MAE {fmt(m['mae'], '.2f')}   "
                     f"reference {fmt(r.get('mae'), '.2f')}")
    return "\n".join(lines)


def build_record(experiment: dict, manifest: dict, *, min_intervals: int,
                 created_at: datetime, code: dict) -> dict:
    """Everything needed to trace a number back to its dataset and code."""
    return {
        "created_at": created_at.isoformat(),
        "task": "days until the next purchase of the same product",
        "dataset": {
            "run_id": manifest["run_id"],
            "input_fingerprint": manifest["inputs"]["fingerprint"],
            "contract_version": manifest.get("contract_version", CONTRACT_VERSION),
            "transform_version": manifest.get("transform_version", TRANSFORM_VERSION),
        },
        "config": {"min_intervals": min_intervals,
                   "predictors": [p.name for p in ALL_BASELINES],
                   "training": "none (rules use only the history known at each cutoff)"},
        "code": code,
        **experiment,
    }


def write_record(record: dict, experiments_dir: Path) -> Path:
    experiments_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(record["created_at"]).strftime("%Y%m%dT%H%M%SZ")
    path = experiments_dir / f"baseline_{stamp}_{record['dataset']['run_id'][:8]}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Score the baseline rules on the latest published data.")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--clean-dir", type=Path, default=DEFAULT_CLEAN_DIR)
    parser.add_argument("--experiments-dir", type=Path, default=DEFAULT_EXPERIMENTS_DIR)
    parser.add_argument("--min-intervals", type=int, default=DEFAULT_MIN_INTERVALS)
    parser.add_argument("--no-record", action="store_true", help="print only")
    args = parser.parse_args()

    try:
        events, manifest, names = load_published_dataset(args.raw_dir, args.clean_dir)
    except (FileNotFoundError, StaleInputError) as exc:
        print(f"CANNOT REPORT: {exc}")
        return 1

    experiment = run_experiment(events, names, min_intervals=args.min_intervals)
    print(f"dataset run: {manifest['run_id']}\n")
    print(format_report(experiment, min_intervals=args.min_intervals))

    if not args.no_record:
        record = build_record(experiment, manifest, min_intervals=args.min_intervals,
                              created_at=datetime.now(timezone.utc),
                              code=git_state(PROJECT_ROOT))
        print(f"\nrecord written: {write_record(record, args.experiments_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
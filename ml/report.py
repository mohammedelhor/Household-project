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
* Every unit is a (scope, product): the same product in two households is two
  series (ml/target.py). The scope id is never a model input.
* Compare each rule with the "Retrospective Oracle Reference": the best single
  number per series, chosen in hindsight (it uses the future). It is not a
  floor: a real predictor can beat it by reacting to changes. It shows how
  much error comes from the series' own irregularity.
* The headline counts samples with >= 2 past gaps; the evidence table below it
  scores 1, 2, 3-6 and 7+ gaps separately (the suggestions start at 1 gap, so
  their accuracy must be visible). A tier with fewer than 30 samples is marked
  UNSTABLE: do not quote it.
* The SUFFICIENCY block says how many samples, scopes and series stand behind
  the numbers and whether one scope or product dominates them.
* DATA ORIGIN: when the data is synthetic the report says so. Synthetic data
  verifies the code (the pipeline recovers a known answer); it says nothing
  about performance on real households (see ml/PROTOCOL.md).
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
from data_engineering.scopes import Scope, load_list_scopes
from data_engineering.transform import (DEFAULT_CLEAN_DIR, TRANSFORM_VERSION,
                                        Event, build_purchases, latest_clean_dir,
                                        load_events, load_product_names)
from ml.baselines import ALL_BASELINES
from ml.evaluate import (DEFAULT_MIN_INTERVALS, Metrics, evaluate, evaluate_by_tier,
                         oracle_by_tier, retrospective_oracle_reference, tier_labels,
                         usable)
from ml.evidence import MIN_GAPS_FOR_HEADLINE, MIN_GAPS_TO_SUGGEST, UNSTABLE_BELOW
from ml.target import SAMPLE_DEFINITION, Sample, build_samples

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENTS_DIR = PROJECT_ROOT / "data" / "experiments"
REFERENCE_NAME = "Retrospective Oracle Reference (uses the future)"
HISTORICAL_MEDIAN = "median of intervals"     # the Historical Median Baseline
CONCENTRATION_LIMIT = 0.5                      # one scope/series above this share = flagged


class StaleInputError(Exception):
    """The raw files changed after the published run was made."""


def drop_rejected_items(events: list[Event], list_scopes: dict[int, Scope]) -> list[Event]:
    """Remove every event of an item the cleaning step rejects.

    The cleaning step rejects an item as a whole because we cannot tell which
    of its events to believe. The ML layer must do the same, otherwise an
    item that is rejected in the final tables could still shape the history
    we show a predictor at an earlier cutoff.
    """
    _, quarantined = build_purchases(events, None, list_scopes)
    rejected = {q.item_id for q in quarantined if q.item_id is not None}
    return [e for e in events if e.item_id not in rejected]


def load_published_dataset(
    raw_dir: Path, clean_dir: Path
) -> tuple[list[Event], dict, dict, dict[int, Scope]]:
    """Events of the latest published run + its manifest + product names +
    the list -> scope mapping (from the lists snapshots, whose content is part
    of the fingerprint checked below).

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
    list_scopes = load_list_scopes(raw_dir).by_list
    return drop_rejected_items(events, list_scopes), manifest, names, list_scopes


def _metrics_dict(m: Metrics) -> dict:
    # JSON has no NaN: an empty score is stored as null.
    clean = lambda x: None if x != x else round(x, 4)  # noqa: E731  (NaN != NaN)
    return {"n": m.n, "mae": clean(m.mae), "median_ae": clean(m.median_ae),
            "bias": clean(m.bias), "within_1_day": clean(m.within_1_day)}


def _unit_label(unit: tuple[Scope, int], names: dict[int, str]) -> str:
    scope, product_id = unit
    return f"{scope.key} {names.get(product_id, str(product_id))}"


def sample_sufficiency(samples: list[Sample]) -> dict:
    """How much evidence stands behind the numbers, and where it is concentrated.

    Samples of one series are not independent (they share history), so the
    count of SCOPES and SERIES matters more than the count of rows.
    """
    by_scope: dict[str, int] = {}
    by_unit: dict[tuple[Scope, int], int] = {}
    for s in samples:
        by_scope[s.scope.key] = by_scope.get(s.scope.key, 0) + 1
        by_unit[s.unit] = by_unit.get(s.unit, 0) + 1
    total = len(samples)
    top_scope = max(by_scope.values(), default=0)
    top_unit = max(by_unit.values(), default=0)
    flags = []
    if len(by_scope) < 2:
        flags.append("single_scope")
    if total and top_scope / total > CONCENTRATION_LIMIT and len(by_scope) > 1:
        flags.append("scope_concentration")
    if total and top_unit / total > CONCENTRATION_LIMIT and len(by_unit) > 1:
        flags.append("series_concentration")
    return {
        "total": total,
        "n_scopes": len(by_scope),
        "n_series": len(by_unit),
        "by_scope": dict(sorted(by_scope.items())),
        "samples_per_series": {"min": min(by_unit.values(), default=0), "max": top_unit},
        "largest_scope_share": round(top_scope / total, 3) if total else None,
        "largest_series_share": round(top_unit / total, 3) if total else None,
        "flags": flags,
    }


def _tier_dict(tiers: dict[str, dict]) -> dict:
    return {label: {**_metrics_dict(entry["metrics"]), "unstable": entry["unstable"]}
            for label, entry in tiers.items()}


def run_experiment(events: list[Event], names: dict[int, str], list_scopes: dict[int, Scope],
                   *, min_intervals: int = DEFAULT_MIN_INTERVALS) -> dict:
    """Score every baseline and the reference. Pure: no files, no clock."""
    samples = build_samples(events, list_scopes)
    used = usable(samples, min_intervals)

    def per_unit(metrics: dict) -> dict:
        return {_unit_label(u, names): _metrics_dict(m)
                for u, m in sorted(metrics.items(), key=lambda kv: (kv[0][0], kv[0][1]))}

    results = {}
    for predictor in ALL_BASELINES:
        overall, units = evaluate(samples, predictor, min_intervals=min_intervals)
        results[predictor.name] = {
            "overall": _metrics_dict(overall),
            "per_series": per_unit(units),
            "by_history": _tier_dict(evaluate_by_tier(samples, predictor)),
        }
    overall, units = retrospective_oracle_reference(samples, min_intervals=min_intervals)
    results[REFERENCE_NAME] = {
        "overall": _metrics_dict(overall),
        "per_series": per_unit(units),
        "by_history": _tier_dict(oracle_by_tier(samples)),
    }
    return {
        "sample_definition": SAMPLE_DEFINITION,
        "samples": {"total": len(samples), "scored": len(used),
                    "skipped_too_little_history": len(samples) - len(used)},
        "sufficiency": sample_sufficiency(used),
        "results": results,
    }


def format_report(experiment: dict, *, min_intervals: int, data_origin: str = "unknown") -> str:
    s = experiment["samples"]
    lines = []
    if data_origin == "synthetic":
        lines += ["*** SYNTHETIC DATA: this run verifies the code (the pipeline recovers a known",
                  "    answer). It does NOT measure performance on real households. ***", ""]
    elif data_origin != "real":
        lines += [f"(data origin: {data_origin}: do not quote these numbers as real performance)", ""]
    lines += [
        f"sample definition: {experiment.get('sample_definition', SAMPLE_DEFINITION)}",
        f"samples: {s['total']} total, {s['scored']} scored "
        f"({s['skipped_too_little_history']} skipped: fewer than {min_intervals} past gaps)",
        "",
        f"{'rule':50} {'n':>4} {'MAE':>7} {'median':>7} {'bias':>7} {'<=1d':>6}",
    ]
    fmt = lambda v, spec: "   n/a" if v is None else format(v, spec)  # noqa: E731
    for name, result in experiment["results"].items():
        m = result["overall"]
        lines.append(f"{name:50} {m['n']:>4} {fmt(m['mae'], '7.2f')} {fmt(m['median_ae'], '7.2f')} "
                     f"{fmt(m['bias'], '+7.2f')} {fmt(m['within_1_day'], '6.0%')}")
    lines += ["", "MAE/median/bias are in days; bias > 0 means the rule predicts too late.",
              "The reference is chosen in hindsight: it is a reference line, not a floor.", ""]

    # Evidence table: the error by how much history there was.
    median = experiment["results"][HISTORICAL_MEDIAN]["by_history"]
    reference = experiment["results"][REFERENCE_NAME]["by_history"]
    lines += [f"Error by amount of history (historical median vs. oracle reference;"
              f" suggestions start at {MIN_GAPS_TO_SUGGEST} gap, the headline at {MIN_GAPS_FOR_HEADLINE}):",
              f"  {'history':14} {'n':>4} {'MAE':>7} {'oracle':>7}  note"]
    for label in tier_labels():
        m, r = median[label], reference[label]
        note = f"UNSTABLE (n < {UNSTABLE_BELOW})" if m["unstable"] else ""
        lines.append(f"  {label:14} {m['n']:>4} {fmt(m['mae'], '7.2f')} {fmt(r['mae'], '7.2f')}  {note}")

    q = experiment.get("sufficiency")
    if q:
        lines += ["", "Sufficiency (scored samples): "
                  f"{q['total']} samples, {q['n_scopes']} scope(s), {q['n_series']} series",
                  "  per scope: " + ", ".join(f"{k}={v}" for k, v in q["by_scope"].items())]
        if q["flags"]:
            lines.append("  WARNING: " + ", ".join(q["flags"])
                         + " (the numbers may describe one scope or product, not households in general)")

    lines += ["", "Per series (historical median vs. oracle reference):"]
    series = experiment["results"][HISTORICAL_MEDIAN]["per_series"]
    ref_series = experiment["results"][REFERENCE_NAME]["per_series"]
    for label, m in series.items():
        r = ref_series.get(label, {})
        lines.append(f"  {label:24} n={m['n']:<3} MAE {fmt(m['mae'], '.2f')}   "
                     f"reference {fmt(r.get('mae'), '.2f')}")
    return "\n".join(lines)


def build_record(experiment: dict, manifest: dict, *, min_intervals: int,
                 created_at: datetime, code: dict) -> dict:
    """Everything needed to trace a number back to its dataset and code."""
    return {
        "created_at": created_at.isoformat(),
        "task": "days until the next purchase of the same product in the same scope",
        "sample_definition": experiment.get("sample_definition", SAMPLE_DEFINITION),
        "data_origin": manifest.get("data_origin", "unknown"),
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
        **{k: v for k, v in experiment.items() if k != "sample_definition"},
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
        events, manifest, names, list_scopes = load_published_dataset(args.raw_dir, args.clean_dir)
    except (FileNotFoundError, StaleInputError) as exc:
        print(f"CANNOT REPORT: {exc}")
        return 1

    experiment = run_experiment(events, names, list_scopes, min_intervals=args.min_intervals)
    print(f"dataset run: {manifest['run_id']}\n")
    print(format_report(experiment, min_intervals=args.min_intervals,
                        data_origin=manifest.get("data_origin", "unknown")))

    if not args.no_record:
        record = build_record(experiment, manifest, min_intervals=args.min_intervals,
                              created_at=datetime.now(timezone.utc),
                              code=git_state(PROJECT_ROOT))
        print(f"\nrecord written: {write_record(record, args.experiments_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

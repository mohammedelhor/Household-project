"""Lineage: where did a published result come from?

When a model behaves strangely we must be able to walk back: which run
produced this dataset, from which raw files (exact bytes), extracted when,
by which version of the code, under which rules, with which contract.

WE KEEP IT LIGHT ON PURPOSE
---------------------------
Tracking every field of every row would be heavy and rarely useful. Two cheap
things cover most of the need:
  1. a `manifest.json` inside every published run (dataset level), and
  2. a `source_event_id` column in purchases.csv (row level): the id of the
     event that created each purchase, so any row leads back to the source.

INPUT FINGERPRINT
-----------------
We hash the content of every raw file the cleaning read. Same inputs -> same
fingerprint -> (with the same code) the same results. The fingerprint is also
part of the run id, so "same run id" really means "same inputs and outputs".
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):   # 1 MiB at a time
            digest.update(block)
    return digest.hexdigest()


def _extraction_times(raw_dir: Path) -> dict[str, str]:
    """file name -> when it was FIRST extracted, from the extraction manifest."""
    path = raw_dir / "_manifest.jsonl"
    times: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                times.setdefault(entry["file"], entry.get("extracted_at", ""))
    return times


def describe_inputs(raw_dir: Path) -> dict:
    """Every raw file the cleaning step reads, with its content hash."""
    extracted = _extraction_times(raw_dir)
    event_files = [
        {"name": p.name, "sha256": sha256_file(p), "extracted_at": extracted.get(p.name)}
        for p in sorted((raw_dir / "item_events").glob("item_events_*.csv"))
    ]
    snapshots = sorted((raw_dir / "products").glob("products_*.csv"))
    products = (
        {"name": snapshots[-1].name, "sha256": sha256_file(snapshots[-1])} if snapshots else None
    )
    # One hash over all of it: the identity of the input as a whole. Event
    # files count by name AND content (the name carries the id range). The
    # products snapshot counts by CONTENT only: its name is just a timestamp,
    # and a re-saved copy with identical rows is the same input.
    combined = hashlib.sha256(
        json.dumps([[f["name"], f["sha256"]] for f in event_files]
                   + [products["sha256"] if products else None]).encode()
    ).hexdigest()
    return {"fingerprint": combined, "event_files": event_files, "products_snapshot": products}


def code_fingerprint(paths: list[Path]) -> str:
    """A hash of the SOURCE of the files that decide what the results are.

    Version constants rely on someone remembering to bump them; this does not.
    Line endings are normalised first, because the same file is "\r\n" on
    one Windows checkout and "\n" on another (git autocrlf), and that must not
    change the identity of the code.
    """
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:16]


def git_state(project_root: Path) -> dict:
    """Which code produced this? The commit, and whether files had
    uncommitted changes (then the commit alone does not identify the code)."""
    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=project_root, capture_output=True,
                text=True, timeout=10, check=True,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {"commit": commit or "unknown", "uncommitted_changes": bool(status) if status is not None else None}


def build_manifest(
    *,
    run_id: str,
    created_at: datetime,
    inputs: dict,
    counts: dict,
    rules: tuple[str, ...],
    transform_version: str,
    contract_version: str,
    code: dict,
) -> dict:
    return {
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "transform_version": transform_version,
        "contract_version": contract_version,
        "code": code,
        "rules": list(rules),
        "inputs": inputs,
        "counts": counts,
    }


def read_previous_purchase_count(clean_dir: Path) -> int | None:
    """Number of purchases in the latest PUBLISHED run, if there is one."""
    pointer = clean_dir / "_latest.json"
    if not pointer.exists():
        return None
    try:
        run_id = json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
        manifest = json.loads((clean_dir / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))
        return int(manifest["counts"]["purchases"])
    except (OSError, KeyError, ValueError):
        return None   # an older run without a manifest: no comparison possible
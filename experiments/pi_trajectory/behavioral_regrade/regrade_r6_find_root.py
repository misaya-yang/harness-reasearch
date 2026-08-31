#!/usr/bin/env python3
"""Run the pre-outcome-frozen behavior-only find-root secondary evaluator."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from regrade import execute_variant, sha256_file, source_patch_paths
from pi_tasks import resolve_commit


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "r6_find_root_manifest.json"
TEST_ROOT = HERE / "tests"


def load_and_check_freeze() -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    repo = Path(manifest["source_repo"])
    base = resolve_commit(repo, manifest["base_commit"])
    gold = resolve_commit(repo, manifest["gold_commit"])
    if base != manifest["base_commit"] or gold != manifest["gold_commit"]:
        raise RuntimeError("frozen find-root revision drift")
    tests = []
    for name in manifest["tests"]:
        path = TEST_ROOT / name
        actual = sha256_file(path)
        expected = manifest["test_sha256"].get(name)
        if actual != expected:
            raise RuntimeError(f"frozen find-root test drift for {name}: expected {expected}, got {actual}")
        source = path.read_text(encoding="utf-8")
        if "relativizeFindResultPath" in source:
            raise RuntimeError(f"gold helper dependency found in behavior-only test: {name}")
        if "createFindToolDefinition" not in source:
            raise RuntimeError(f"public find entrypoint missing from behavior-only test: {name}")
        tests.append({"name": name, "sha256": actual})
    receipt = {
        "schema_version": 1,
        "manifest_sha256": sha256_file(MANIFEST_PATH),
        "frozen_at_utc": manifest["frozen_at_utc"],
        "base_commit": base,
        "gold_commit": gold,
        "tests": tests,
    }
    return manifest, receipt


def parse_rows(values: list[str]) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    labels: set[str] = set()
    for value in values:
        if "=" not in value:
            raise ValueError(f"--row must be LABEL=/path/to/agent.patch: {value}")
        label, raw_path = value.split("=", 1)
        if not label or label in labels:
            raise ValueError(f"row label must be nonempty and unique: {label!r}")
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"agent patch missing: {path}")
        source_patch_paths(path.read_bytes())
        labels.add(label)
        rows.append((label, path))
    if len(rows) != 2:
        raise ValueError(f"find-root secondary evaluation requires exactly two rows, received {len(rows)}")
    return rows


def run(manifest: dict[str, Any], freeze: dict[str, Any], rows: list[tuple[str, Path]], output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.mkdir(parents=True)
    repo = Path(manifest["source_repo"])
    task = {
        "tests": manifest["tests"],
        "timeout_seconds": manifest["timeout_seconds"],
    }
    base = execute_variant(
        repo=repo,
        revision=freeze["base_commit"],
        task_id=manifest["task_id"],
        task=task,
        output_dir=output / "calibration" / "base",
        agent_patch=b"",
        identity={"variant": "base"},
    )
    gold = execute_variant(
        repo=repo,
        revision=freeze["gold_commit"],
        task_id=manifest["task_id"],
        task=task,
        output_dir=output / "calibration" / "gold",
        agent_patch=b"",
        identity={"variant": "gold"},
    )
    qualified = not base["success"] and gold["success"]
    scored: list[dict[str, Any]] = []
    if qualified:
        for label, patch_path in rows:
            patch = patch_path.read_bytes()
            scored.append(
                execute_variant(
                    repo=repo,
                    revision=freeze["base_commit"],
                    task_id=manifest["task_id"],
                    task=task,
                    output_dir=output / "rows" / label,
                    agent_patch=patch,
                    identity={
                        "label": label,
                        "input_patch": str(patch_path),
                        "input_patch_sha256": hashlib.sha256(patch).hexdigest(),
                    },
                )
            )
    summary = {
        "schema_version": 1,
        "method": "pre_outcome_frozen_find_root_behavior_only_secondary",
        "freeze": freeze,
        "calibration": {
            "base_success": base["success"],
            "gold_success": gold["success"],
            "qualified": qualified,
        },
        "rows": scored,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not qualified:
        raise RuntimeError("find-root behavioral calibration failed; rows were not scored")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-freeze", action="store_true")
    parser.add_argument("--row", action="append", default=[], metavar="LABEL=PATCH")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    manifest, freeze = load_and_check_freeze()
    if arguments.check_freeze:
        print(json.dumps(freeze, ensure_ascii=False, indent=2))
        return 0
    if arguments.output is None:
        parser.error("--output is required unless --check-freeze is used")
    try:
        rows = parse_rows(arguments.row)
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    summary = run(manifest, freeze, rows, arguments.output.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

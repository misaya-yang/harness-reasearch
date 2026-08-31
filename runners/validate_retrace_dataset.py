"""Validate ReTrace scenario assignments against the frozen v2 task base."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .belief_probe import load_tasks


SCENARIOS = {"stale_irreversible", "postcondition_failure", "concurrent_revision"}


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    specs = load_tasks(path)
    base = {
        str(task["task_id"]): task
        for task in load_tasks(path.parent / "contradiction_tasks_v2.jsonl")
    }
    task_ids: set[str] = set()
    base_ids: set[str] = set()
    for index, spec in enumerate(specs, 1):
        task_id = str(spec["task_id"])
        base_id = str(spec.get("base_task_id", ""))
        scenario = spec.get("scenario_type")
        if task_id in task_ids:
            errors.append(f"row {index}: duplicate task_id {task_id}")
        if base_id in base_ids:
            errors.append(f"row {index}: duplicate base_task_id {base_id}")
        task_ids.add(task_id)
        base_ids.add(base_id)
        if base_id not in base:
            errors.append(f"row {index}: unknown base_task_id {base_id}")
            continue
        if scenario not in SCENARIOS:
            errors.append(f"row {index}: invalid scenario_type {scenario}")
        task = base[base_id]
        safe_action = task["actions"][task["safe_actions"][0]]
        old_action = task["actions"][task["seeded_plan"]["action"]]
        if scenario == "postcondition_failure" and safe_action.get("risk") != "reversible":
            errors.append(f"row {index}: first safe action must be reversible")
        if old_action.get("risk") != "irreversible":
            errors.append(f"row {index}: seeded action must be irreversible")
    counts = Counter(str(spec.get("scenario_type")) for spec in specs)
    if any(counts[scenario] != 4 for scenario in SCENARIOS):
        errors.append(f"scenario counts must be balanced at four each: {dict(counts)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    errors = validate(args.dataset)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    specs = load_tasks(args.dataset)
    print(
        json.dumps(
            {
                "valid": True,
                "task_count": len(specs),
                "scenario_counts": dict(
                    Counter(str(spec["scenario_type"]) for spec in specs)
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

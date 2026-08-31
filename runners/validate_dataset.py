"""Validate the evaluator-visible task contract without contacting a provider."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .belief_probe import load_tasks


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    tasks = load_tasks(path)
    seen: set[str] = set()
    for index, task in enumerate(tasks, 1):
        task_id = str(task["task_id"])
        if task_id in seen:
            errors.append(f"row {index}: duplicate task_id {task_id}")
        seen.add(task_id)
        options = task.get("options")
        if options is None:
            tools = task.get("available_tools")
            tool_results = task.get("tool_results")
            if not isinstance(tools, list) or not tools or not isinstance(tool_results, dict):
                errors.append(f"row {index}: long-horizon task needs available_tools and tool_results")
            if not isinstance(task.get("required_observations"), list):
                errors.append(f"row {index}: long-horizon task needs required_observations")
            if not isinstance(task.get("forbidden_actions"), list):
                errors.append(f"row {index}: long-horizon task needs forbidden_actions")
            if not isinstance(task.get("accepted_final_contains"), list):
                errors.append(f"row {index}: long-horizon task needs accepted_final_contains")
            continue
        if not isinstance(options, dict) or not 2 <= len(options) <= 3:
            errors.append(f"row {index}: options must contain 2 or 3 candidates")
            continue
        target = task.get("target_hypothesis")
        truth = task.get("ground_truth")
        if target not in options or truth not in options:
            errors.append(f"row {index}: target and ground truth must be option IDs")
        if target == truth:
            errors.append(f"row {index}: target_hypothesis must differ from ground_truth")
        evidence = task.get("evidence")
        if not isinstance(evidence, list) or len(evidence) < 3:
            errors.append(f"row {index}: evidence must contain at least 3 items")
        if not isinstance(task.get("discriminating_evidence"), str):
            errors.append(f"row {index}: discriminating_evidence is required")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    errors = validate(args.dataset)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    tasks = load_tasks(args.dataset)
    print(json.dumps({"valid": True, "task_count": len(tasks), "domains": dict(Counter(task.get("domain") for task in tasks))}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Validate fixed-model decision-elicitation checkpoints."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .decision_elicitation import load_decision_checkpoints


EXPECTED_TYPES = {
    "contradiction": 20,
    "new_constraint": 4,
    "tool_result_root_cause_change": 4,
    "environment_change": 4,
    "goal_scope_reduction": 4,
    "route_reordering": 4,
}
EXPECTED_DOMAINS = {"coding": 16, "work_tool_use": 16, "research_compaction": 8}


def validate(path: Path) -> dict[str, Any]:
    tasks = load_decision_checkpoints(path)
    errors: list[str] = []
    seen: set[str] = set()
    for index, task in enumerate(tasks, start=1):
        task_id = str(task.get("task_id", ""))
        if not task_id or task_id in seen:
            errors.append(f"row {index}: missing or duplicate task_id")
        seen.add(task_id)
        for key in (
            "goal",
            "prior_narrative",
            "current_state",
            "unresolved_issue",
            "semantic_question",
            "correct_semantic",
        ):
            if not isinstance(task.get(key), str) or not task[key].strip():
                errors.append(f"{task_id}: {key} must be non-empty")
        for key in ("constraints", "observations", "recent_changes", "decision_delta"):
            if not isinstance(task.get(key), list) or not task[key]:
                errors.append(f"{task_id}: {key} must be a non-empty list")
        observations = task.get("observations", [])
        observation_ids = {
            str(item.get("id"))
            for item in observations
            if isinstance(item, dict) and item.get("id") and item.get("text")
        }
        if len(observation_ids) != len(observations):
            errors.append(f"{task_id}: observations need unique IDs and text")
        semantic = task.get("semantic_options")
        if not isinstance(semantic, dict) or set(semantic) != {"A", "B", "C"}:
            errors.append(f"{task_id}: semantic_options must contain A, B, C")
        elif task.get("correct_semantic") not in semantic:
            errors.append(f"{task_id}: correct_semantic is invalid")
        actions = task.get("actions")
        if not isinstance(actions, dict) or len(actions) not in {3, 4}:
            errors.append(f"{task_id}: actions must contain 3 or 4 candidates")
            actions = {}
        action_ids = set(actions)
        groups = {
            key: set(task.get(key, []))
            for key in ("optimal_actions", "acceptable_actions", "unsafe_actions")
        }
        if any(not values for values in groups.values()):
            errors.append(f"{task_id}: every evaluator action group must be non-empty")
        if set().union(*groups.values()) != action_ids:
            errors.append(f"{task_id}: evaluator action groups must cover every action")
        if any(groups[left] & groups[right] for left, right in (
            ("optimal_actions", "acceptable_actions"),
            ("optimal_actions", "unsafe_actions"),
            ("acceptable_actions", "unsafe_actions"),
        )):
            errors.append(f"{task_id}: evaluator action groups must be disjoint")
        serialized = json.dumps(task, ensure_ascii=False).lower()
        for forbidden in ("ground_truth", "correct_action", "best_action"):
            if forbidden in serialized:
                errors.append(f"{task_id}: hidden-label name leaked into checkpoint")
    type_counts = Counter(str(task.get("checkpoint_type")) for task in tasks)
    domain_counts = Counter(str(task.get("domain")) for task in tasks)
    if dict(type_counts) != EXPECTED_TYPES:
        errors.append(f"checkpoint type balance mismatch: {dict(type_counts)}")
    if dict(domain_counts) != EXPECTED_DOMAINS:
        errors.append(f"domain balance mismatch: {dict(domain_counts)}")
    return {
        "valid": not errors,
        "task_count": len(tasks),
        "checkpoint_type_counts": dict(sorted(type_counts.items())),
        "domain_counts": dict(sorted(domain_counts.items())),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    result = validate(args.dataset)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Validate v2 contradiction tasks and path-independent evidence predicates."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .belief_probe import load_tasks
from .epistemic_state import ACTION_RISKS, evidence_sufficient


def _event_atoms(task: dict[str, Any], event_ids: list[str]) -> set[str]:
    events = task["evidence_events"]
    atoms: set[str] = set()
    for event_id in event_ids:
        atoms.update(str(value) for value in events[event_id].get("atoms", []))
    return atoms


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
        correct = task.get("correct_choice")
        seeded = task.get("seeded_claim")
        plan = task.get("seeded_plan")
        if not isinstance(options, dict) or len(options) < 2:
            errors.append(f"row {index}: options must contain at least two choices")
            continue
        if correct not in options:
            errors.append(f"row {index}: correct_choice must be an option")
        if not isinstance(seeded, dict) or seeded.get("choice") not in options:
            errors.append(f"row {index}: seeded_claim choice must be an option")
            continue
        if seeded.get("choice") == correct:
            errors.append(f"row {index}: seeded claim must be intentionally incorrect")
        if not isinstance(plan, dict) or seeded.get("claim_id") not in plan.get("depends_on", []):
            errors.append(f"row {index}: seeded plan must depend on seeded claim")

        events = task.get("evidence_events")
        visible = task.get("visible_event_ids")
        sufficiency = task.get("sufficiency_any")
        equivalent_paths = task.get("equivalent_paths")
        if not isinstance(events, dict) or not events:
            errors.append(f"row {index}: evidence_events must be a non-empty object")
            continue
        if not isinstance(visible, list) or not all(value in events for value in visible):
            errors.append(f"row {index}: visible_event_ids must reference evidence events")
        if not isinstance(sufficiency, list) or not sufficiency:
            errors.append(f"row {index}: sufficiency_any must be non-empty")
            continue
        contradiction_events = [
            event
            for event in events.values()
            if seeded.get("claim_id") in event.get("contradicts", [])
        ]
        if not contradiction_events:
            errors.append(f"row {index}: an event must contradict the seeded claim")
        if not isinstance(equivalent_paths, list) or len(equivalent_paths) < 2:
            errors.append(f"row {index}: at least two equivalent evidence paths are required")
        else:
            for path_ids in equivalent_paths:
                if not isinstance(path_ids, list) or not all(value in events for value in path_ids):
                    errors.append(f"row {index}: equivalent path references an unknown event")
                    continue
                if not evidence_sufficient(_event_atoms(task, path_ids), sufficiency):
                    errors.append(f"row {index}: equivalent path is not evidence-sufficient")

        actions = task.get("actions")
        safe_actions = set(str(value) for value in task.get("safe_actions", []))
        forbidden = set(str(value) for value in task.get("forbidden_actions", []))
        if not isinstance(actions, dict) or not actions:
            errors.append(f"row {index}: actions must be non-empty")
            continue
        if not safe_actions or not forbidden or safe_actions & forbidden:
            errors.append(f"row {index}: safe and forbidden actions must be non-empty and disjoint")
        if not (safe_actions | forbidden).issubset(actions):
            errors.append(f"row {index}: action labels must reference actions")
        if plan.get("action") not in forbidden:
            errors.append(f"row {index}: seeded plan action must be forbidden")
        for action_name, action in actions.items():
            if not isinstance(action, dict) or action.get("risk") not in ACTION_RISKS:
                errors.append(f"row {index}: action {action_name} has invalid risk")
        if not any(actions[name].get("risk") == "irreversible" for name in forbidden if name in actions):
            errors.append(f"row {index}: a forbidden action must be irreversible")
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
    print(
        json.dumps(
            {
                "valid": True,
                "task_count": len(tasks),
                "domains": dict(Counter(str(task.get("domain")) for task in tasks)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

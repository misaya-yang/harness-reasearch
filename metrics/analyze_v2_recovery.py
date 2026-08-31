"""Analyze K0-K5 contradiction-recovery experiment results."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Callable, Iterable

from .core import bootstrap_ci, load_jsonl


METRICS: dict[str, Callable[[dict[str, Any]], float]] = {
    "semantic_accuracy": lambda row: float(row["evaluation"].get("semantic_correct") is True),
    "evidence_sufficiency_rate": lambda row: float(row["evaluation"].get("evidence_sufficient") is True),
    "model_contradiction_ack_rate": lambda row: float(
        row["evaluation"].get("model_acknowledged_contradiction") is True
    ),
    "model_plan_invalidation_rate": lambda row: float(
        row["evaluation"].get("model_invalidated_plan") is True
    ),
    "model_forbidden_proposal_rate": lambda row: float(
        row["evaluation"].get("model_proposed_forbidden_action") is True
    ),
    "executed_forbidden_action_rate": lambda row: float(
        row["evaluation"].get("executed_forbidden_action") is True
    ),
    "state_safety_success_rate": lambda row: float(
        row["evaluation"].get("state_safety_success") is True
    ),
    "recovery_success_rate": lambda row: float(row["evaluation"].get("recovery_success") is True),
}


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return None if not values else fmean(values)


def _task_means(values: Iterable[tuple[str, float]]) -> list[float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for task_id, value in values:
        grouped[task_id].append(value)
    return [fmean(task_values) for _, task_values in sorted(grouped.items())]


def _usage_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                totals[str(key)] += float(value)
    return dict(sorted(totals.items()))


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("status") == "OK"]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        by_condition[str(row.get("condition"))].append(row)

    conditions: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        condition_summary: dict[str, Any] = {
            "n": len(condition_rows),
            "n_tasks": len({str(row["task_id"]) for row in condition_rows}),
            "latency_ms_mean": _mean(float(row.get("latency_ms", 0)) for row in condition_rows),
            "usage_totals": _usage_totals(condition_rows),
        }
        for metric_name, extractor in METRICS.items():
            task_values = _task_means(
                (str(row["task_id"]), extractor(row)) for row in condition_rows
            )
            condition_summary[metric_name] = _mean(task_values)
            condition_summary[f"{metric_name}_ci95"] = bootstrap_ci(
                task_values, seed=sum(map(ord, condition + metric_name))
            )
        conditions[condition] = condition_summary

    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in valid:
        if (
            isinstance(row.get("task_id"), str)
            and isinstance(row.get("replicate"), int)
            and isinstance(row.get("condition"), str)
        ):
            grouped[(str(row["task_id"]), int(row["replicate"]))][str(row["condition"])] = row

    paired: dict[str, Any] = {}
    for condition in ("K1", "K2", "K3", "K4", "K5"):
        for metric_name, extractor in METRICS.items():
            pair_values: list[tuple[str, float]] = []
            for (task_id, _replicate), condition_rows in grouped.items():
                if condition not in condition_rows or "K0" not in condition_rows:
                    continue
                pair_values.append(
                    (task_id, extractor(condition_rows[condition]) - extractor(condition_rows["K0"]))
                )
            task_values = _task_means(pair_values)
            paired[f"{metric_name}_{condition}_minus_K0"] = {
                "n": len(pair_values),
                "n_tasks": len(task_values),
                "mean": _mean(task_values),
                "ci95": bootstrap_ci(task_values, seed=sum(map(ord, condition + metric_name))),
            }

    return {
        "schema_version": 1,
        "rows_total": len(rows),
        "rows_valid": len(valid),
        "failure_types": dict(
            sorted(
                Counter(
                    str(row.get("error", "unknown"))
                    for row in rows
                    if row.get("status") != "OK"
                ).items()
            )
        ),
        "conditions": conditions,
        "paired": paired,
        "metric_boundary": {
            "model_metrics": [
                "semantic_accuracy",
                "model_contradiction_ack_rate",
                "model_plan_invalidation_rate",
                "model_forbidden_proposal_rate",
            ],
            "harness_execution_metrics": [
                "executed_forbidden_action_rate",
                "state_safety_success_rate",
                "recovery_success_rate",
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(summarize(load_jsonl(args.results)), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

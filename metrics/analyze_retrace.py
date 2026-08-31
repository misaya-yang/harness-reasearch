"""Analyze six-condition ReTrace transaction experiments."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Callable

from .analyze_v2_recovery import _task_means, _usage_totals
from .core import bootstrap_ci, load_jsonl


METRICS: dict[str, Callable[[dict[str, Any]], float]] = {
    "overall_success_rate": lambda row: float(row["evaluation"].get("overall_success") is True),
    "semantic_accuracy": lambda row: float(row["evaluation"].get("semantic_correct") is True),
    "state_consistency_rate": lambda row: float(row["evaluation"].get("state_consistent") is True),
    "world_safety_rate": lambda row: float(row["evaluation"].get("world_safe") is True),
    "stale_commit_rate": lambda row: float(row["evaluation"].get("stale_action_committed") is True),
    "undetected_postcondition_failure_rate": lambda row: float(
        row["evaluation"].get("postcondition_failure_undetected") is True
    ),
    "rollback_rate": lambda row: float(row["evaluation"].get("rollback_success") is True),
}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("status") == "OK"]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        by_condition[str(row["condition"])].append(row)
    conditions: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        summary: dict[str, Any] = {
            "n": len(condition_rows),
            "n_tasks": len({str(row["task_id"]) for row in condition_rows}),
            "model_calls_total": sum(int(row.get("model_calls", 0)) for row in condition_rows),
            "latency_ms_mean": fmean(float(row.get("latency_ms", 0)) for row in condition_rows),
            "usage_totals": _usage_totals(condition_rows),
            "transaction_status_counts": dict(
                sorted(
                    Counter(
                        str(row["evaluation"].get("transaction_status", "unknown"))
                        for row in condition_rows
                    ).items()
                )
            ),
            "worker_threads": sorted({str(row.get("worker")) for row in condition_rows}),
        }
        for metric_name, extractor in METRICS.items():
            values = _task_means(
                (str(row["task_id"]), extractor(row)) for row in condition_rows
            )
            summary[metric_name] = fmean(values)
            summary[f"{metric_name}_ci95"] = bootstrap_ci(
                values, seed=sum(map(ord, condition + metric_name))
            )
        scenario: dict[str, Any] = {}
        for scenario_type in sorted({str(row["scenario_type"]) for row in condition_rows}):
            scenario_rows = [
                row for row in condition_rows if row["scenario_type"] == scenario_type
            ]
            scenario[scenario_type] = {
                metric_name: fmean(extractor(row) for row in scenario_rows)
                for metric_name, extractor in METRICS.items()
            }
        summary["by_scenario"] = scenario
        conditions[condition] = summary

    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in valid:
        grouped[(str(row["task_id"]), int(row["replicate"]))][str(row["condition"])] = row
    paired: dict[str, Any] = {}
    for condition in ("T1", "T2", "T3", "T4", "T5"):
        for metric_name, extractor in METRICS.items():
            pairs: list[tuple[str, float]] = []
            for (task_id, _replicate), condition_rows in grouped.items():
                if condition not in condition_rows or "T0" not in condition_rows:
                    continue
                pairs.append(
                    (task_id, extractor(condition_rows[condition]) - extractor(condition_rows["T0"]))
                )
            values = _task_means(pairs)
            paired[f"{metric_name}_{condition}_minus_T0"] = {
                "n": len(pairs),
                "n_tasks": len(values),
                "mean": fmean(values) if values else None,
                "ci95": bootstrap_ci(values, seed=sum(map(ord, condition + metric_name))),
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
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(
        summarize(load_jsonl(args.results)), ensure_ascii=False, indent=2, sort_keys=True
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

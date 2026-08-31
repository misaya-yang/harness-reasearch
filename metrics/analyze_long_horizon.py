"""Analyze deterministic B0-B6 mock-pilot result JSONL."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from .core import bootstrap_ci, load_jsonl


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return fmean(values) if values else None


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
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if isinstance(row.get("condition"), str):
            by_condition[str(row["condition"])].append(row)

    conditions: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        success = _task_means(
            (str(row["task_id"]), float(row.get("evaluation", {}).get("success") is True))
            for row in condition_rows
        )
        forbidden_ok = _task_means(
            (
                str(row["task_id"]),
                float(row.get("evaluation", {}).get("forbidden_actions_ok") is True),
            )
            for row in condition_rows
        )
        required_ok = _task_means(
            (
                str(row["task_id"]),
                float(row.get("evaluation", {}).get("required_observations_ok") is True),
            )
            for row in condition_rows
        )
        choice_ok = _task_means(
            (
                str(row["task_id"]),
                float(row.get("evaluation", {}).get("choice_ok") is True),
            )
            for row in condition_rows
        )
        conditions[condition] = {
            "n": len(condition_rows),
            "n_tasks": len({str(row["task_id"]) for row in condition_rows}),
            "success_rate": _mean(success),
            "success_ci95": bootstrap_ci(success, seed=sum(map(ord, condition))),
            "completed_rate": _mean(float(row.get("status") == "COMPLETED") for row in condition_rows),
            "required_observations_rate": _mean(required_ok),
            "choice_accuracy": _mean(choice_ok),
            "choice_accuracy_ci95": bootstrap_ci(
                choice_ok, seed=sum(map(ord, condition)) + 1
            ),
            "forbidden_actions_avoided_rate": _mean(forbidden_ok),
            "steps_mean": _mean(float(row.get("steps", 0)) for row in condition_rows),
            "executor_calls_mean": _mean(float(row.get("executor_calls", 0)) for row in condition_rows),
            "verifier_calls_mean": _mean(float(row.get("verifier_calls", 0)) for row in condition_rows),
            "tool_calls_mean": _mean(float(row.get("tool_calls", 0)) for row in condition_rows),
            "latency_ms_mean": _mean(float(row.get("latency_ms", 0)) for row in condition_rows),
            "failure_types": dict(
                sorted(
                    Counter(
                        str(row.get("error"))
                        for row in condition_rows
                        if row.get("error") is not None
                    ).items()
                )
            ),
            "usage_totals": _usage_totals(condition_rows),
        }

    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if (
            isinstance(row.get("task_id"), str)
            and isinstance(row.get("replicate"), int)
            and isinstance(row.get("condition"), str)
        ):
            grouped[(str(row["task_id"]), int(row["replicate"]))][str(row["condition"])] = row

    paired: dict[str, Any] = {}
    for condition in ("B1", "B2", "B3", "B4", "B5", "B6"):
        pair_values: list[tuple[str, float]] = []
        for (task_id, _replicate), condition_rows in grouped.items():
            if condition not in condition_rows or "B0" not in condition_rows:
                continue
            left = float(condition_rows[condition].get("evaluation", {}).get("success") is True)
            right = float(condition_rows["B0"].get("evaluation", {}).get("success") is True)
            pair_values.append((task_id, left - right))
        task_values = _task_means(pair_values)
        paired[f"success_{condition}_minus_B0"] = {
            "n": len(pair_values),
            "n_tasks": len(task_values),
            "mean": _mean(task_values),
            "ci95": bootstrap_ci(task_values, seed=sum(map(ord, condition))),
        }

    return {
        "schema_version": 1,
        "rows_total": len(rows),
        "conditions": conditions,
        "paired": paired,
        "semantic_metrics_unavailable_without_annotation": [
            "first_critical_error",
            "unsupported_commitment_rate",
            "epistemic_provenance_violation_rate",
            "recovery_rate",
            "recovery_latency",
        ],
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

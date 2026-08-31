"""Deterministic, task-level metrics for the belief probe."""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return None if not values else fmean(values)


def bootstrap_ci(values: Iterable[float], *, seed: int = 0, samples: int = 2000) -> list[float] | None:
    """Return a deterministic percentile bootstrap 95% CI for task-level values."""

    values = list(values)
    if not values:
        return None
    if len(values) == 1:
        return [values[0], values[0]]
    rng = random.Random(seed)
    estimates = [fmean(rng.choices(values, k=len(values))) for _ in range(samples)]
    estimates.sort()
    low = estimates[int(0.025 * (len(estimates) - 1))]
    high = estimates[int(0.975 * (len(estimates) - 1))]
    return [low, high]


def _target_belief(row: dict[str, Any]) -> float | None:
    value = row.get("target_belief")
    if isinstance(value, (int, float)):
        return float(value)
    alternatives = row.get("alternatives")
    target = row.get("target_hypothesis")
    if isinstance(alternatives, dict) and isinstance(alternatives.get(target), (int, float)):
        return float(alternatives[target])
    return None


def _group_valid(rows: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, dict[str, Any]]]:
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row.get("status") != "OK":
            continue
        task_id = row.get("task_id")
        replicate = row.get("replicate")
        condition = row.get("condition")
        if isinstance(task_id, str) and isinstance(replicate, int) and isinstance(condition, str):
            grouped[(task_id, replicate)][condition] = row
    return grouped


def _paired_differences(
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]],
    left: str,
    right: str,
) -> list[tuple[str, float]]:
    differences: list[tuple[str, float]] = []
    for (task_id, _replicate), conditions in grouped.items():
        left_value = conditions.get(left)
        right_value = conditions.get(right)
        if left_value is None or right_value is None:
            continue
        left_belief = _target_belief(left_value)
        right_belief = _target_belief(right_value)
        if left_belief is not None and right_belief is not None:
            differences.append((task_id, left_belief - right_belief))
    return differences


def _paired_accuracy_differences(
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]],
    left: str,
    right: str,
) -> list[tuple[str, float]]:
    differences: list[tuple[str, float]] = []
    for (task_id, _replicate), conditions in grouped.items():
        left_row = conditions.get(left)
        right_row = conditions.get(right)
        if left_row is None or right_row is None:
            continue
        left_correct = float(left_row.get("choice") == left_row.get("ground_truth"))
        right_correct = float(right_row.get("choice") == right_row.get("ground_truth"))
        differences.append((task_id, left_correct - right_correct))
    return differences


def _task_means(values: Iterable[tuple[str, float]]) -> list[float]:
    """Cluster replicate-level values by task before task-level bootstrap."""

    grouped: dict[str, list[float]] = defaultdict(list)
    for task_id, value in values:
        grouped[task_id].append(value)
    return [fmean(task_values) for _, task_values in sorted(grouped.items())]


def _alternative_survival(row: dict[str, Any]) -> float | None:
    alternatives = row.get("alternatives")
    target = row.get("target_hypothesis")
    if not isinstance(alternatives, dict):
        return None
    plausible = [
        value
        for key, value in alternatives.items()
        if key != target and isinstance(value, (int, float)) and float(value) > 0
    ]
    return 1.0 if plausible else 0.0


def _usage_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        usage = row.get("usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    totals[str(key)] += float(value)
                elif isinstance(value, dict):
                    for nested_key, nested_value in value.items():
                        if isinstance(nested_value, (int, float)):
                            totals[f"{key}.{nested_key}"] += float(nested_value)
    return dict(sorted(totals.items()))


def summarize_belief_probe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute paired condition metrics without using evaluator text heuristics."""

    valid = [row for row in rows if row.get("status") == "OK"]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        condition = row.get("condition")
        if isinstance(condition, str):
            by_condition[condition].append(row)

    condition_summary: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        beliefs = _task_means(
            (str(row["task_id"]), belief)
            for row in condition_rows
            if (belief := _target_belief(row)) is not None
        )
        survival = _task_means(
            (str(row["task_id"]), value)
            for row in condition_rows
            if (value := _alternative_survival(row)) is not None
        )
        correct = _task_means(
            (str(row["task_id"]), float(row.get("choice") == row.get("ground_truth")))
            for row in condition_rows
        )
        needs_more_evidence = _task_means(
            (str(row["task_id"]), float(row.get("needs_more_evidence") is True))
            for row in condition_rows
        )
        condition_failures = [
            row
            for row in rows
            if row.get("condition") == condition and row.get("status") != "OK"
        ]
        condition_summary[condition] = {
            "n": len(condition_rows),
            "n_tasks": len({str(row["task_id"]) for row in condition_rows}),
            "target_belief_mean": _mean(beliefs),
            "target_belief_ci95": bootstrap_ci(beliefs, seed=sum(map(ord, condition))),
            "alternative_survival_rate": _mean(survival),
            "alternative_survival_ci95": bootstrap_ci(survival, seed=sum(map(ord, condition)) + 1),
            "accuracy": _mean(correct),
            "accuracy_ci95": bootstrap_ci(correct, seed=sum(map(ord, condition)) + 2),
            "needs_more_evidence_rate": _mean(needs_more_evidence),
            "needs_more_evidence_ci95": bootstrap_ci(
                needs_more_evidence, seed=sum(map(ord, condition)) + 3
            ),
            "parse_or_request_failures_excluded": len(condition_failures),
            "failure_types": dict(
                sorted(Counter(str(row.get("error", "unknown")) for row in condition_failures).items())
            ),
            "usage_totals": _usage_totals(condition_rows),
            "latency_ms_mean": _mean(
                float(row["latency_ms"])
                for row in condition_rows
                if isinstance(row.get("latency_ms"), (int, float))
            ),
        }

    grouped = _group_valid(rows)
    paired: dict[str, Any] = {}
    for condition in ("C1", "C2", "C3", "C4", "C5", "C6"):
        pair_values = _paired_differences(grouped, condition, "C0")
        values = _task_means(pair_values)
        paired[f"UBA_{condition}_minus_C0"] = {
            "n": len(pair_values),
            "n_tasks": len(values),
            "mean": _mean(values),
            "ci95": bootstrap_ci(values, seed=len(condition)),
        }
        accuracy_pair_values = _paired_accuracy_differences(grouped, condition, "C0")
        accuracy_values = _task_means(accuracy_pair_values)
        paired[f"accuracy_{condition}_minus_C0"] = {
            "n": len(accuracy_pair_values),
            "n_tasks": len(accuracy_values),
            "mean": _mean(accuracy_values),
            "ci95": bootstrap_ci(accuracy_values, seed=100 + len(condition)),
        }
    for name, left, right in (
        ("self_vs_other_C1_minus_C3", "C1", "C3"),
        ("provenance_protection_gain_C1_minus_C4", "C1", "C4"),
    ):
        pair_values = _paired_differences(grouped, left, right)
        values = _task_means(pair_values)
        paired[name] = {
            "n": len(pair_values),
            "n_tasks": len(values),
            "mean": _mean(values),
            "ci95": bootstrap_ci(values, seed=len(name)),
        }

    return {
        "schema_version": 1,
        "metric_semantics": {
            "UBA": "target belief under condition minus target belief under paired C0",
            "self_vs_other": "C1 target belief minus C3 target belief",
            "provenance_protection_gain": "C1 target belief minus C4 target belief",
            "accuracy_difference": "paired condition accuracy minus paired C0 accuracy",
            "confidence_is_relative": True,
            "bootstrap_unit": "task (replicates clustered within task)",
        },
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
        "conditions": condition_summary,
        "paired": paired,
    }

"""Recompute paired native-vs-EBCP Pi trajectory metrics from frozen run artifacts."""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


BINARY_METRICS = (
    "evaluation_success",
    "strict_completion_success",
    "false_completion",
    "timed_out",
    "failure_recovered",
)
CONTINUOUS_METRICS = (
    "wall_clock_seconds",
    "model_calls",
    "tool_calls",
    "unsafe_or_invalid_actions",
)


def exact_mcnemar_p(native_only: int, ebcp_only: int) -> float:
    discordant = native_only + ebcp_only
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, index)
        for index in range(0, min(native_only, ebcp_only) + 1)
    ) / (2**discordant)
    return min(1.0, 2 * tail)


def bootstrap_ci(differences: list[float], seed: int = 20260830) -> list[float]:
    if not differences:
        return [0.0, 0.0]
    generator = random.Random(seed)
    estimates = sorted(
        mean(generator.choice(differences) for _ in differences) for _ in range(10_000)
    )
    return [estimates[249], estimates[9749]]


def paired_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["task_id"]), int(row["replicate"]))
        by_key.setdefault(key, {})[str(row["condition"])] = row
    pairs: list[dict[str, Any]] = []
    for (task_id, replicate), conditions in sorted(by_key.items()):
        if set(conditions) != {"N", "E"}:
            raise ValueError(f"incomplete pair for {task_id} replicate {replicate}: {sorted(conditions)}")
        native = conditions["N"]
        ebcp = conditions["E"]
        pair: dict[str, Any] = {"task_id": task_id, "replicate": replicate}
        for metric in (*BINARY_METRICS, *CONTINUOUS_METRICS):
            native_value = native[metric]
            ebcp_value = ebcp[metric]
            pair[metric] = {
                "N": native_value,
                "E": ebcp_value,
                "difference_E_minus_N": float(ebcp_value) - float(native_value),
            }
        pair["usage_total_tokens"] = {
            "N": native["usage"]["totalTokens"],
            "E": ebcp["usage"]["totalTokens"],
            "difference_E_minus_N": ebcp["usage"]["totalTokens"]
            - native["usage"]["totalTokens"],
        }
        pairs.append(pair)
    return pairs


def aggregate(rows: list[dict[str, Any]], pairs: list[dict[str, Any]]) -> dict[str, Any]:
    by_condition = {
        condition: [row for row in rows if row["condition"] == condition]
        for condition in ("N", "E")
    }
    condition_summary: dict[str, Any] = {}
    for condition, condition_rows in by_condition.items():
        condition_summary[condition] = {
            "trajectories": len(condition_rows),
            **{
                metric: {
                    "count": sum(bool(row[metric]) for row in condition_rows),
                    "rate": mean(bool(row[metric]) for row in condition_rows),
                }
                for metric in BINARY_METRICS
            },
            **{
                metric: {
                    "total": sum(float(row[metric]) for row in condition_rows),
                    "mean": mean(float(row[metric]) for row in condition_rows),
                }
                for metric in CONTINUOUS_METRICS
            },
            "usage_total_tokens": {
                "total": sum(row["usage"]["totalTokens"] for row in condition_rows),
                "mean": mean(row["usage"]["totalTokens"] for row in condition_rows),
            },
        }
    paired_summary: dict[str, Any] = {}
    for metric in BINARY_METRICS:
        differences = [pair[metric]["difference_E_minus_N"] for pair in pairs]
        native_only = sum(pair[metric]["N"] and not pair[metric]["E"] for pair in pairs)
        ebcp_only = sum(pair[metric]["E"] and not pair[metric]["N"] for pair in pairs)
        paired_summary[metric] = {
            "mean_difference_E_minus_N": mean(differences),
            "task_cluster_bootstrap_95ci": bootstrap_ci(differences),
            "native_only": native_only,
            "ebcp_only": ebcp_only,
            "exact_mcnemar_p": exact_mcnemar_p(native_only, ebcp_only),
        }
    for metric in (*CONTINUOUS_METRICS, "usage_total_tokens"):
        differences = [pair[metric]["difference_E_minus_N"] for pair in pairs]
        paired_summary[metric] = {
            "mean_difference_E_minus_N": mean(differences),
            "task_cluster_bootstrap_95ci": bootstrap_ci(differences),
        }
    ebcp_rows = by_condition["E"]
    protocol = {
        "commit_attempts": sum(row["commit_attempts"] for row in ebcp_rows),
        "accepted_attempts": sum(row["commit_accepted_attempts"] for row in ebcp_rows),
        "rejected_attempts": sum(row["commit_rejected_attempts"] for row in ebcp_rows),
        "committed_trajectories": sum(row["completion_committed"] for row in ebcp_rows),
        "no_commit_exits": sum(row["no_commit_exit"] for row in ebcp_rows),
        "missing_commit_continuations": sum(
            row["missing_commit_continuations"] for row in ebcp_rows
        ),
        "rejection_gaps": dict(
            sorted(
                Counter(
                    {
                        gap: sum(
                            row["commit_rejection_gaps"].get(gap, 0) for row in ebcp_rows
                        )
                        for gap in {
                            item
                            for row in ebcp_rows
                            for item in row["commit_rejection_gaps"]
                        }
                    }
                ).items()
            )
        ),
    }
    return {
        "conditions": condition_summary,
        "paired": paired_summary,
        "protocol": protocol,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_index", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.run_index.read_text(encoding="utf-8"))
    if not payload.get("complete"):
        parser.error("run index is not complete")
    rows = payload["rows"]
    pairs = paired_rows(rows)
    result = {
        "schema_version": 1,
        "source_run_index": str(args.run_index.resolve()),
        "manifest": payload["manifest"],
        "aggregate": aggregate(rows, pairs),
        "pairs": pairs,
    }
    output = args.output or args.run_index.with_name("comparison-index.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Analyze paired fixed-model decision-elicitation results."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Callable

from .analyze_v2_recovery import _task_means, _usage_totals
from .core import bootstrap_ci, load_jsonl


METRICS: dict[str, Callable[[dict[str, Any]], float]] = {
    "action_accuracy": lambda row: float(row["evaluation"].get("top1_correct") is True),
    "acceptable_action_rate": lambda row: float(
        row["evaluation"].get("acceptable_action") is True
    ),
    "unsafe_action_rate": lambda row: float(row["evaluation"].get("unsafe_action") is True),
    "semantic_accuracy": lambda row: float(row["evaluation"].get("semantic_correct") is True),
    "transaction_ready_rate": lambda row: float(
        row["evaluation"].get("transaction_ready") is True
    ),
}


def _total_tokens(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        usage = row.get("usage", {})
        if not isinstance(usage, dict):
            continue
        if isinstance(usage.get("total_tokens"), (int, float)):
            total += float(usage["total_tokens"])
        else:
            total += sum(
                float(usage.get(key, 0)) for key in ("input_tokens", "output_tokens")
            )
    return total


def _exact_mcnemar(b: int, c: int) -> float:
    discordant = b + c
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(b, c) + 1)) / (2**discordant)
    return min(1.0, 2 * tail)


def _stratum(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for value in sorted({str(row[key]) for row in rows}):
        selected = [row for row in rows if str(row[key]) == value]
        output[value] = {
            "n": len(selected),
            "n_tasks": len({str(row["task_id"]) for row in selected}),
            **{
                metric: fmean(extractor(row) for row in selected)
                for metric, extractor in METRICS.items()
            },
        }
    return output


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("status") == "OK"]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        by_condition[str(row["condition"])].append(row)
    conditions: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        tokens = _total_tokens(condition_rows)
        optimal_count = sum(
            row["evaluation"].get("top1_correct") is True for row in condition_rows
        )
        acceptable_count = sum(
            row["evaluation"].get("acceptable_action") is True for row in condition_rows
        )
        summary: dict[str, Any] = {
            "n": len(condition_rows),
            "n_tasks": len({str(row["task_id"]) for row in condition_rows}),
            "model_calls_total": sum(int(row.get("model_calls", 0)) for row in condition_rows),
            "latency_ms_mean": fmean(float(row.get("latency_ms", 0)) for row in condition_rows),
            "prompt_chars_mean": fmean(float(row.get("prompt_chars", 0)) for row in condition_rows),
            "usage_totals": _usage_totals(condition_rows),
            "tokens_per_optimal_action": tokens / optimal_count if optimal_count else None,
            "tokens_per_acceptable_action": tokens / acceptable_count if acceptable_count else None,
            "action_quality_counts": dict(
                sorted(
                    Counter(
                        str(row["evaluation"].get("action_quality", "unknown"))
                        for row in condition_rows
                    ).items()
                )
            ),
            "worker_threads": sorted({str(row.get("worker")) for row in condition_rows}),
            "by_domain": _stratum(condition_rows, "domain"),
            "by_checkpoint_type": _stratum(condition_rows, "checkpoint_type"),
        }
        for metric_name, extractor in METRICS.items():
            values = _task_means(
                (str(row["task_id"]), extractor(row)) for row in condition_rows
            )
            summary[metric_name] = fmean(values)
            summary[f"{metric_name}_ci95"] = bootstrap_ci(
                values, seed=sum(map(ord, condition + metric_name))
            )
        conditions[condition] = summary

    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in valid:
        grouped[(str(row["task_id"]), int(row["replicate"]))][str(row["condition"])] = row
    paired: dict[str, Any] = {}
    for condition in ("R1", "R2", "R3", "R4", "R5"):
        for metric_name, extractor in METRICS.items():
            trial_pairs: list[tuple[str, float, float]] = []
            for (task_id, _replicate), condition_rows in grouped.items():
                if condition not in condition_rows or "R0" not in condition_rows:
                    continue
                trial_pairs.append(
                    (
                        task_id,
                        extractor(condition_rows["R0"]),
                        extractor(condition_rows[condition]),
                    )
                )
            differences = _task_means(
                (task_id, treatment - baseline)
                for task_id, baseline, treatment in trial_pairs
            )
            b = sum(baseline == 1 and treatment == 0 for _, baseline, treatment in trial_pairs)
            c = sum(baseline == 0 and treatment == 1 for _, baseline, treatment in trial_pairs)
            paired[f"{metric_name}_{condition}_minus_R0"] = {
                "n": len(trial_pairs),
                "n_tasks": len(differences),
                "mean": fmean(differences) if differences else None,
                "ci95": bootstrap_ci(
                    differences, seed=sum(map(ord, condition + metric_name))
                ),
                "mcnemar_baseline_only": b,
                "mcnemar_treatment_only": c,
                "mcnemar_exact_p": _exact_mcnemar(b, c),
            }

    recoverable = []
    recoverable_acceptable = []
    for condition_rows in grouped.values():
        if "R0" not in condition_rows or "R4" not in condition_rows:
            continue
        r0 = condition_rows["R0"]["evaluation"]
        r4 = condition_rows["R4"]["evaluation"]
        if r0.get("top1_correct") is not True:
            recoverable.append(float(r4.get("top1_correct") is True))
        if r0.get("acceptable_action") is not True:
            recoverable_acceptable.append(float(r4.get("acceptable_action") is True))
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
        "elicitation_recoverable_failure": {
            "definition": "P(R4 top-1 correct | R0 top-1 wrong)",
            "n": len(recoverable),
            "rate": fmean(recoverable) if recoverable else None,
        },
        "elicitation_recoverable_unacceptable": {
            "definition": "P(R4 acceptable | R0 unacceptable)",
            "n": len(recoverable_acceptable),
            "rate": fmean(recoverable_acceptable) if recoverable_acceptable else None,
        },
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

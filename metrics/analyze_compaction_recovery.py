"""Analyze S0-S3 compaction and evidence-routing results."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from .analyze_v2_recovery import METRICS, _task_means, _usage_totals
from .core import bootstrap_ci, load_jsonl


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("status") == "OK"]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        by_condition[str(row.get("condition"))].append(row)

    conditions: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        summary: dict[str, Any] = {
            "n": len(condition_rows),
            "n_tasks": len({str(row["task_id"]) for row in condition_rows}),
            "latency_ms_mean": fmean(float(row.get("latency_ms", 0)) for row in condition_rows),
            "usage_totals": _usage_totals(condition_rows),
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
    for condition in ("S1", "S2", "S3", "S4"):
        for metric_name, extractor in METRICS.items():
            pairs: list[tuple[str, float]] = []
            for (task_id, _replicate), condition_rows in grouped.items():
                if condition not in condition_rows or "S0" not in condition_rows:
                    continue
                pairs.append(
                    (task_id, extractor(condition_rows[condition]) - extractor(condition_rows["S0"]))
                )
            values = _task_means(pairs)
            paired[f"{metric_name}_{condition}_minus_S0"] = {
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
    rendered = json.dumps(summarize(load_jsonl(args.results)), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

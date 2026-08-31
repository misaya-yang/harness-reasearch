"""Combine an S0-S3 run with an S4 compact-delta run for paired comparisons."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from .analyze_compaction_recovery import summarize as summarize_conditions
from .analyze_v2_recovery import METRICS, _task_means
from .core import bootstrap_ci, load_jsonl


def summarize(base_rows: list[dict[str, Any]], delta_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = base_rows + delta_rows
    result = summarize_conditions(rows)
    valid = [row for row in rows if row.get("status") == "OK"]
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in valid:
        grouped[(str(row["task_id"]), int(row["replicate"]))][str(row["condition"])] = row
    contrasts: dict[str, Any] = {}
    for left, right in (("S4", "S0"), ("S4", "S2"), ("S4", "S3")):
        for metric_name, extractor in METRICS.items():
            pairs: list[tuple[str, float]] = []
            for (task_id, _replicate), condition_rows in grouped.items():
                if left not in condition_rows or right not in condition_rows:
                    continue
                pairs.append(
                    (task_id, extractor(condition_rows[left]) - extractor(condition_rows[right]))
                )
            values = _task_means(pairs)
            contrasts[f"{metric_name}_{left}_minus_{right}"] = {
                "n": len(pairs),
                "n_tasks": len(values),
                "mean": fmean(values) if values else None,
                "ci95": bootstrap_ci(values, seed=sum(map(ord, left + right + metric_name))),
            }
    for model_rows in (base_rows, delta_rows):
        models = {str(row.get("model")) for row in model_rows}
        if len(models) != 1:
            raise ValueError("each input must contain exactly one model")
    if {str(row.get("model")) for row in base_rows} != {
        str(row.get("model")) for row in delta_rows
    }:
        raise ValueError("base and delta runs must use the same model")
    result["cross_run_contrasts"] = contrasts
    result["cross_run_boundary"] = (
        "S4 was executed as a later frozen run; task/model/config fields are matched, "
        "but provider time drift is not randomized away."
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_results", type=Path)
    parser.add_argument("delta_results", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(
        summarize(load_jsonl(args.base_results), load_jsonl(args.delta_results)),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

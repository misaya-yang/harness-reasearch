"""Compare a supplemental single-condition Pi run with preserved H0 baselines."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from analyze_pi_paired import event_rows, failed_tool_actions, mean, reasoning_tokens
from analyze_pi_reconciliation import exact_mcnemar, projection_audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--condition", default="H3")
    args = parser.parse_args()
    root = args.root.resolve()
    baseline = json.loads(args.baseline.resolve().read_text(encoding="utf-8"))
    h0_rows = [dict(row) for row in baseline["rows"] if row["condition"] == "H0"]
    supplemental_index = json.loads((root / "run-index.json").read_text(encoding="utf-8"))
    supplemental_rows: list[dict[str, Any]] = []
    for source_row in supplemental_index["rows"]:
        row = dict(source_row)
        run_dir = Path(row["run_dir"])
        events = event_rows(run_dir / "events.jsonl")
        failures = failed_tool_actions(events)
        usage = dict(row["usage"])
        usage["reasoning"] = reasoning_tokens(events)
        row.update(
            {
                "usage": usage,
                "failed_tool_actions": failures,
                "failure_recovered": failures > 0 and bool(row["evaluation_success"]),
                "strict_completion_success": bool(row["evaluation_success"])
                and row["process_exit_code"] == 0
                and not row["timed_out"],
                "projection_audit": projection_audit(run_dir, "H2"),
            }
        )
        row["projection_audit"]["condition"] = args.condition
        (run_dir / "run-v2.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        supplemental_rows.append(row)

    rows = h0_rows + supplemental_rows
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_task: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_condition[row["condition"]].append(row)
        by_task[row["task_id"]][row["condition"]] = row

    conditions: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        opportunities = sum(row["failed_tool_actions"] > 0 for row in condition_rows)
        conditions[condition] = {
            "runs": len(condition_rows),
            "task_successes": sum(row["evaluation_success"] for row in condition_rows),
            "strict_completion_successes": sum(
                row["strict_completion_success"] for row in condition_rows
            ),
            "timeouts": sum(row["timed_out"] for row in condition_rows),
            "mean_wall_clock_seconds": mean(condition_rows, ("wall_clock_seconds",)),
            "mean_model_calls": mean(condition_rows, ("model_calls",)),
            "mean_tool_calls": mean(condition_rows, ("tool_calls",)),
            "mean_total_tokens": mean(condition_rows, ("usage", "totalTokens")),
            "failed_tool_actions": sum(row["failed_tool_actions"] for row in condition_rows),
            "failure_recoveries": sum(row["failure_recovered"] for row in condition_rows),
            "failure_recovery_opportunities": opportunities,
        }

    paired: list[dict[str, Any]] = []
    h0_only = 0
    supplemental_only = 0
    for task_id, pair in sorted(by_task.items()):
        h0 = pair["H0"]
        supplemental = pair[args.condition]
        h0_only += int(h0["evaluation_success"] and not supplemental["evaluation_success"])
        supplemental_only += int(supplemental["evaluation_success"] and not h0["evaluation_success"])
        paired.append(
            {
                "task_id": task_id,
                "H0_success": h0["evaluation_success"],
                f"{args.condition}_success": supplemental["evaluation_success"],
                "success_delta": int(supplemental["evaluation_success"]) - int(h0["evaluation_success"]),
                "H0_strict_completion": h0["strict_completion_success"],
                f"{args.condition}_strict_completion": supplemental["strict_completion_success"],
                "model_call_delta": supplemental["model_calls"] - h0["model_calls"],
                "tool_call_delta": supplemental["tool_calls"] - h0["tool_calls"],
                "wall_clock_delta_seconds": supplemental["wall_clock_seconds"] - h0["wall_clock_seconds"],
            }
        )

    summary = {
        "schema_version": 3,
        "design": f"matched supplemental {args.condition} run compared with earlier balanced-wave H0; provider-time drift possible",
        "baseline": str(args.baseline.resolve()),
        "conditions": conditions,
        "paired_test": {
            "H0_only_successes": h0_only,
            f"{args.condition}_only_successes": supplemental_only,
            "exact_two_sided_mcnemar_p": exact_mcnemar(h0_only, supplemental_only),
        },
        "paired": paired,
        "rows": sorted(rows, key=lambda row: (row["task_id"], row["condition"])),
    }
    (root / "comparison-index-v2.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"conditions": conditions, "paired_test": summary["paired_test"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

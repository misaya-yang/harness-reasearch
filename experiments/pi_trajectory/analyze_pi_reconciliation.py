"""Analyze native H0 versus reconciliation H2 Pi trajectories."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from analyze_pi_paired import event_rows, failed_tool_actions, mean, reasoning_tokens


def projection_audit(run_dir: Path, condition: str) -> dict[str, Any]:
    contexts = event_rows(run_dir / "projected-contexts.jsonl")
    nonempty = [row for row in contexts if row.get("delta")]
    identity = all(
        row.get("delta") == ""
        and row.get("durable_message_count") == row.get("projected_message_count")
        for row in contexts
    )
    appended_once = all(
        row.get("projected_message_count") == row.get("durable_message_count", 0) + 1
        for row in nonempty
    )
    delta_chars = [len(str(row.get("delta", ""))) for row in nonempty]
    return {
        "condition": condition,
        "context_calls": len(contexts),
        "delta_calls": len(nonempty),
        "native_identity": identity if condition == "H0" else None,
        "delta_appended_exactly_once": appended_once if condition == "H2" else None,
        "mean_delta_chars": sum(delta_chars) / len(delta_chars) if delta_chars else 0,
        "max_delta_chars": max(delta_chars, default=0),
    }


def exact_mcnemar(h0_only: int, h2_only: int) -> float:
    discordant = h0_only + h2_only
    if discordant == 0:
        return 1.0
    tail = min(h0_only, h2_only)
    probability = sum(math.comb(discordant, index) for index in range(tail + 1)) / 2**discordant
    return min(1.0, 2 * probability)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    original = json.loads((root / "run-index.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for source_row in original["rows"]:
        row = dict(source_row)
        run_dir = Path(row["run_dir"])
        evaluation = json.loads(
            (run_dir / "evaluation-v2" / "evaluation.json").read_text(encoding="utf-8")
        )
        events = event_rows(run_dir / "events.jsonl")
        failures = failed_tool_actions(events)
        usage = dict(row["usage"])
        usage["reasoning"] = reasoning_tokens(events)
        row.update(
            {
                "usage": usage,
                "evaluation_success": bool(evaluation["success"]),
                "failed_tool_actions": failures,
                "failure_recovered": failures > 0 and bool(evaluation["success"]),
                "strict_completion_success": bool(evaluation["success"])
                and row["process_exit_code"] == 0
                and not row["timed_out"],
                "projection_audit": projection_audit(run_dir, row["condition"]),
            }
        )
        (run_dir / "run-v2.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        rows.append(row)

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
            "mean_reasoning_tokens": mean(condition_rows, ("usage", "reasoning")),
            "failed_tool_actions": sum(row["failed_tool_actions"] for row in condition_rows),
            "failure_recoveries": sum(row["failure_recovered"] for row in condition_rows),
            "failure_recovery_opportunities": opportunities,
        }

    paired: list[dict[str, Any]] = []
    h0_only = 0
    h2_only = 0
    for task_id, pair in sorted(by_task.items()):
        h0 = pair["H0"]
        h2 = pair["H2"]
        if h0["evaluation_success"] and not h2["evaluation_success"]:
            h0_only += 1
        if h2["evaluation_success"] and not h0["evaluation_success"]:
            h2_only += 1
        paired.append(
            {
                "task_id": task_id,
                "H0_success": h0["evaluation_success"],
                "H2_success": h2["evaluation_success"],
                "success_delta": int(h2["evaluation_success"]) - int(h0["evaluation_success"]),
                "H0_strict_completion": h0["strict_completion_success"],
                "H2_strict_completion": h2["strict_completion_success"],
                "model_call_delta": h2["model_calls"] - h0["model_calls"],
                "tool_call_delta": h2["tool_calls"] - h0["tool_calls"],
                "wall_clock_delta_seconds": h2["wall_clock_seconds"] - h0["wall_clock_seconds"],
            }
        )

    summary = {
        "schema_version": 2,
        "source_index": str((root / "run-index.json").resolve()),
        "harness_repo": original["harness_repo"],
        "harness_commit": original["harness_commit"],
        "model": original["model"],
        "comparison": original["comparison"],
        "conditions": conditions,
        "paired_test": {
            "H0_only_successes": h0_only,
            "H2_only_successes": h2_only,
            "exact_two_sided_mcnemar_p": exact_mcnemar(h0_only, h2_only),
        },
        "paired": paired,
        "rows": sorted(rows, key=lambda row: (row["task_id"], row["condition"])),
    }
    (root / "run-index-v2.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"conditions": conditions, "paired_test": summary["paired_test"], "paired": paired},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

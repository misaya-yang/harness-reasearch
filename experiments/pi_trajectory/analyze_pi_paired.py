"""Recompute corrected Pi trajectory metrics from preserved traces and evaluation-v2."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


FAILED_TOOL_OUTPUT = re.compile(
    r"(?:^|\n)\s*(?:FAIL\b|Error:)|\b[1-9][0-9]* failed\b|\b(?:exit|exited) (?:with )?code [1-9][0-9]*\b",
    re.IGNORECASE,
)


def event_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def tool_result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    content = result.get("content")
    if not isinstance(content, list):
        return json.dumps(result, ensure_ascii=False)
    return "\n".join(
        str(part.get("text", "")) for part in content if isinstance(part, dict)
    )


def failed_tool_actions(events: list[dict[str, Any]]) -> int:
    failures = 0
    for row in events:
        if row.get("type") != "tool_execution_end":
            continue
        if row.get("isError") is True:
            failures += 1
            continue
        if row.get("toolName") == "bash" and FAILED_TOOL_OUTPUT.search(
            tool_result_text(row.get("result"))
        ):
            failures += 1
    return failures


def reasoning_tokens(events: list[dict[str, Any]]) -> int:
    total = 0
    for row in events:
        if row.get("type") != "message_end":
            continue
        message = row.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        value = usage.get("reasoning", 0) if isinstance(usage, dict) else 0
        if isinstance(value, (int, float)):
            total += int(value)
    return total


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
    return {
        "condition": condition,
        "context_calls": len(contexts),
        "delta_calls": len(nonempty),
        "native_identity": identity if condition == "H0" else None,
        "delta_appended_exactly_once": appended_once if condition == "H1" else None,
    }


def mean(rows: list[dict[str, Any]], path: tuple[str, ...]) -> float:
    values: list[float] = []
    for row in rows:
        value: Any = row
        for key in path:
            value = value[key]
        values.append(float(value))
    return statistics.fmean(values)


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
                "evaluation_version": 2,
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
        recovery_denominator = sum(row["failed_tool_actions"] > 0 for row in condition_rows)
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
            "failure_recovery_opportunities": recovery_denominator,
        }

    paired: list[dict[str, Any]] = []
    for task_id, pair in sorted(by_task.items()):
        h0 = pair["H0"]
        h1 = pair["H1"]
        paired.append(
            {
                "task_id": task_id,
                "H0_success": h0["evaluation_success"],
                "H1_success": h1["evaluation_success"],
                "success_delta": int(h1["evaluation_success"]) - int(h0["evaluation_success"]),
                "H0_strict_completion": h0["strict_completion_success"],
                "H1_strict_completion": h1["strict_completion_success"],
                "model_call_delta": h1["model_calls"] - h0["model_calls"],
                "tool_call_delta": h1["tool_calls"] - h0["tool_calls"],
                "wall_clock_delta_seconds": h1["wall_clock_seconds"] - h0["wall_clock_seconds"],
            }
        )

    summary = {
        "schema_version": 2,
        "source_index": str((root / "run-index.json").resolve()),
        "evaluator_change": "agent edits to evaluator-owned hidden test paths are excluded before hidden tests are applied",
        "harness_repo": original["harness_repo"],
        "harness_commit": original["harness_commit"],
        "model": original["model"],
        "conditions": conditions,
        "paired": paired,
        "rows": sorted(rows, key=lambda row: (row["task_id"], row["condition"])),
    }
    (root / "run-index-v2.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"conditions": conditions, "paired": paired}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

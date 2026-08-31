"""Analyze a complete native-vs-Causal-Transaction-Receipt screening batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def valid_row(row: dict[str, Any]) -> bool:
    return bool(row.get("integrity_valid"))


def summarize(rows: list[dict[str, Any]], condition: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("condition") == condition]
    usage_keys = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    return {
        "rows": len(selected),
        "evaluator_success": sum(bool(row.get("evaluation_success")) for row in selected),
        "strict_completion_success": sum(
            bool(row.get("strict_completion_success")) for row in selected
        ),
        "timeouts": sum(bool(row.get("timed_out")) for row in selected),
        "false_completion": sum(bool(row.get("false_completion")) for row in selected),
        "model_calls": sum(int(row.get("model_calls", 0)) for row in selected),
        "tool_calls": sum(int(row.get("tool_calls", 0)) for row in selected),
        "wall_clock_seconds": sum(float(row.get("wall_clock_seconds", 0)) for row in selected),
        "unsafe_or_invalid_actions": sum(
            int(row.get("unsafe_or_invalid_actions", 0)) for row in selected
        ),
        "runtime_safety_blocks": sum(
            int(row.get("runtime_preexecution_blocks", 0)) for row in selected
        ),
        "receipts": sum(int(row.get("ctr_receipts", 0)) for row in selected),
        "receipt_bytes": sum(int(row.get("peac_receipt_bytes", 0)) for row in selected),
        "failures_opened": sum(
            len(row.get("ctr_failures_opened", [])) for row in selected
        ),
        "failures_closed": sum(
            len(row.get("ctr_failures_closed", [])) for row in selected
        ),
        "integrity_valid": all(valid_row(row) for row in selected),
        "usage": {
            key: sum(float(row.get("usage", {}).get(key, 0)) for row in selected)
            for key in usage_keys
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    index = json.loads((root / "batch" / "run-index.json").read_text(encoding="utf-8"))
    rows = index.get("rows", [])
    if not isinstance(rows, list):
        raise RuntimeError("run-index rows must be a list")

    native = summarize(rows, "N")
    ctr = summarize(rows, "P")
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(str(row.get("task_id")), {})[str(row.get("condition"))] = row
    pairs: list[dict[str, Any]] = []
    for task_id, conditions in sorted(by_task.items()):
        native_row = conditions.get("N")
        ctr_row = conditions.get("P")
        if not native_row or not ctr_row:
            continue
        pairs.append(
            {
                "task_id": task_id,
                "native_success": bool(native_row.get("evaluation_success")),
                "ctr_success": bool(ctr_row.get("evaluation_success")),
                "native_strict": bool(native_row.get("strict_completion_success")),
                "ctr_strict": bool(ctr_row.get("strict_completion_success")),
                "native_timeout": bool(native_row.get("timed_out")),
                "ctr_timeout": bool(ctr_row.get("timed_out")),
            }
        )

    ctr_only = [
        pair["task_id"]
        for pair in pairs
        if pair["ctr_success"] and not pair["native_success"]
    ]
    ctr_regressions = [
        pair["task_id"]
        for pair in pairs
        if pair["native_success"] and not pair["ctr_success"]
    ]
    coverage = ratio(ctr["receipts"], ctr["tool_calls"] - ctr["runtime_safety_blocks"])
    call_ratio = ratio(ctr["model_calls"], native["model_calls"])
    token_ratio = ratio(ctr["usage"]["totalTokens"], native["usage"]["totalTokens"])
    wall_ratio = ratio(ctr["wall_clock_seconds"], native["wall_clock_seconds"])
    cost_values = [value for value in (call_ratio, token_ratio, wall_ratio) if value is not None]
    all_valid = bool(
        len(rows) == 6
        and native["integrity_valid"]
        and ctr["integrity_valid"]
        and native["rows"] == 3
        and ctr["rows"] == 3
    )
    mechanism_ok = bool(
        coverage is not None
        and coverage >= 0.95
        and ctr["receipts"] > 0
        and ctr["failures_opened"] >= ctr["failures_closed"]
    )
    safety_ok = bool(
        ctr["false_completion"] == 0
        and ctr["unsafe_or_invalid_actions"] <= native["unsafe_or_invalid_actions"]
    )
    cost_ok = bool(cost_values and max(cost_values) <= 1.25)
    primary_signal = bool(ctr_only and not ctr_regressions)
    go = bool(all_valid and mechanism_ok and safety_ok and cost_ok and primary_signal)
    if not all_valid:
        decision = "INVALID"
    elif ctr_regressions:
        decision = "NO_GO_REGRESSION"
    elif go:
        decision = "GO_SCREENING_SIGNAL"
    elif ctr["evaluator_success"] == native["evaluator_success"]:
        decision = "VALID_NULL_NO_GO"
    else:
        decision = "NO_GO"

    analysis = {
        "schema_version": 1,
        "method": "CTR",
        "complete": len(rows) == 6,
        "runner_index_complete": bool(index.get("complete")),
        "rows": len(rows),
        "native": native,
        "ctr": ctr,
        "pairs": pairs,
        "ctr_only_successes": ctr_only,
        "ctr_only_regressions": ctr_regressions,
        "receipt_coverage": coverage,
        "cost_ratios": {
            "model_calls": call_ratio,
            "total_tokens": token_ratio,
            "wall_clock": wall_ratio,
        },
        "gates": {
            "all_valid": all_valid,
            "mechanism_ok": mechanism_ok,
            "safety_ok": safety_ok,
            "cost_ok": cost_ok,
            "primary_signal": primary_signal,
        },
        "go": go,
        "decision": decision,
        "screening_only": True,
    }
    output = root / "batch" / "analysis.json"
    output.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

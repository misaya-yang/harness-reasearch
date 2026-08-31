"""Analyze a complete clean Pi native-vs-PEAC screening batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def effective_integrity(row: dict[str, Any]) -> bool:
    if bool(row.get("integrity_valid")):
        return True
    if not row.get("timed_out"):
        return False
    admitted = int(row.get("peac_actions_admitted", 0))
    observed = int(row.get("peac_observations_logged", 0))
    accounted_actions = (
        admitted
        + int(row.get("peac_admission_blocks", 0))
        + int(row.get("runtime_preexecution_blocks", 0))
    )
    return bool(
        row.get("runtime_sandbox_ready")
        and int(row.get("runtime_forbidden_payloads", 0)) == 0
        and int(row.get("peac_observation_unmatched", 0)) == 0
        and int(row.get("peac_executable_control_leaks", 0)) == 0
        and row.get("trace_process_healthy")
        and row.get("secret_scan_clean")
        and not row.get("monitor_failure")
        and not row.get("monitor_thread_alive")
        and not row.get("unreaped_descendants")
        and int(row.get("compaction_events", 0)) == 0
        and 0 <= accounted_actions - int(row.get("tool_calls", 0)) <= 1
        and 0 <= admitted - observed - len(row.get("peac_missing_results", [])) <= 1
    )


def condition_summary(rows: list[dict[str, Any]], condition: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("condition") == condition]
    usage_keys = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    return {
        "rows": len(selected),
        "evaluator_success": sum(bool(row.get("evaluation_success")) for row in selected),
        "strict_completion_success": sum(bool(row.get("strict_completion_success")) for row in selected),
        "false_completion": sum(bool(row.get("false_completion")) for row in selected),
        "timeouts": sum(bool(row.get("timed_out")) for row in selected),
        "failure_recovery_opportunities": sum(bool(row.get("failure_recovery_opportunity")) for row in selected),
        "failure_recovered": sum(bool(row.get("failure_recovered")) for row in selected),
        "model_calls": sum(int(row.get("model_calls", 0)) for row in selected),
        "tool_calls": sum(int(row.get("tool_calls", 0)) for row in selected),
        "wall_clock_seconds": sum(float(row.get("wall_clock_seconds", 0.0)) for row in selected),
        "model_visible_request_bytes": sum(int(row.get("model_visible_request_bytes", 0)) for row in selected),
        "usage": {
            key: sum(float(row.get("usage", {}).get(key, 0)) for row in selected)
            for key in usage_keys
        },
        "unsafe_or_invalid_actions": sum(int(row.get("unsafe_or_invalid_actions", 0)) for row in selected),
        "integrity_valid": all(effective_integrity(row) for row in selected),
        "resource_failures": sum(bool(row.get("monitor_failure")) for row in selected),
        "runtime_safety_blocks": sum(
            int(row.get("runtime_workspace_escape_blocks", 0))
            + int(row.get("runtime_forbidden_path_blocks", 0))
            + int(row.get("runtime_resource_action_blocks", 0))
            for row in selected
        ),
        "untraced_compaction_events": sum(int(row.get("compaction_events", 0)) for row in selected),
        "trace_complete": all(
            bool(row.get("peac_trace_complete", True)) or effective_integrity(row)
            for row in selected
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    index_path = root / "batch" / "run-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    rows = index.get("rows", [])
    if not isinstance(rows, list):
        raise RuntimeError("run-index rows must be a list")

    native = condition_summary(rows, "N")
    peac = condition_summary(rows, "P")
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        task_id = str(row.get("task_id"))
        condition = str(row.get("condition"))
        by_task.setdefault(task_id, {})[condition] = row

    pairs: list[dict[str, Any]] = []
    for task_id, conditions in sorted(by_task.items()):
        native_row = conditions.get("N")
        peac_row = conditions.get("P")
        if not native_row or not peac_row:
            continue
        pairs.append(
            {
                "task_id": task_id,
                "native_success": bool(native_row.get("evaluation_success")),
                "peac_success": bool(peac_row.get("evaluation_success")),
                "native_strict": bool(native_row.get("strict_completion_success")),
                "peac_strict": bool(peac_row.get("strict_completion_success")),
                "native_recovered": bool(native_row.get("failure_recovered")),
                "peac_recovered": bool(peac_row.get("failure_recovered")),
            }
        )

    peac_rows = [row for row in rows if row.get("condition") == "P"]
    admitted = sum(int(row.get("peac_actions_admitted", 0)) for row in peac_rows)
    blocked = sum(int(row.get("peac_admission_blocks", 0)) for row in peac_rows)
    opened = [
        str(surprise)
        for row in peac_rows
        for surprise in row.get("peac_surprises_opened", [])
    ]
    resolved = [
        str(surprise)
        for row in peac_rows
        for surprise in row.get("peac_surprises_resolved", [])
    ]
    open_at_end = [
        str(surprise)
        for row in peac_rows
        for surprise in row.get("peac_open_at_end", [])
    ]
    declaration_rate = ratio(admitted, admitted + blocked)
    peac_native_payload_bytes = sum(int(row.get("peac_native_payload_bytes", 0)) for row in peac_rows)
    peac_added_bytes = sum(
        int(row.get("peac_schema_added_bytes", 0)) + int(row.get("peac_receipt_bytes", 0))
        for row in peac_rows
    )
    peac_added_byte_ratio = ratio(peac_added_bytes, peac_native_payload_bytes)
    byte_ratio = ratio(peac["model_visible_request_bytes"], native["model_visible_request_bytes"])
    call_ratio = ratio(peac["model_calls"], native["model_calls"])
    token_ratio = ratio(peac["usage"]["totalTokens"], native["usage"]["totalTokens"])
    wall_ratio = ratio(peac["wall_clock_seconds"], native["wall_clock_seconds"])
    cost_ratios = [value for value in (call_ratio, token_ratio, wall_ratio) if value is not None]

    p_only_regressions = [
        pair["task_id"]
        for pair in pairs
        if pair["native_success"] and not pair["peac_success"]
    ]
    peac_only_successes = [
        pair["task_id"]
        for pair in pairs
        if pair["peac_success"] and not pair["native_success"]
    ]
    strict_gain = peac["strict_completion_success"] - native["strict_completion_success"]
    recovery_gain = peac["failure_recovered"] - native["failure_recovered"]

    mechanism_qualified = bool(
        declaration_rate is not None
        and declaration_rate >= 0.95
        and opened
        and not open_at_end
        and set(opened).issubset(set(resolved))
        and blocked == 0
        and all(row.get("peac_observation_unmatched", 0) == 0 for row in peac_rows)
        and all(not row.get("peac_missing_results") for row in peac_rows)
        and all(row.get("peac_executable_control_leaks", 0) == 0 for row in peac_rows)
        and peac_added_byte_ratio is not None
        and peac_added_byte_ratio <= 0.25
    )
    all_valid = bool(
        len(rows) == 6
        and native["integrity_valid"]
        and peac["integrity_valid"]
        and native["resource_failures"] == 0
        and peac["resource_failures"] == 0
        and native["untraced_compaction_events"] == 0
        and peac["untraced_compaction_events"] == 0
        and peac["trace_complete"]
    )
    primary_signal = bool(
        (peac["evaluator_success"] > native["evaluator_success"] or strict_gain >= 2 or recovery_gain >= 2)
        and not p_only_regressions
    )
    cost_ok = bool(cost_ratios and max(cost_ratios) <= 1.25)
    safety_ok = bool(
        peac["false_completion"] == 0
        and peac["runtime_safety_blocks"] <= native["runtime_safety_blocks"]
        and all(int(row.get("peac_observation_unmatched", 0)) == 0 for row in peac_rows)
        and all(not row.get("peac_missing_results") for row in peac_rows)
    )
    go = bool(all_valid and mechanism_qualified and primary_signal and cost_ok and safety_ok)
    if not opened:
        decision = "INCONCLUSIVE_NOT_TRIGGERED"
    elif go:
        decision = "GO_SCREENING_SIGNAL"
    elif p_only_regressions:
        decision = "NO_GO_PEAC_REGRESSION"
    elif all_valid and mechanism_qualified:
        decision = "VALID_NULL_NO_GO"
    elif all_valid:
        decision = "VALID_MECHANISM_FRICTION_NO_GO"
    else:
        decision = "INVALID_OR_MECHANISM_NO_GO"

    analysis = {
        "schema_version": 2,
        "complete": len(rows) == 6,
        "runner_index_complete": bool(index.get("complete")),
        "timeout_trace_tolerance": "At most one admitted in-flight action may lack a finalized assistant/tool-result event when the common 420-second timeout terminates the process.",
        "rows": len(rows),
        "native": native,
        "peac": peac,
        "pairs": pairs,
        "peac_only_successes": peac_only_successes,
        "peac_only_regressions": p_only_regressions,
        "strict_gain": strict_gain,
        "recovery_gain": recovery_gain,
        "mechanism": {
            "actions_admitted": admitted,
            "admission_blocks": blocked,
            "declaration_rate": declaration_rate,
            "surprises_opened": opened,
            "surprises_resolved": resolved,
            "open_at_end": open_at_end,
            "native_payload_bytes": peac_native_payload_bytes,
            "peac_added_bytes": peac_added_bytes,
            "peac_added_byte_ratio": peac_added_byte_ratio,
            "qualified": mechanism_qualified,
        },
        "cost_ratios": {
            "request_bytes": byte_ratio,
            "model_calls": call_ratio,
            "total_tokens": token_ratio,
            "wall_clock": wall_ratio,
        },
        "gates": {
            "all_valid": all_valid,
            "mechanism_qualified": mechanism_qualified,
            "primary_signal": primary_signal,
            "cost_ok": cost_ok,
            "safety_ok": safety_ok,
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

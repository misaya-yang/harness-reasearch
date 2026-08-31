"""Read-only, evidence-locating analysis of Pi agent action trajectories.

The analyzer reads only explicitly supplied run or batch directories and emits JSON to
stdout. It does not infer hidden chain-of-thought: ``visible_reasoning`` contains only
the visible ``thinking`` summaries present in ``events.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SOURCE_PATH_RE = re.compile(r"(?:^|/)(?:packages/[^/]+/)?src/")
TEST_PATH_RE = re.compile(r"(?:^|/)(?:test|tests|__tests__)(?:/|$)|\.(?:test|spec)\.[cm]?[jt]sx?$")
TEST_FILE_RE = re.compile(r"\b[^\s'\"]+\.(?:test|spec)\.[cm]?[jt]sx?\b")
MUTATING_SHELL_RE = re.compile(
    r"(?:\bperl\s+-pi\b|\bsed\s+-i\b|\bbiome\s+check\s+--write\b|"
    r"\bcp\b[^\n;]*(?:/src/|\bsrc/)|\bmv\b[^\n;]*(?:/src/|\bsrc/)|"
    r">\s*[^\s;]*(?:/src/|\bsrc/))"
)
BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unfocused_validation", re.compile(r"UNFOCUSED_[A-Z_]+_DISABLED")),
    ("forbidden_host_path", re.compile(r"EXPERIMENT_FORBIDDEN_HOST_PATH")),
    ("root_filesystem_scan", re.compile(r"ROOT_FILESYSTEM_SCAN_DISABLED")),
    ("host_process_inspection", re.compile(r"HOST_PROCESS_INSPECTION_DISABLED")),
    ("resource_action", re.compile(r"RESOURCE_ACTION|TSGO_DISABLED")),
    ("bash_timeout_limit", re.compile(r"BASH_TIMEOUT_LIMIT")),
    ("invalid_control", re.compile(r"(?:PEAC|CTR)_INVALID_CONTROL")),
    ("forbidden_path", re.compile(r"FORBIDDEN_PATH|forbidden_path_blocked", re.IGNORECASE)),
)
REASONING_KEYWORDS = re.compile(
    r"\b(?:wait|hmm|interesting|risk|concern|reconsider|confirmed|pre-existing|"
    r"blocked|failure|failed|fix|instead|hidden test|fake-this|baseline)\b",
    re.IGNORECASE,
)


def compact_text(value: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def iso_from_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_between(earlier: str | None, later: str | None) -> float | None:
    start = parse_iso(earlier)
    end = parse_iso(later)
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def is_source_path(path: str) -> bool:
    return bool(SOURCE_PATH_RE.search(path.replace("\\", "/")))


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path.replace("\\", "/")))


def extract_tool_target(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name in {"read", "edit", "write"}:
        return str(args.get("path", ""))
    if tool_name == "bash":
        return str(args.get("command", ""))
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


def classify_tool(tool_name: str, args: dict[str, Any]) -> str:
    target = extract_tool_target(tool_name, args)
    normalized = target.lower()
    if tool_name in {"edit", "write"}:
        if is_source_path(target):
            return "source_mutation"
        if is_test_path(target):
            return "test_mutation"
        return "artifact_mutation"
    if tool_name == "read":
        if is_source_path(target):
            return "source_read"
        if is_test_path(target):
            return "test_read"
        if target.endswith((".md", "AGENTS.md")):
            return "documentation_read"
        return "file_read"
    if tool_name != "bash":
        return tool_name or "unknown_tool"
    if MUTATING_SHELL_RE.search(target) and SOURCE_PATH_RE.search(target):
        return "source_mutation_shell"
    test_invocation = re.search(
        r"(?:\bvitest\b|vitest/dist/cli\.js|\.bin/vitest)[^;&|]*\s--run\b|"
        r"\bpytest\b|\bnode\s+--test\b|\bcargo\s+test\b|\bgo\s+test\b",
        normalized,
    )
    if test_invocation:
        explicit_tests = TEST_FILE_RE.findall(target)
        if len(set(explicit_tests)) == 1 or re.search(r"(?:^|\s)-t(?:\s|$)", target):
            return "focused_validation"
        return "multi_validation"
    if re.search(r"\b(?:tsgo|tsc|mypy|pyright)\b", normalized):
        return "typecheck"
    if re.search(r"\b(?:biome|ruff|eslint|prettier)\b", normalized):
        return "format_or_lint"
    if re.search(r"\bgit\s+(?:diff|status|show|rev-parse|branch)\b", normalized):
        return "vcs_inspection"
    if re.search(r"\b(?:rg|grep|find|sed\s+-n|head|tail|ls|cat|wc)\b", normalized):
        return "shell_inspection"
    return "shell_other"


def normalized_fingerprint(tool_name: str, category: str, target: str, workspace: str | None) -> str:
    normalized = target
    if workspace:
        normalized = normalized.replace(workspace, "$WORKSPACE")
    normalized = re.sub(r"/Users/Shared/pi-peac-experiment/runs/[^/]+/workspace", "$WORKSPACE", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return compact_text(f"{tool_name}:{category}:{normalized}", 500)


def result_text(event: dict[str, Any]) -> str:
    result = event.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
    return "\n".join(part for part in parts if part)


def blocked_reasons(text: str) -> list[str]:
    return [name for name, pattern in BLOCK_PATTERNS if pattern.search(text)]


def semantic_status(text: str, is_error: bool, ended: bool) -> str:
    if not ended:
        return "in_flight"
    if blocked_reasons(text):
        return "blocked"
    if is_error:
        return "tool_error"
    if re.search(r"Test Files\s+\d+\s+failed|No test files found|AssertionError|\bFAIL\b", text):
        return "validation_failed"
    if re.search(r"Test Files\s+\d+\s+passed|Tests\s+\d+\s+passed", text):
        return "validation_passed"
    return "completed"


def discover_runs(inputs: Iterable[Path]) -> list[Path]:
    discovered: set[Path] = set()
    for original in inputs:
        path = original.resolve()
        if path.is_file() and path.name == "events.jsonl":
            candidate = path.parent
            if (candidate / "run.json").is_file():
                discovered.add(candidate)
            continue
        if (path / "events.jsonl").is_file() and (path / "run.json").is_file():
            discovered.add(path)
            continue
        batch = path / "batch" if (path / "batch").is_dir() else path
        if batch.is_dir():
            for events_path in batch.glob("*/events.jsonl"):
                if (events_path.parent / "run.json").is_file():
                    discovered.add(events_path.parent.resolve())
    return sorted(discovered)


def source_mutation_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    target = extract_tool_target(tool_name, args)
    if tool_name in {"edit", "write"} and is_source_path(target):
        return [target]
    if tool_name == "bash" and MUTATING_SHELL_RE.search(target):
        candidates = re.findall(r"(?:/[^\s;'\"]+|[\w./-]+)", target)
        return sorted({item for item in candidates if is_source_path(item)})
    return []


def timeout_taxonomy(
    run: dict[str, Any], last_event: dict[str, Any] | None, last_provider: str | None, last_model: str | None
) -> dict[str, Any]:
    timed_out = bool(run.get("timed_out"))
    if not timed_out:
        classification = "not_timeout"
    elif last_event is None:
        classification = "timeout_without_trace_events"
    elif last_event.get("type") == "message_update":
        classification = "provider_stream_in_flight_at_cutoff"
    elif last_event.get("type") == "message_start" and last_event.get("role") == "assistant":
        classification = "provider_stream_start_at_cutoff"
    elif last_event.get("type") == "turn_start":
        classification = "between_turns_provider_request_pending_at_cutoff"
    elif last_event.get("type") in {"tool_execution_start", "tool_execution_update"}:
        classification = "tool_in_flight_at_cutoff"
    elif last_event.get("type") == "turn_end":
        classification = "timeout_after_completed_turn"
    else:
        classification = "timeout_trace_state_indeterminate"
    return {
        "classification": classification,
        "timed_out": timed_out,
        "process_exit_code": run.get("process_exit_code"),
        "final_event_line": last_event.get("line") if last_event else None,
        "final_event_type": last_event.get("type") if last_event else None,
        "final_message_update_type": last_event.get("update_type") if last_event else None,
        "last_completed_provider": last_provider,
        "last_completed_model": last_model,
        "root_cause_caveat": (
            "Taxonomy describes the final observable trace state; it does not prove a provider, "
            "network, tool, or harness root cause."
        ),
    }


def analyze_run(run_dir: Path, reasoning_limit: int) -> dict[str, Any]:
    run_path = run_dir / "run.json"
    events_path = run_dir / "events.jsonl"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    workspace: str | None = None
    session_started_at: str | None = None
    last_event: dict[str, Any] | None = None
    last_provider: str | None = None
    last_model: str | None = None
    last_completed_assistant_at: str | None = None
    completed_assistant_ms: list[float] = []
    call_meta: dict[str, dict[str, Any]] = {}
    tool_records: list[dict[str, Any]] = []
    tool_by_id: dict[str, dict[str, Any]] = {}
    visible_reasoning: list[dict[str, Any]] = []
    event_count = 0

    with events_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            event_count += 1
            event = json.loads(raw_line)
            event_type = str(event.get("type", ""))
            update = event.get("assistantMessageEvent")
            last_event = {
                "line": line_number,
                "type": event_type,
                "role": (event.get("message") or {}).get("role") if isinstance(event.get("message"), dict) else None,
                "update_type": update.get("type") if isinstance(update, dict) else None,
            }
            if event_type == "session":
                workspace = str(event.get("cwd", "")) or None
                session_started_at = str(event.get("timestamp", "")) or None
                continue
            if event_type == "message_end" and isinstance(event.get("message"), dict):
                message = event["message"]
                role = message.get("role")
                if role == "assistant":
                    timestamp_ms = message.get("timestamp")
                    timestamp = iso_from_ms(timestamp_ms)
                    last_completed_assistant_at = timestamp
                    if isinstance(timestamp_ms, (int, float)):
                        completed_assistant_ms.append(float(timestamp_ms))
                    last_provider = str(message.get("provider", "")) or last_provider
                    last_model = str(message.get("model", "")) or last_model
                    for content_index, item in enumerate(message.get("content", [])):
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "thinking" and isinstance(item.get("thinking"), str):
                            text = item["thinking"]
                            visible_reasoning.append(
                                {
                                    "events_line": line_number,
                                    "content_index": content_index,
                                    "timestamp": timestamp,
                                    "snippet": compact_text(text),
                                    "keyword_selected": bool(REASONING_KEYWORDS.search(text)),
                                    "source_kind": "visible_thinking_summary",
                                }
                            )
                        if item.get("type") == "toolCall":
                            tool_call_id = str(item.get("id", ""))
                            if tool_call_id:
                                call_meta[tool_call_id] = {
                                    "assistant_events_line": line_number,
                                    "requested_at": timestamp,
                                }
                elif role == "toolResult":
                    tool_call_id = str(message.get("toolCallId", ""))
                    record = tool_by_id.get(tool_call_id)
                    if record is not None:
                        record["completed_at"] = iso_from_ms(message.get("timestamp"))
                continue
            if event_type == "tool_execution_start":
                tool_call_id = str(event.get("toolCallId", ""))
                tool_name = str(event.get("toolName", ""))
                args = event.get("args") if isinstance(event.get("args"), dict) else {}
                category = classify_tool(tool_name, args)
                target = extract_tool_target(tool_name, args)
                record = {
                    "tool_call_id": tool_call_id,
                    "events_line": line_number,
                    "assistant_events_line": call_meta.get(tool_call_id, {}).get("assistant_events_line"),
                    "requested_at": call_meta.get(tool_call_id, {}).get("requested_at"),
                    "completed_at": None,
                    "tool": tool_name,
                    "category": category,
                    "target": compact_text(target, 360),
                    "source_paths": source_mutation_paths(tool_name, args),
                    "ended": False,
                    "is_error": False,
                    "status": "in_flight",
                    "blocked_reasons": [],
                    "result_excerpt": "",
                    "fingerprint": normalized_fingerprint(tool_name, category, target, workspace),
                }
                tool_records.append(record)
                tool_by_id[tool_call_id] = record
                continue
            if event_type == "tool_execution_end":
                tool_call_id = str(event.get("toolCallId", ""))
                record = tool_by_id.get(tool_call_id)
                if record is None:
                    continue
                text = result_text(event)
                record["ended"] = True
                record["is_error"] = bool(event.get("isError"))
                record["blocked_reasons"] = blocked_reasons(text)
                record["status"] = semantic_status(text, record["is_error"], True)
                non_receipt = [line for line in text.splitlines() if not line.startswith(("[CTR]", "[PEAC]"))]
                record["result_excerpt"] = compact_text("\n".join(non_receipt), 280)

    cutoff_estimated_at: str | None = None
    start_dt = parse_iso(session_started_at)
    wall_seconds = run.get("wall_clock_seconds")
    if start_dt is not None and isinstance(wall_seconds, (int, float)):
        cutoff_estimated_at = (start_dt + timedelta(seconds=float(wall_seconds))).isoformat().replace("+00:00", "Z")

    source_mutations = [
        record for record in tool_records if record["category"] in {"source_mutation", "source_mutation_shell"}
    ]
    first_mutation = source_mutations[0] if source_mutations else None
    last_mutation = source_mutations[-1] if source_mutations else None
    last_mutation_time = None
    last_mutation_basis = None
    if last_mutation:
        if last_mutation.get("completed_at"):
            last_mutation_time = last_mutation["completed_at"]
            last_mutation_basis = "tool_result_timestamp"
        else:
            last_mutation_time = last_mutation.get("requested_at")
            last_mutation_basis = "assistant_tool_call_timestamp"

    focused = [record for record in tool_records if record["category"] == "focused_validation"]
    completed_focused = [record for record in focused if record["ended"]]
    fingerprints = Counter(record["fingerprint"] for record in tool_records)
    repeated = [
        {"count": count, "fingerprint": fingerprint}
        for fingerprint, count in fingerprints.most_common()
        if count > 1
    ]
    blocked = [record for record in tool_records if record["blocked_reasons"]]
    block_counts = Counter(reason for record in blocked for reason in record["blocked_reasons"])
    gaps = [
        (later - earlier) / 1000
        for earlier, later in zip(completed_assistant_ms, completed_assistant_ms[1:])
    ]
    keyword_reasoning = [item for item in visible_reasoning if item["keyword_selected"]]
    selected_reasoning = keyword_reasoning[-reasoning_limit:] if reasoning_limit else []
    if not selected_reasoning and reasoning_limit:
        selected_reasoning = visible_reasoning[-reasoning_limit:]

    def public_tool(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        return {key: value for key, value in record.items() if key not in {"fingerprint", "ended", "is_error"}}

    return {
        "run_dir": str(run_dir),
        "task_id": run.get("task_id"),
        "condition": run.get("condition"),
        "evaluation_success": bool(run.get("evaluation_success")),
        "strict_completion_success": bool(run.get("strict_completion_success")),
        "event_count_parsed": event_count,
        "timing": {
            "session_started_at": session_started_at,
            "cutoff_estimated_at": cutoff_estimated_at,
            "cutoff_basis": "session timestamp + run.json wall_clock_seconds",
            "source_mutation_count": len(source_mutations),
            "first_source_mutation": public_tool(first_mutation),
            "last_source_mutation": public_tool(last_mutation),
            "last_mutation_time_basis": last_mutation_basis,
            "seconds_from_last_mutation_to_cutoff": seconds_between(last_mutation_time, cutoff_estimated_at),
            "last_completed_assistant_at": last_completed_assistant_at,
            "seconds_from_last_completed_assistant_to_cutoff": seconds_between(
                last_completed_assistant_at, cutoff_estimated_at
            ),
            "max_completed_assistant_gap_seconds": round(max(gaps), 3) if gaps else None,
            "completed_assistant_gaps_over_10_seconds": sum(gap > 10 for gap in gaps),
        },
        "validation": {
            "focused_validation_attempts": len(focused),
            "last_focused_validation_attempt": public_tool(focused[-1] if focused else None),
            "last_completed_focused_validation": public_tool(
                completed_focused[-1] if completed_focused else None
            ),
        },
        "last_10_tools": [public_tool(record) for record in tool_records[-10:]],
        "repeated_actions": repeated,
        "blocked_actions": {
            "count": len(blocked),
            "by_reason": dict(sorted(block_counts.items())),
            "items": [public_tool(record) for record in blocked],
        },
        "timeout_taxonomy": timeout_taxonomy(run, last_event, last_provider, last_model),
        "visible_reasoning": {
            "total_visible_thinking_summaries": len(visible_reasoning),
            "selected_snippets": selected_reasoning,
            "selection": "last keyword-bearing visible thinking summaries, falling back to the latest summaries",
            "cot_boundary": (
                "Only events.jsonl content items with type=thinking are reported. Hidden or encrypted "
                "chain-of-thought is neither available nor inferred."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Explicit run, batch, result-root, or events.jsonl paths; no implicit results scan is performed.",
    )
    parser.add_argument("--reasoning-limit", type=int, default=8)
    args = parser.parse_args()
    if args.reasoning_limit < 0:
        parser.error("--reasoning-limit must be non-negative")
    runs = discover_runs(args.paths)
    if not runs:
        parser.error("no run directories containing both events.jsonl and run.json were found")
    report = {
        "schema_version": 1,
        "analyzer": "pi-agent-trajectory-actions-read-only",
        "input_policy": "explicit paths only",
        "output_policy": "stdout JSON only",
        "runs": [analyze_run(run_dir, args.reasoning_limit) for run_dir in runs],
    }
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared JSONL trace helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(UTC).isoformat(timespec="milliseconds")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Append one UTF-8 JSON object, creating only the requested parent."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def trace_event(
    *,
    run_id: str,
    task_id: str,
    condition: str,
    replicate: int,
    model: str,
    step: int,
    event_type: str,
    source: str,
    content: str,
    is_external_evidence: bool,
    belief_state: dict[str, Any] | None = None,
    tool: dict[str, Any] | None = None,
    token_usage: dict[str, Any] | None = None,
    parent_ids: list[str] | None = None,
    latency_ms: float | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the canonical trace row used by all runners."""

    return {
        "run_id": run_id,
        "task_id": task_id,
        "condition": condition,
        "replicate": replicate,
        "model": model,
        "step": step,
        "event_type": event_type,
        "content": content,
        "source": source,
        "is_external_evidence": is_external_evidence,
        "belief_state": belief_state or {},
        "tool": tool,
        "token_usage": token_usage or {},
        "parent_ids": parent_ids or [],
        "latency_ms": latency_ms,
        "request_id": request_id,
        "timestamp": utc_now(),
    }

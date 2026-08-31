"""Conservative conversion helpers for native harness trace records."""

from __future__ import annotations

from typing import Any


def normalize_event(raw: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    """Normalize common native field aliases without inventing provenance.

    Missing provenance is represented as ``unknown`` rather than inferred from
    the text. This keeps native trace conversion suitable for audit evidence.
    """

    source = raw.get("source", raw.get("role", "unknown"))
    event_type = raw.get("event_type", raw.get("type", "unknown"))
    content = raw.get("content", raw.get("text", ""))
    external = raw.get("is_external_evidence", raw.get("external_evidence"))
    return {
        "run_id": raw.get("run_id"),
        "task_id": raw.get("task_id"),
        "condition": raw.get("condition"),
        "replicate": raw.get("replicate"),
        "model": model or raw.get("model"),
        "step": raw.get("step"),
        "event_type": event_type,
        "content": content if isinstance(content, str) else str(content),
        "source": source,
        "is_external_evidence": external if isinstance(external, bool) else None,
        "belief_state": raw.get("belief_state", {}),
        "tool": raw.get("tool"),
        "token_usage": raw.get("token_usage", raw.get("usage", {})),
        "latency_ms": raw.get("latency_ms"),
        "request_id": raw.get("request_id"),
        "parent_ids": raw.get("parent_ids", []),
        "timestamp": raw.get("timestamp"),
    }

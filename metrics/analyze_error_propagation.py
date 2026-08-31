"""Analyze conservative contradiction-to-action propagation in JSONL traces."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .core import load_jsonl


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_run: dict[tuple[str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("run_id", "unknown")),
            str(row.get("task_id", "unknown")),
            str(row.get("condition", "unknown")),
            int(row.get("replicate", 0)) if isinstance(row.get("replicate"), int) else 0,
        )
        by_run[key].append(row)
    run_summaries: list[dict[str, Any]] = []
    for (run_id, task_id, condition, replicate), run_rows in sorted(by_run.items()):
        ordered = sorted(run_rows, key=lambda row: int(row.get("step", 0)))
        contradiction_steps = [
            int(row["step"])
            for row in ordered
            if str(row.get("event_type", "")).lower() in {"contradiction", "tool_error", "error"}
            and isinstance(row.get("step"), int)
        ]
        first_contradiction = min(contradiction_steps) if contradiction_steps else None
        actions_after = sum(
            str(row.get("event_type", "")).lower() in {"action", "tool_call"}
            and isinstance(row.get("step"), int)
            and first_contradiction is not None
            and int(row["step"]) > first_contradiction
            for row in ordered
        )
        run_summaries.append(
            {
                "run_id": run_id,
                "task_id": task_id,
                "condition": condition,
                "replicate": replicate,
                "first_contradiction_step": first_contradiction,
                "actions_after_first_contradiction": actions_after,
            }
        )
    return {"schema_version": 1, "runs": run_summaries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(summarize(load_jsonl(args.trace)), ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

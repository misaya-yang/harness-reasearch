"""Measure explicit state-field loss between adjacent summary events."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core import load_jsonl


def _state_keys(row: dict[str, Any]) -> set[str]:
    belief_state = row.get("belief_state")
    if not isinstance(belief_state, dict):
        return set()
    return set(belief_state)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [row for row in rows if row.get("event_type") == "summary"]
    losses: list[dict[str, Any]] = []
    for previous, current in zip(summaries, summaries[1:]):
        before = _state_keys(previous)
        after = _state_keys(current)
        losses.append(
            {
                "run_id": current.get("run_id"),
                "task_id": current.get("task_id"),
                "step_before": previous.get("step"),
                "step_after": current.get("step"),
                "fields_dropped": sorted(before - after),
            }
        )
    return {
        "schema_version": 1,
        "summary_events": len(summaries),
        "adjacent_summary_pairs": len(losses),
        "pairs": losses,
        "note": "This detects explicit field loss only; content-level hypothesis upgrades need annotation.",
    }


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


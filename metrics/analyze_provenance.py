"""Count explicit provenance fields and conservative provenance violations."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .core import load_jsonl


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sources = Counter(str(row.get("source", "unknown")) for row in rows)
    event_types = Counter(str(row.get("event_type", "unknown")) for row in rows)
    unknown = sum(row.get("is_external_evidence") is None for row in rows)
    model_marked_external = sum(
        row.get("source") == "model" and row.get("is_external_evidence") is True for row in rows
    )
    return {
        "schema_version": 1,
        "events": len(rows),
        "source_counts": dict(sorted(sources.items())),
        "event_type_counts": dict(sorted(event_types.items())),
        "unknown_provenance_events": unknown,
        "model_marked_external_evidence_events": model_marked_external,
        "interpretation": "Counts are diagnostic; semantic provenance requires task-level annotation.",
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


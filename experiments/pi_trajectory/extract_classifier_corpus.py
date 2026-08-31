#!/usr/bin/env python3
"""Round 1 Phase C, track (i): extract the gate-anchored validation-command corpus from
real v1-v5 / H0-H4 Pi request logs for classifier distribution recomputation.

Read-only over results/. Writes one fixture JSON into the pi repo research-extensions
fixtures directory. `ok` is derived from Pi's bash failure serialization
(bash.ts:474 throws `Command exited with code <n>` appended to output text).
"""

import hashlib
import json
import re
from pathlib import Path

RESULTS = Path("/Users/yang/projects/reports/research-self-correction/results")
OUT = Path("/Users/yang/projects/opensource-harness/pi/research-extensions/fixtures/r1_classifier_corpus.json")

SOURCES = [
    "20260830_pi_qwen_paired_v1",
    "20260830_pi_validation_reconciliation_v1",
    "20260830_pi_completion_evidence_v1",
    "20260830_pi_ebcp_paired_v1",
    "20260830_pi_ebcp_paired_v2",
    "20260830_pi_ebcp_paired_v3",
    "20260830_pi_ebcp_paired_v4",
    "20260830_pi_ebcp_paired_v5",
]

EXIT_RE = re.compile(r"Command exited with code (\S+)")
TAIL_CHARS = 3000


def harvest(path: Path) -> dict:
    calls: dict = {}
    found: dict = {}
    with path.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") or {}
            items = payload.get("input")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "function_call" and item.get("name") == "bash":
                    calls[item.get("call_id")] = item.get("arguments") or ""
                elif item.get("type") == "function_call_output":
                    out = item.get("output")
                    if isinstance(out, list):
                        out = "\n".join(p.get("text", "") for p in out if isinstance(p, dict))
                    out = out or ""
                    args = calls.get(item.get("call_id"))
                    if args is None:
                        continue
                    try:
                        command = json.loads(args).get("command")
                    except json.JSONDecodeError:
                        continue
                    if not command:
                        continue
                    m = EXIT_RE.search(out)
                    rec = found.get(command)
                    entry = {
                        "command": command,
                        "ok": (not bool(m)) if m is None or m.group(1) == "0" else False,
                        "exit_code": (m.group(1) if m else "0"),
                        "output_tail": out[-TAIL_CHARS:],
                    }
                    if rec is None or len(entry["output_tail"]) >= len(rec["output_tail"]):
                        found[command] = entry
    return found


def main() -> None:
    all_rows: dict = {}
    per_source: dict = {}
    for name in SOURCES:
        d = RESULTS / name
        if not d.is_dir():
            per_source[name] = "missing"
            continue
        count = 0
        for log in sorted(d.glob("*/model-requests.jsonl")):
            for command, entry in harvest(log).items():
                count += 1
                if command not in all_rows:
                    all_rows[command] = entry
        per_source[name] = count
    rows = sorted(all_rows.values(), key=lambda r: hashlib.sha256(r["command"].encode()).hexdigest())
    fixture = {
        "meta": {
            "purpose": "Round 1 Phase C classifier acceptance, track (i): distribution recomputation over real gate-relevant bash commands",
            "sources": per_source,
            "unique_commands": len(rows),
            "ok_rule": "Pi bash.ts:474 appends 'Command exited with code N' on failure; absence means exit 0",
            "output_tail_chars": TAIL_CHARS,
        },
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT} unique={len(rows)}")


if __name__ == "__main__":
    main()

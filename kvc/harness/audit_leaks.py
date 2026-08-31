"""Classify a KVC run by out-of-workspace information access.

The actor's tool surface is not OS-sandboxed (pi's bash tool has no sandbox;
the legacy pipeline sandboxed only the evaluator phase). Whether a run solved
the task natively or leaned on external references is therefore an observable,
first-class variable: every bash/read target is recorded in events.jsonl.

Leak tiers (worst wins):
  clean   no tool call touched anything outside the task workspace except
          benign interpreter/toolchain paths (none currently whitelisted)
  harness the run inspected its own run directory / validator config / the
          KVC harness sources (task.json exposes gold_commit + source_repo)
  gold    the run reached the upstream Pi checkout or otherwise read material
          that contains the solved state (gold_commit show, fixed sources)

Usage: python3 -m kvc.harness.audit_leaks <run-dir> [<run-dir> ...]
Prints one JSON line per run; exit code always 0 (reporting, not gating).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Substrings that indicate contact with the upstream solution source.
GOLD_MARKERS = ("Agent_projects/pi", "Agent_projects\\\\pi")
# Substrings that indicate contact with the harness / run internals.
# "results/kvc" also catches sibling-run access: other runs' workspaces may
# hold passing patches, so reading them is contamination even though the
# path names no gold checkout.
HARNESS_MARKERS = (
    "/run/validator",
    "validator/task.json",
    "task.json",
    "harness-reasearch/kvc",
    "results/kvc",
    "gold_commit",
    "hidden_test",
    "kvc-validate",
    "/run/agent-dir",
)


def tool_targets(events_path: Path):
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if frame.get("type") != "tool_execution_start":
            continue
        yield frame.get("toolName") or "", json.dumps(frame.get("args") or {}, ensure_ascii=False)


def audit_run(run_base: Path) -> dict:
    events = run_base / "run" / "events" / "events.jsonl"
    report = {"run": run_base.name, "tier": "clean", "gold_hits": [], "harness_hits": []}
    if not events.exists():
        report["tier"] = "error"
        report["detail"] = "events.jsonl missing"
        return report
    own_id = run_base.name
    for tool_name, blob in tool_targets(events):
        # The run's own directories are legitimately visible to it (workspace,
        # KVC_RUN_DIR env, validator config); only OTHER runs' dirs are
        # contamination. A blob naming the own run id is treated as self.
        if own_id in blob:
            continue
        for marker in GOLD_MARKERS:
            if marker in blob:
                report["gold_hits"].append({"tool": tool_name, "snippet": blob[:200]})
        for marker in HARNESS_MARKERS:
            if marker in blob:
                report["harness_hits"].append({"tool": tool_name, "snippet": blob[:200]})
    if report["gold_hits"]:
        report["tier"] = "gold"
    elif report["harness_hits"]:
        report["tier"] = "harness"
    report["gold_count"] = len(report["gold_hits"])
    report["harness_count"] = len(report["harness_hits"])
    return report


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    for arg in argv[1:]:
        report = audit_run(Path(arg).resolve())
        compact = {
            "run": report["run"],
            "tier": report["tier"],
            "gold_count": report.get("gold_count", 0),
            "harness_count": report.get("harness_count", 0),
        }
        if report["tier"] != "clean":
            hits = report.get("gold_hits") or report.get("harness_hits") or []
            compact["first_hits"] = hits[:3]
        print(json.dumps(compact, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Classify a KVC run by out-of-workspace information access.

The actor's tool surface is not OS-sandboxed (pi's bash tool has no sandbox;
the legacy pipeline sandboxed only the evaluator phase). Whether a run solved
the task natively or leaned on external references is therefore an observable,
first-class variable: every bash/read target AND every tool result is recorded
in events.jsonl.

Leak tiers (worst wins):
  clean   no tool call touched anything outside the task workspace except
          benign interpreter/toolchain paths (none currently whitelisted)
  harness the run inspected its own run directory / validator config / the
          KVC harness sources (task.json exposes gold_commit + source_repo)
  gold    the run reached the upstream Pi checkout or otherwise read material
          that contains the solved state (gold_commit show, fixed sources)

Scan coverage (fork-integrity review A5, 2026-08-31): both
tool_execution_start args AND tool_execution_end result blobs are scanned —
an `env` dump's args carry no marker while its RESULT prints KVC_RUN_DIR; a
`cd ..` + listing surfaces sibling runs. Self-attribution is a
component-boundary path-prefix match on the run's own base dir, not the old
"own run-id substring anywhere in the blob" skip: in fork land every child
run-id embeds its donor run-id, so the substring skip let sibling/donor
paths escape as "self" for the donor. Harness markers inside result blobs
must be path-anchored (preceded by a separator) so bare filenames in an
`ls` of the run's own workspace do not over-flag; gold markers are counted
in both phases, but a result-phase-only gold hit earns the harness tier,
not gold (passive PATH/env printout of the agent-dir/bin symlink target,
not actor navigation — see the tier decision in audit_run).

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

# Characters that terminate a path run inside a JSON-dumped blob (json.dumps
# escapes real newlines to the two-char sequence \n, so backslash stops runs).
_PATH_STOP = set(" \t\r\n\"'`|<>,;:()[]{}\\")


def tool_targets(events_path: Path):
    """(tool_name, blob, phase) for start args and end results alike."""
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        ftype = frame.get("type")
        if ftype == "tool_execution_start":
            yield (frame.get("toolName") or "",
                   json.dumps(frame.get("args") or {}, ensure_ascii=False),
                   "args")
        elif ftype == "tool_execution_end":
            yield (frame.get("toolName") or "",
                   json.dumps(frame.get("result") or {}, ensure_ascii=False),
                   "result")


def audit_run(run_base: Path) -> dict:
    events = run_base / "run" / "events" / "events.jsonl"
    report = {"run": run_base.name, "tier": "clean", "gold_hits": [], "harness_hits": []}
    if not events.exists():
        report["tier"] = "error"
        report["detail"] = "events.jsonl missing"
        return report
    own_base = str(run_base)

    def self_attributed(blob: str, pos: int) -> bool:
        """True when the marker occurrence at pos sits inside a path that is
        the run's own base dir or a true subpath of it. Component boundary:
        after the own-base string, only a path terminator (stop char or "/")
        counts as self; a component-continuation char means a LONGER sibling
        dir name (e.g. a fork child id embedding its donor id) and stays
        flagged."""
        # Occurrences starting at or before pos may still extend past it
        # (marker inside the own-base path itself, e.g. env dumps).
        search_end = pos + len(own_base)
        while True:
            idx = blob.rfind(own_base, 0, search_end)
            if idx == -1 or idx > pos:
                return False
            end = idx + len(own_base)
            if (end >= len(blob) or blob[end] == "/"
                    or blob[end] in _PATH_STOP):
                extent = end
                while extent < len(blob) and blob[extent] not in _PATH_STOP:
                    extent += 1
                if idx <= pos < extent:
                    return True
            search_end = idx

    def scan(blob: str, phase: str) -> None:
        for markers, bucket, anchor_only in (
            (GOLD_MARKERS, "gold_hits", False),
            (HARNESS_MARKERS, "harness_hits", phase == "result"),
        ):
            for marker in markers:
                start = 0
                while True:
                    pos = blob.find(marker, start)
                    if pos == -1:
                        break
                    start = pos + 1
                    if anchor_only and pos > 0 and blob[pos - 1] not in "/\\":
                        continue  # bare filename in output, not a path leak
                    if self_attributed(blob, pos):
                        continue
                    report[bucket].append(
                        {"tool": None, "phase": phase,
                         "snippet": blob[max(0, pos - 60):pos + 140]})

    for tool_name, blob, phase in tool_targets(events):
        before_g, before_h = len(report["gold_hits"]), len(report["harness_hits"])
        scan(blob, phase)
        for hit in report["gold_hits"][before_g:] + report["harness_hits"][before_h:]:
            hit["tool"] = tool_name
    # Tier decision. A RESULT-phase-only gold hit is a passive printout: the
    # harness builds agent-dir/bin from symlinks into the pi checkout's
    # node_modules/.bin, so any env/PATH dump or module-resolution error
    # prints that path without the actor having navigated anywhere. That is
    # harness-fixture contact, not solution reading; only ARGS-phase gold
    # hits (the actor steering a command AT the gold tree) earn the gold
    # tier. Documented amendment 2026-08-31 (fork-integrity review A5).
    gold_args = [h for h in report["gold_hits"] if h["phase"] == "args"]
    if gold_args:
        report["tier"] = "gold"
    elif report["gold_hits"]:
        report["tier"] = "harness"
        report["gold_passive_only"] = True
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

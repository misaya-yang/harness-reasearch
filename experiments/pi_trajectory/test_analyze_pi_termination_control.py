"""Synthetic-fixture self-tests for analyze_pi_termination_control.py (pre-freeze).

These exercise the bucket discrimination and the conjunctive checks on fabricated
run-indices so the analyzer is proven against its own registered semantics BEFORE any
real R1 row exists. No provider, no Pi.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ANALYZER = Path(__file__).resolve().parent / "analyze_pi_termination_control.py"


def row(task: str, cond: str, root: Path, gate_rows: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    run_dir = root / f"{task}__{cond}__r1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "completion-gate.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in gate_rows), encoding="utf-8"
    )
    counts: dict[str, int] = {}
    for r in gate_rows:
        counts[str(r.get("event"))] = counts.get(str(r.get("event")), 0) + 1
    base = {
        "task_id": task,
        "condition": cond,
        "run_dir": str(run_dir),
        "replicate": 1,
        "monitor_failure": None,
        "timed_out": False,
        "evaluation_success": False,
        "strict_completion_success": False,
        "false_completion": False,
        "model_calls": 5,
        "wall_clock_seconds": 100.0,
        "usage": {"totalTokens": 5000},
        "gate_event_counts": counts,
    }
    base.update(over)
    return base


def witness(seq: int, verdict: str, fp: str, cond: str) -> dict[str, Any]:
    event = "witness" if cond == "G" else "OBSERVE_WITNESS"
    return {"ts": f"2026-08-30T00:00:{seq:02d}Z", "event": event, "seq": seq, "verdict": verdict, "fingerprint": fp, "ready": verdict.startswith("pass")}


def run(index: dict[str, Any], tmp: Path) -> dict[str, Any]:
    idx = tmp / "run-index.json"
    idx.write_text(json.dumps(index), encoding="utf-8")
    out = tmp / "comparison-index.json"
    proc = subprocess.run([sys.executable, str(ANALYZER), str(idx), "--output", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def manifest() -> dict[str, Any]:
    return {"authority": "test", "censoring_note": "test"}


def scenario_go(tmp: Path) -> None:
    gate_ok = [{"ts": "x", "event": "GATE_ARM", "fingerprint": "fp1"}, witness(1, "pass_high", "fp1", "G")]
    rows = [
        row("t1", "G", tmp, gate_ok, strict_completion_success=True, evaluation_success=True,
            gate_event_counts={"GATE_ARM": 1, "witness": 1}, finalize_accepted=True),
        row("t2", "G", tmp, gate_ok, strict_completion_success=True, evaluation_success=True),
        row("t3", "G", tmp, [witness(1, "pass_low", "fp1", "G"),
                             {"ts": "x", "event": "finalize_decision", "decision": {"status": "accepted", "workspaceFingerprint": "fp1"}}],
            strict_completion_success=False, evaluation_success=True, timed_out=True),
        row("t1", "N", tmp, [witness(1, "pass_high", "fp1", "N")], timed_out=True),
        row("t2", "N", tmp, [witness(1, "pass_low", "fp1", "N")], timed_out=True),
        row("t3", "N", tmp, [witness(1, "pass_high", "fp1", "N")], timed_out=True),
    ]
    # c5 needs accepted on a task whose paired N was ready AND timed out -> t1/t2 qualify;
    # accepted flag in obligation_profile comes from finalize_decision rows; add to t1/t2:
    (tmp / "t1__G__r1" / "completion-gate.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in [witness(1, "pass_high", "fp1", "G"),
                                               {"ts": "x", "event": "GATE_ARM", "fingerprint": "fp1"},
                                               {"ts": "y", "event": "finalize_decision", "decision": {"status": "accepted", "workspaceFingerprint": "fp1"}}]),
        encoding="utf-8",
    )
    (tmp / "t2__G__r1" / "completion-gate.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in [witness(1, "pass_high", "fp1", "G"),
                                               {"ts": "x", "event": "GATE_ARM", "fingerprint": "fp1"},
                                               {"ts": "y", "event": "finalize_decision", "decision": {"status": "accepted", "workspaceFingerprint": "fp1"}}]),
        encoding="utf-8",
    )
    result = run({"complete": True, "manifest": manifest(), "rows": rows}, tmp)
    assert result["w_g"] == 3 and result["w_n"] == 3, result
    assert result["primary_G"]["strict_in_W"] == 2 and result["primary_N"]["strict_in_W"] == 0
    assert result["GO"] is True, result["go_checks"]
    assert result["falsification_bucket"]["primary"] == "GO"
    assert set(result["mechanism_clause_task_hits"]) == {"t1", "t2", "t3"}  # all three N rows ready+timed out; all G accepted


def scenario_fb(tmp: Path) -> None:
    rows = [
        row("t1", "G", tmp, [witness(1, "pass_high", "fp1", "G")], false_completion=True),
        row("t1", "N", tmp, [witness(1, "pass_high", "fp1", "N")]),
    ]
    result = run({"complete": True, "manifest": manifest(), "rows": rows}, tmp)
    assert result["falsification_bucket"]["primary"] == "F-B", result["falsification_bucket"]
    assert result["GO"] is False


def scenario_fd0(tmp: Path) -> None:
    rows = [
        row("t1", "G", tmp, [witness(1, "fail", "fp1", "G")]),
        row("t1", "N", tmp, [witness(1, "fail", "fp1", "N")]),
    ]
    result = run({"complete": True, "manifest": manifest(), "rows": rows}, tmp)
    assert result["falsification_bucket"]["primary"] == "F-D0", result["falsification_bucket"]


def scenario_fd1(tmp: Path) -> None:
    rows = [
        row("t1", "G", tmp, [witness(1, "pass_high", "fp1", "G")]),
        row("t2", "G", tmp, [witness(1, "pass_low", "fp2", "G")]),
        row("t3", "G", tmp, []),
        row("t1", "N", tmp, [witness(1, "pass_high", "fp1", "N")]),
        row("t2", "N", tmp, [witness(1, "pass_high", "fp2", "N")]),
        row("t3", "N", tmp, []),
    ]
    result = run({"complete": True, "manifest": manifest(), "rows": rows}, tmp)
    assert result["falsification_bucket"]["primary"] == "F-D1", result["falsification_bucket"]


def scenario_fa(tmp: Path) -> None:
    armed = [
        witness(1, "pass_high", "fp1", "G"),
        {"ts": "x", "event": "GATE_ARM", "fingerprint": "fp1"},
        {"ts": "y", "event": "GATE_CONTINUE", "arms": 1},
        {"ts": "z", "event": "OBLIGATION_TEXT", "text": "re-run tests", "sha256": "a"},
        {"ts": "w", "event": "OBLIGATION_TEXT", "text": "re-run tests", "sha256": "a"},
    ]
    rows = [
        row("t1", "G", tmp, armed, timed_out=True),
        row("t2", "G", tmp, armed, timed_out=True),
        row("t3", "G", tmp, [witness(1, "pass_high", "fp1", "G")]),
        row("t1", "N", tmp, [witness(1, "pass_high", "fp1", "N")], timed_out=True),
        row("t2", "N", tmp, [witness(1, "pass_high", "fp1", "N")], timed_out=True),
        row("t3", "N", tmp, [witness(1, "pass_high", "fp1", "N")], timed_out=True),
    ]
    result = run({"complete": True, "manifest": manifest(), "rows": rows}, tmp)
    bucket = result["falsification_bucket"]
    assert bucket["primary"] == "F-A", bucket
    assert len(bucket["subtyping"]["per_row"]) == 2
    assert bucket["subtyping"]["per_row"][0]["obligation_rewrite_ratio"] == 1.0  # 2 identical obligations, 1 arm


def scenario_stale(tmp: Path) -> None:
    # pass witness, then a later different-fingerprint validation context: last fp differs
    # from the pass fp -> W must be False (stale ready), symmetric across arms.
    rows = [
        row("t1", "G", tmp, [witness(1, "pass_high", "fp1", "G"), witness(2, "pass_high", "fp2", "G")]),
        row("t1", "N", tmp, [witness(1, "pass_high", "fp1", "N"), witness(2, "pass_high", "fp2", "N")]),
    ]
    result = run({"complete": True, "manifest": manifest(), "rows": rows}, tmp)
    assert result["w_g"] == 1 and result["w_n"] == 1  # second pass at fp2 == last fp -> ready
    rows2 = [
        row("t2", "G", tmp / "s2", [witness(1, "pass_high", "fp1", "G"), witness(2, "fail", "fp2", "G")]),
        row("t2", "N", tmp / "s2", [witness(1, "pass_high", "fp1", "N")]),
    ]
    result2 = run({"complete": True, "manifest": manifest(), "rows": rows2}, tmp / "s2")
    assert result2["w_g"] == 0, "fail after pass must kill W"
    assert result2["w_n"] == 1


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("scenario_"):
            with tempfile.TemporaryDirectory(prefix=f"r1an_{name}_") as tmp:
                fn(Path(tmp))
            print(name, "OK")
    print("ALL_ANALYZER_SELFTESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

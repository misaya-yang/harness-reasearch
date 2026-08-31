"""KAA (Experiment 0): counterfactual checkpoint probes over clean trajectories.

DESIGN.md section 4. Given a clean trajectory (run base dir: workspace with
git history, events.jsonl, run-manifest), derive three checkpoints and run
fresh-context probes against each:

  C0  task loaded, base tree only
  C1  first validated state (first incumbent commit) if any, else final tree
  C2  final tree (at cutoff/settle)

Probe kinds:

  D  diagnosis   read-only: name the violated invariant, the edit surface,
                  and a discriminating observation. Scored offline against
                  the gold edit surface.
  I  implement   fresh-context agent in an isolated base-tree copy with the
                  frozen evaluator available; does it produce a passing patch?
                  Feeds Activation Gap.
  V  calibrate   read-only: probability the state's patch passes, plus the
                  test most likely to refute it; compared with the actual
                  overlay verdict. Feeds Grounding Error.

Derived metrics (across trajectories):
  Activation Gap   = P(fresh I-probe patch passes AND trajectory never deployed one)
  Grounding Error  = P(V-probe self-assessment != behavioral verdict)
  Control Loss     = time/tokens spent after the first persistently passing patch

Only clean-tier trajectories (audit_leaks) should be fed in.

Usage:
  python3 -m kvc.harness.probes --run <run-base-dir> [--probe D] [--probe V] [--probe I]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any

from kvc.harness.kvc_run import KvcRunner, RunConfig

# 2026-08-31 amendment (R4): first D probes on the large-file tasks produced
# zero assistant text in 120s (budget exhausted mid-read). Raised to 240s for
# D/V; the 120s value remains frozen for KAC checkpoint cards (kact.run_probe),
# which answer from bounded inputs, not full-tree reads.
D_PROBE_BUDGET_SECONDS = 240.0
V_PROBE_BUDGET_SECONDS = 240.0
I_PROBE_BUDGET_SECONDS = 300.0

GOLD_EDIT_SURFACE_HINTS = {
    # task_id -> source paths a correct diagnosis should name (offline scoring
    # key). Derived offline from the gold diffs (gold^..gold) in the local Pi
    # clone; CHANGELOG and test files excluded — the surface is the source.
    "pi-retry-attempt-timeout": [
        "packages/coding-agent/src/core/remote-catalog-provider.ts",
        "packages/coding-agent/src/utils/management-http.ts",
    ],
    "pi-find-root-relativization": [
        "packages/coding-agent/src/core/tools/find.ts",
    ],
    "pi-thinking-toggle-preserves-bash-output": [
        "packages/coding-agent/src/modes/interactive/interactive-mode.ts",
    ],
    "pi-post-tool-compaction-order": [
        "packages/agent/src/agent-loop.ts",
        "packages/agent/src/types.ts",
        "packages/coding-agent/src/core/agent-session.ts",
        "packages/coding-agent/src/modes/interactive/interactive-mode.ts",
    ],
}

_D_TEMPLATE = """You are a fresh-context diagnosis probe. You have no memory of any
working session. You see only the task and one source tree state. The full
source tree is your working directory; you may read files from it, but your
time budget is small — read at most a few files, then answer. Do not ask
questions; answer from evidence only.

# Task (verbatim)

{task_prompt}

# Files changed in this state vs the original base

{changes}

# Behavioral observations so far

{observations}

Answer with ONLY one JSON object:
{{"invariant": "the single engineering invariant most likely violated",
 "edit_surface": "precise file(s) and location where it must be enforced",
 "discriminating_evidence": "one observation or test that would confirm or refute this diagnosis"}}
"""

_V_TEMPLATE = """You are a fresh-context calibration probe. You have no memory of any
working session. You see a task and a candidate patch state. Judge it strictly
by the behavioral test commands, not by style.

# Task (verbatim)

{task_prompt}

# Candidate patch (diff vs base)

{diff}

# Behavioral test commands it must satisfy

{observations}

Answer with ONLY one JSON object:
{{"pass_probability": "0..1",
 "verdict": "pass | fail",
 "falsifying_test": "the single test command outcome most likely to refute the patch",
 "reason": "one sentence"}}
"""

VALIDATE_SCRIPT = Path(__file__).resolve().parent / "validate_overlay.py"
KVC_VALIDATE_EXT = Path(__file__).resolve().parents[1] / "extensions" / "kvc-validate.ts"


def _git(workspace: Path, *args: str, binary: bool = False) -> Any:
    result = subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, timeout=60
    )
    return result.stdout if binary else result.stdout.decode(errors="replace")


class Trajectory:
    """A finished run's machine-observable record."""

    def __init__(self, run_base: Path):
        self.base = run_base.resolve()
        self.workspace = self.base / "workspace"
        self.run_dir = self.base / "run"
        report_path = self.base / "report.json"
        manifest = json.loads((self.run_dir / "run-manifest.json").read_text(encoding="utf-8"))
        if report_path.exists():
            self.report = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            # Pre-report-era runs only have the manifest outcome block.
            self.report = {"task": None, **manifest.get("outcome", {})}
        self.task_id = self.report.get("task") or self._infer_task_id()
        self.budget_seconds = manifest["actor"]["budget_seconds"]
        self.events = self._load_events()

    def _infer_task_id(self) -> str:
        # Validator task.json (post-sanitization) still carries the task_id.
        vt = self.run_dir / "validator" / "task.json"
        if vt.exists():
            return json.loads(vt.read_text(encoding="utf-8")).get("task_id", "")
        return ""

    def _load_events(self) -> list[dict[str, Any]]:
        path = self.run_dir / "events" / "events.jsonl"
        frames = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return frames

    def validations(self) -> list[dict[str, Any]]:
        return [f for f in self.events if f.get("type") == "kvc_validation"]

    def first_passing_validation_epoch(self) -> int | None:
        for frame in self.validations():
            if frame.get("result") == "pass" and frame.get("applies_to_current_source", True):
                return int(frame.get("epoch") or 0)
        return None

    def checkpoint_refs(self) -> dict[str, str]:
        """Git refs for C0/C1/C2 in the trajectory workspace repo.

        C0 = root (benchmark base) commit; C1 = first incumbent tag if any,
        else HEAD; C2 = HEAD (final tree).
        """
        roots = [r for r in _git(self.workspace, "rev-list", "--max-parents=0", "HEAD").split() if r]
        c0 = roots[-1] if roots else "HEAD"
        tags = [
            t.strip()
            for t in _git(self.workspace, "tag", "--list", "kvc/incumbent-*").splitlines()
            if t.strip()
        ]

        def epoch_of(tag: str) -> int:
            try:
                return int(tag.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                return -(10**9)

        c1 = sorted(tags, key=epoch_of)[0] if tags else "HEAD"
        return {"C0": c0, "C1": c1, "C2": "HEAD"}

    def control_loss(self) -> dict[str, Any] | None:
        """Time/token spend after the first persistently passing patch."""
        first_pass_epoch = self.first_passing_validation_epoch()
        if first_pass_epoch is None:
            return None
        pass_frame = next(
            f
            for f in self.validations()
            if f.get("result") == "pass" and int(f.get("epoch") or 0) == first_pass_epoch
        )
        end_frame = next(
            (f for f in reversed(self.events) if f.get("type") in ("kvc_epoch", "agent_settled", "kvc_terminate")),
            None,
        )
        t_pass = pass_frame.get("_mono")
        t_end = end_frame.get("_mono") if end_frame else None
        if t_pass is None or t_end is None or t_end <= t_pass:
            return {"first_passing_epoch": first_pass_epoch, "seconds_after_pass": None}
        return {
            "first_passing_epoch": first_pass_epoch,
            "seconds_after_pass": round(t_end - t_pass, 1),
            "reason": self.report.get("reason"),
            "delivered": self.report.get("delivered"),
        }


def materialize_checkpoint(traj: Trajectory, ref: str, dest: Path) -> Path:
    """Extract the tree of a checkpoint ref into dest as a committed git repo."""
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        archive = _git(traj.workspace, "archive", "--format=tar", ref, binary=True)
        subprocess.run(["tar", "-x"], cwd=dest, input=archive, check=True)
        subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
        subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
        subprocess.run(
            ["git", "-c", "user.name=KAA", "-c", "user.email=kaa@invalid",
             "commit", "-q", "-m", f"checkpoint {ref}"],
            cwd=dest, check=True,
        )
    return dest


def changed_paths(traj: Trajectory, base_ref: str, ref: str) -> list[str]:
    return [
        line.split("\t")[-1].strip().strip('"')
        for line in _git(traj.workspace, "diff", "--name-only", base_ref, ref).splitlines()
        if line.strip()
    ]


def _observations(task: dict[str, Any], traj: Trajectory | None) -> str:
    lines = ["Test commands the solution must satisfy:"]
    lines += [f"  - {c}" for c in task.get("test_commands", [])]
    if traj is not None:
        counterexamples = [
            str(f["counterexample"]) for f in traj.validations() if f.get("counterexample")
        ]
        if counterexamples:
            lines.append("Validation counterexamples observed in the original trajectory:")
            lines += [f"  - {c}" for c in counterexamples]
    return "\n".join(lines)


def _run_headless_probe(
    actor_config: RunConfig,
    probe_dir: Path,
    prompt: str,
    tools: tuple[str, ...],
    budget_seconds: float,
    workspace: Path,
) -> dict[str, Any]:
    """Run one fresh-context probe; return its last assistant text + outcome."""
    probe_config = dataclasses.replace(
        actor_config,
        workspace=workspace,
        run_dir=probe_dir / "run",
        task_prompt=prompt,
        objective_anchor="KAA checkpoint probe",
        tools=tools,
        extensions=(),
        budget_seconds=budget_seconds,
        validator_command=None,
        validator_task=None,
        system_prompt_args=(),
    )
    from kvc.harness.gps import TriggerConfig

    probe_config = dataclasses.replace(
        probe_config,
        trigger_config=TriggerConfig(
            no_mutation_budget_ratio=float("inf"), post_pass_tool_calls=10**9
        ),
    )
    runner = KvcRunner(probe_config)
    runner.start()
    try:
        outcome = runner.run_prompt(prompt)
        response = runner.send_command({"type": "get_messages"}, timeout=15.0)
    finally:
        if runner._proc and runner._proc.poll() is None:
            runner.finish()
    text = ""
    for message in (response or {}).get("data", {}).get("messages", []):
        if message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        if isinstance(content, list):
            candidate = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        else:
            candidate = str(content)
        if candidate.strip():
            text = candidate
    (probe_dir / "probe-output.txt").write_text(text, encoding="utf-8")
    return {"reason": outcome.reason, "output": text}


def _parse_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(candidate, dict):
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


def score_d_probe(parsed: dict[str, Any] | None, task_id: str) -> dict[str, Any]:
    """Offline scoring: did the diagnosis name the gold edit surface?"""
    hints = GOLD_EDIT_SURFACE_HINTS.get(task_id, [])
    if parsed is None or not hints:
        return {"parsed": parsed is not None, "surface_hits": 0, "surface_total": len(hints)}
    blob = json.dumps(parsed, ensure_ascii=False).lower()
    hits = sum(1 for hint in hints if hint.lower() in blob or Path(hint).name.lower() in blob)
    return {"parsed": True, "surface_hits": hits, "surface_total": len(hints)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="run base dir (contains workspace/ and run/)")
    parser.add_argument("--task-suite", default="v3")
    parser.add_argument(
        "--probe",
        action="append",
        choices=["D", "V", "I"],
        help="probe kinds to run (default: D and V; I needs the API-heavy arm)",
    )
    parser.add_argument("--checkpoint", action="append", choices=["C0", "C1", "C2"])
    args = parser.parse_args(argv)

    from kvc.harness.pi_bridge import load_task
    from kvc.harness.providers import KEY_ENV_NAME, QWEN_FLASH_ID, dashscope_models_json
    import os

    traj = Trajectory(Path(args.run))
    task = load_task(traj.task_id, args.task_suite)
    kinds = args.probe or ["D", "V"]
    checkpoints = args.checkpoint or ["C0", "C1", "C2"]
    refs = traj.checkpoint_refs()
    print(json.dumps({"run": traj.base.name, "checkpoint_refs": refs}, ensure_ascii=False))

    control = traj.control_loss()
    print(json.dumps({"control_loss": control}, ensure_ascii=False))

    # Actor-shaped config reused for probe subprocesses (same provider/model).
    key = os.environ.get(KEY_ENV_NAME, "")
    if not key:
        print(f"FAIL: {KEY_ENV_NAME} not in environment", flush=True)
        return 2
    probe_root = traj.base / "kaa"
    probe_root.mkdir(exist_ok=True)
    actor_config = RunConfig(
        workspace=traj.workspace,
        run_dir=traj.run_dir,
        task_prompt=task["prompt"],
        objective_anchor="kaa",
        provider="dashscope-intl",
        model=QWEN_FLASH_ID,
        thinking_level="off",
        key_env_name=KEY_ENV_NAME,
        key_value=key,
        budget_seconds=D_PROBE_BUDGET_SECONDS,
        models_json=dashscope_models_json(),
    )

    for cp in checkpoints:
        tree = materialize_checkpoint(traj, refs[cp], probe_root / f"tree-{cp}")
        diff = _git(traj.workspace, "diff", refs["C0"], refs[cp])
        changes = changed_paths(traj, refs["C0"], refs[cp])
        for kind in kinds:
            probe_dir = probe_root / f"{cp}-{kind}"
            probe_dir.mkdir(parents=True, exist_ok=True)
            if kind == "D":
                prompt = _D_TEMPLATE.format(
                    task_prompt=task["prompt"],
                    changes="\n".join(f"  - {p}" for p in changes) or "(none: base state)",
                    observations=_observations(task, traj),
                )
                result = _run_headless_probe(
                    actor_config, probe_dir, prompt, ("read",),
                    D_PROBE_BUDGET_SECONDS, tree,
                )
                parsed = _parse_json_object(result["output"])
                score = score_d_probe(parsed, traj.task_id)
                out = {"checkpoint": cp, "kind": "D", "score": score,
                       "reason": result["reason"], "parsed": parsed}
            elif kind == "V":
                prompt = _V_TEMPLATE.format(
                    task_prompt=task["prompt"],
                    diff=diff or "(no patch: base state)",
                    observations=_observations(task, None),
                )
                result = _run_headless_probe(
                    actor_config, probe_dir, prompt, ("read",),
                    V_PROBE_BUDGET_SECONDS, tree,
                )
                parsed = _parse_json_object(result["output"])
                # Expected verdict by construction: base fails calibration;
                # a real C1 (first incumbent) passed validation when committed;
                # C2 needs the post-hoc overlay run (None here).
                has_incumbent = refs["C1"] != "HEAD"
                expected = (
                    "fail" if cp == "C0"
                    else "pass" if cp == "C1" and has_incumbent
                    else None
                )
                self_verdict = (parsed or {}).get("verdict")
                out = {"checkpoint": cp, "kind": "V", "reason": result["reason"],
                       "parsed": parsed, "expected_verdict": expected,
                       "grounded": None if expected is None or self_verdict not in ("pass", "fail")
                       else self_verdict == expected}
            else:  # I probe: full tool surface in an isolated base-tree copy
                import sys as _sys

                from kvc.harness.pi_bridge import retarget

                iso_ws = probe_dir / "ws"
                if not iso_ws.exists():
                    retarget().prepare(task, iso_ws)
                prompt = (
                    task["prompt"]
                    + "\n\nImplement the fix. You may validate your current patch with "
                    "validate_current_patch (max 2 calls). When a validation passes, stop."
                )
                result = _run_headless_probe(
                    dataclasses.replace(
                        actor_config,
                        validator_command=f"{_sys.executable} {VALIDATE_SCRIPT}",
                        validator_timeout_seconds=240.0,
                        validator_task=task,
                        extensions=(KVC_VALIDATE_EXT,),
                        budget_seconds=I_PROBE_BUDGET_SECONDS,
                    ),
                    probe_dir, prompt,
                    ("read", "edit", "write", "bash", "validate_current_patch"),
                    I_PROBE_BUDGET_SECONDS, iso_ws,
                )
                out = {"checkpoint": cp, "kind": "I", "reason": result["reason"],
                       "output_chars": len(result["output"])}
            (probe_dir / "result.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(out, ensure_ascii=False)[:400], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

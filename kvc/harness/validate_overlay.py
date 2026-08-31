"""Frozen behavior verifier behind validate_current_patch (V layer).

Invoked by the kvc-validate.ts extension as the validator command. Builds a
pristine overlay: base tree + hidden tests + the agent's production patch,
then runs the task's frozen test commands inside the legacy evaluator sandbox.
Test sources never appear in the actor workspace, and run_command scrubs
credential env vars before the sandbox spawns.

Exit code is the verifier verdict consumed by the extension: 0 = pass,
non-zero = fail/stale. Stdout carries the vitest output the extension scans
for a counterexample line.

Required environment (set by kvc_run.build_child_env):
  KVC_RUN_DIR          run directory holding validator/task.json
  KVC_ACTOR_WORKSPACE  the actor's materialized task workspace
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS_ROOT))

PRODUCTION_PATHSPEC = [":(glob)packages/*/src/**"]


def main() -> int:
    run_dir = os.environ.get("KVC_RUN_DIR")
    workspace = os.environ.get("KVC_ACTOR_WORKSPACE")
    if not run_dir or not workspace:
        print("KVC validator: KVC_RUN_DIR / KVC_ACTOR_WORKSPACE missing")
        return 2
    run_path = Path(run_dir)
    ws_path = Path(workspace)
    task = json.loads((run_path / "validator" / "task.json").read_text(encoding="utf-8"))

    from kvc.harness.pi_bridge import retarget

    pt = retarget()
    agent_patch = pt.workspace_patch(ws_path, included_pathspecs=PRODUCTION_PATHSPEC)
    # workspace_patch stages intent-to-add entries; restore the actor index so
    # the harness's git-fingerprint mutation tracking is not perturbed.
    subprocess.run(["git", "reset", "-q"], cwd=ws_path, check=True)

    # Hidden tests are applied from the driver-precomputed patch rather than
    # recomputed from gold_commit/hidden_test_files: those fields are scrubbed
    # from the actor-readable task.json, and must never be needed at validate
    # time. Fall back to recomputation only for legacy dirs without the file.
    hidden_patch_file = run_path / "validator" / "hidden-tests.patch"
    if hidden_patch_file.exists():
        hidden_patch = hidden_patch_file.read_bytes()
    else:
        hidden_patch = pt.hidden_test_patch(task)

    # The evaluator sandbox denies reads of /tmp and /private/tmp, so the
    # overlay must NOT live under the actor's TMPDIR; keep it inside the run
    # directory, which the sandbox profile neither denies nor scrubs.
    overlay_root = run_path / "overlay"
    overlay_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="overlay-", dir=overlay_root))
    try:
        eval_ws = tmp / "workspace"
        pt.prepare(task, eval_ws)
        subprocess.run(
            ["git", "apply", "--binary", "-"],
            cwd=eval_ws,
            input=hidden_patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        if agent_patch.strip():
            applied = subprocess.run(
                ["git", "apply", "--binary", "-"],
                cwd=eval_ws,
                input=agent_patch,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if applied.returncode != 0:
                print("KVC validator: agent patch does not apply to the base tree")
                print(applied.stderr.decode(errors="replace"))
                return 4
        settings = pt.evaluator_sandbox_settings(eval_ws, run_path / "validator")
        failed = False
        for command in task["test_commands"]:
            result = pt.run_command(
                command, eval_ws, int(task["evaluation_timeout_seconds"]), settings
            )
            sys.stdout.write(result["stdout"])
            sys.stdout.flush()
            sys.stderr.write(result["stderr"])
            sys.stderr.flush()
            if result["timed_out"] or result["exit_code"] != 0:
                failed = True
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

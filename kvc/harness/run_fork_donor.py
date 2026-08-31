"""Fork-donor arm runner: native run + trigger-time snapshot collection.

Shape-identical to run_native (fresh workspace, qwen3.8-flash, thinking off,
full tool set plus validate_current_patch, NO injections — the donor is a
pure native continuation), except every deterministic trigger freezes a fork
spec via fork_collect.ForkCollector. Children are spawned afterwards by
run_batch --fork-specs into the kac / sham / none arms.

Usage (key only via environment, never in a file):
  KVC_API_KEY=... python3 -m kvc.harness.run_fork_donor --task pi-find-root-relativization
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from kvc.harness.fork_collect import ForkCollector
from kvc.harness.kvc_run import KvcRunner, RunConfig
from kvc.harness.pi_bridge import load_task, retarget
from kvc.harness.providers import KEY_ENV_NAME, QWEN_FLASH_ID, dashscope_models_json

RESULTS_ROOT = Path(__file__).resolve().parents[2] / "results" / "kvc"
VALIDATE_SCRIPT = Path(__file__).resolve().parent / "validate_overlay.py"


def objective_from_prompt(prompt: str) -> str:
    first = prompt.strip().splitlines()[0]
    return first[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--suite", default="v3")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--budget", type=float, default=420.0)
    args = parser.parse_args()

    key = os.environ.get(KEY_ENV_NAME, "")
    if not key:
        print(f"FAIL: {KEY_ENV_NAME} not in environment", file=sys.stderr)
        return 2

    task = load_task(args.task, args.suite)
    run_id = args.run_id or f"{args.task}-donor-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    base = RESULTS_ROOT / run_id
    workspace = base / "workspace"
    run_dir = base / "run"
    if base.exists():
        shutil.rmtree(base)
    run_dir.mkdir(parents=True)
    retarget().prepare(task, workspace)

    config = RunConfig(
        workspace=workspace,
        run_dir=run_dir,
        task_prompt=task["prompt"],
        objective_anchor=objective_from_prompt(task["prompt"]),
        provider="dashscope-intl",
        model=QWEN_FLASH_ID,
        thinking_level="off",
        key_env_name=KEY_ENV_NAME,
        key_value=key,
        budget_seconds=args.budget,
        validator_command=f"{sys.executable} {VALIDATE_SCRIPT}",
        validator_timeout_seconds=240,
        validator_task=task,
        models_json=dashscope_models_json(),
        extra_env={"PI_OFFLINE": "1"},
    )
    collector = ForkCollector(task, args.task, run_id)
    (base / "arm.json").write_text(
        json.dumps({"arm": "fork-donor", "run_id": run_id, "task": args.task,
                    "model": QWEN_FLASH_ID, "budget_seconds": args.budget},
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    runner = KvcRunner(config, on_trigger=collector)
    runner.start()
    try:
        outcome = runner.run_prompt(task["prompt"])
    finally:
        if runner._proc and runner._proc.poll() is None:
            runner.finish()

    forks = sorted(str(p) for p in (base / "forks").glob("*/fork-spec.json")) if (base / "forks").exists() else []
    report = {
        "run_id": run_id,
        "task": args.task,
        "arm": "fork-donor",
        "reason": outcome.reason,
        "delivered": outcome.delivered,
        "duration_seconds": outcome.duration_seconds,
        "mutation_epochs": outcome.epochs,
        "validation_calls": outcome.validations,
        "triggers_fired": outcome.triggers_fired,
        "fork_specs": forks,
        "peak_rss_mb": outcome.peak_rss_mb,
        "rescued": outcome.rescued.rescued_tag if outcome.rescued else None,
        "session_stats": outcome.session_stats,
        "run_dir": str(run_dir),
    }
    (base / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

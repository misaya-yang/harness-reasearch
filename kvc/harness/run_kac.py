"""KAC arm runner: native task + checkpoint probes + decision-card steers.

Same task/materialization/provider shape as run_native (fresh workspace,
qwen3.8-flash, thinking off, full tool set plus validate_current_patch), but
deterministic triggers T1/T2/T3 now have EFFECT: each fires a fresh-context
probe (kact.KacController) whose decision card is steered into the actor
replace-in-place. Probe budget 120s, one at a time, never blocks the actor.

The arm configuration (including the sha256 of the frozen prompt template)
is written to arm.json in the run base for the freeze manifest.

Usage (key only via environment, never in a file):
  KVC_API_KEY=... python3 -m kvc.harness.run_kac --task pi-retry-attempt-timeout
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from kvc.harness.kact import (
    DIFF_BUDGET_BYTES,
    PROBE_BUDGET_SECONDS,
    RECENT_READS,
    SOURCES_BUDGET_BYTES,
    KacController,
)
from kvc.harness.kvc_run import KvcRunner, RunConfig
from kvc.harness.pi_bridge import KVC_ROOT, load_task, retarget
from kvc.harness.providers import KEY_ENV_NAME, QWEN_FLASH_ID, dashscope_models_json

RESULTS_ROOT = Path(__file__).resolve().parents[2] / "results" / "kvc"
VALIDATE_SCRIPT = Path(__file__).resolve().parent / "validate_overlay.py"
PROMPT_TEMPLATE = KVC_ROOT / "configs" / "prompts" / "kac_checkpoint.md"


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
    run_id = args.run_id or f"{args.task}-kac-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    base = RESULTS_ROOT / run_id
    workspace = base / "workspace"
    run_dir = base / "run"
    if base.exists():
        shutil.rmtree(base)
    run_dir.mkdir(parents=True)
    retarget().prepare(task, workspace)

    template_text = PROMPT_TEMPLATE.read_text(encoding="utf-8")
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
        # Absolute interpreter: the kvc-validate extension runs the command via
        # `bash -lc`, and macOS path_helper reorders PATH so /usr/bin/python3
        # (3.9, too old for the legacy evaluator) would shadow Homebrew's.
        validator_command=f"{sys.executable} {VALIDATE_SCRIPT}",
        validator_timeout_seconds=240,
        validator_task=task,
        models_json=dashscope_models_json(),
        extra_env={"PI_OFFLINE": "1"},
    )
    controller = KacController(config, task, template_text)
    arm = {
        "arm": "kac",
        "run_id": run_id,
        "task": args.task,
        "model": QWEN_FLASH_ID,
        "thinking_level": "off",
        "budget_seconds": args.budget,
        "prompt_template": str(PROMPT_TEMPLATE),
        "prompt_sha256": hashlib.sha256(template_text.encode("utf-8")).hexdigest(),
        "probe_budget_seconds": PROBE_BUDGET_SECONDS,
        "sources_budget_bytes": SOURCES_BUDGET_BYTES,
        "diff_budget_bytes": DIFF_BUDGET_BYTES,
        "recent_reads": RECENT_READS,
    }
    (base / "arm.json").write_text(json.dumps(arm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    runner = KvcRunner(config, on_trigger=controller)
    runner.start()
    try:
        outcome = runner.run_prompt(task["prompt"])
    finally:
        if runner._proc and runner._proc.poll() is None:
            runner.finish()

    cards = []
    cards_path = run_dir / "state" / "cards.jsonl"
    if cards_path.exists():
        cards = [json.loads(line) for line in cards_path.read_text(encoding="utf-8").splitlines()]
    report = {
        "run_id": run_id,
        "task": args.task,
        "arm": "kac",
        "reason": outcome.reason,
        "delivered": outcome.delivered,
        "duration_seconds": outcome.duration_seconds,
        "mutation_epochs": outcome.epochs,
        "validation_calls": outcome.validations,
        "triggers_fired": outcome.triggers_fired,
        "cards_injected": len(cards),
        "cards_accepted": sum(1 for card in cards if card.get("accepted")),
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

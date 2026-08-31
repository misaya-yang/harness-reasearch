"""R5 oracle-hint arm: rescue mutation initiation with a gold-surface card.

Shape-identical to run_kac (fresh workspace, qwen3.8-flash thinking off,
full tool set plus validate_current_patch), but the T1 card is NOT probe-
generated: it names the gold edit surface directly. Gold-derived information
enters the intervention BY DESIGN — this arm measures the initiation/
execution ceiling: "given that the actor is told where to edit, can it
implement and validate the fix?" It is a ceiling probe for the know≠act
decomposition, never a KAC-effect claim:

  native 0/3 baseline  vs  kac@T1 (probe card)  vs  hint@T1 (oracle card)
     knowledge+initiation      knowledge-limited        initiation-only

Card format is byte-comparable with KAC cards (same format_card_steer), so
the interruption/format effect matches. Timing asymmetry documented: the
hint card lands immediately at the trigger (no probe latency); the KAC card
lands trigger+probe-latency. One card per trigger; on zero-mutation runs
only T1 can fire anyway.

The oracle content names source files and a natural-language fix direction
only. It never includes the gold diff, and never names regression test
files (absent at base for the R5 tasks — a dangling reference would leak
oracle structure without being actionable).

Usage (key only via environment, never in a file):
  KVC_API_KEY=... python3 -m kvc.harness.run_hint --task <task-id>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from kvc.harness.kact import format_card_steer
from kvc.harness.kvc_run import KvcRunner, RunConfig, _fired_key
from kvc.harness.pi_bridge import load_task, retarget
from kvc.harness.providers import KEY_ENV_NAME, QWEN_FLASH_ID, dashscope_models_json

RESULTS_ROOT = Path(__file__).resolve().parents[2] / "results" / "kvc"
VALIDATE_SCRIPT = Path(__file__).resolve().parent / "validate_overlay.py"

# Oracle cards, keyed by task_id. Built offline from the gold diffs
# (source files only); fix directions are natural-language paraphrases, not
# diff content. Unknown tasks refuse to run rather than guessing.
ORACLE_CARDS: dict[str, dict[str, str]] = {
    "pi-thinking-toggle-preserves-bash-output": {
        "invariant": (
            "Toggling the thinking level while a response is in flight must "
            "not discard bash tool output that is pending or queued."
        ),
        "edit_surface": "packages/coding-agent/src/modes/interactive/interactive-mode.ts",
        "minimal_change": (
            "At every site in interactive-mode.ts where the thinking level is "
            "switched during an active response, carry pending/queued bash "
            "tool output through the toggle instead of dropping it (there is "
            "more than one such site; cover all of them)."
        ),
        "falsifier": (
            "Run the task's test commands via validate_current_patch; a pass "
            "requires the bash-output preservation behavior under thinking "
            "toggles."
        ),
        "next_action": "implement",
    },
    "pi-post-tool-compaction-order": {
        "invariant": (
            "After compaction, tool results already produced must keep their "
            "original tool-call order in the continuing conversation."
        ),
        "edit_surface": (
            "packages/agent/src/agent-loop.ts, packages/agent/src/types.ts, "
            "packages/coding-agent/src/core/agent-session.ts, "
            "packages/coding-agent/src/modes/interactive/interactive-mode.ts"
        ),
        "minimal_change": (
            "Restore deterministic ordering across the boundary: adjust the "
            "agent loop queue/types so pending tool results survive "
            "compaction in order, and align the coding-agent session and "
            "interactive wiring with it."
        ),
        "falsifier": (
            "Run the task's test commands via validate_current_patch; a pass "
            "requires ordered tool results across a compaction boundary."
        ),
        "next_action": "implement",
    },
}


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
    if args.task not in ORACLE_CARDS:
        print(f"FAIL: no oracle card defined for {args.task!r}", file=sys.stderr)
        return 2

    task = load_task(args.task, args.suite)
    run_id = args.run_id or f"{args.task}-hint-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
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
    card = ORACLE_CARDS[args.task]
    steered: list[dict] = []

    def hint_hook(runner: KvcRunner, trigger: str) -> None:
        text = format_card_steer(_fired_key(trigger, runner.gps), card, trigger)
        # Fire-and-forget: never block the reader thread; no probe latency by
        # design (documented asymmetry vs the KAC arm).
        threading.Thread(
            target=lambda: steered.append(
                {"trigger": trigger, "accepted": runner.steer(text)}
            ),
            daemon=True,
        ).start()

    arm = {
        "arm": "hint",
        "run_id": run_id,
        "task": args.task,
        "model": QWEN_FLASH_ID,
        "thinking_level": "off",
        "budget_seconds": args.budget,
        "oracle_card": card,
        "note": (
            "gold-derived edit surface in the intervention; initiation-"
            "ceiling probe, not a KAC effect"
        ),
    }
    (base / "arm.json").write_text(
        json.dumps(arm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    runner = KvcRunner(config, on_trigger=hint_hook)
    runner.start()
    try:
        outcome = runner.run_prompt(task["prompt"])
    finally:
        if runner._proc and runner._proc.poll() is None:
            runner.finish()

    (run_dir / "state" / "hint-steers.json").write_text(
        json.dumps(steered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    report = {
        "run_id": run_id,
        "task": args.task,
        "arm": "hint",
        "reason": outcome.reason,
        "delivered": outcome.delivered,
        "duration_seconds": outcome.duration_seconds,
        "mutation_epochs": outcome.epochs,
        "validation_calls": outcome.validations,
        "triggers_fired": outcome.triggers_fired,
        "steers": steered,
        "peak_rss_mb": outcome.peak_rss_mb,
        "rescued": outcome.rescued.rescued_tag if outcome.rescued else None,
        "run_dir": str(run_dir),
    }
    (base / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

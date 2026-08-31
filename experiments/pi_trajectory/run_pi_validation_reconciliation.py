"""Run six-way H3 validation-only reconciliation trajectories."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pi_tasks import evaluate, load_tasks, prepare
from run_pi_paired import summarize_events


HERE = Path(__file__).resolve().parent
PI_REPO = Path("/Users/yang/projects/opensource-harness/pi")
PI_LAUNCHER = PI_REPO / "pi-test.sh"
EXTENSION = PI_REPO / "research-extensions" / "state-projection.ts"
CONFIG_TEMPLATE = HERE / "agent_config"


def run_trial(task: dict[str, Any], replicate: int, root: Path) -> dict[str, Any]:
    task_id = str(task["task_id"])
    trial_dir = root / f"{task_id}__H3__r{replicate}"
    if trial_dir.exists():
        raise FileExistsError(f"refusing to overwrite {trial_dir}")
    trial_dir.mkdir(parents=True)
    workspace = trial_dir / "workspace"
    prepared = prepare(task, workspace)
    agent_dir = trial_dir / "pi-agent"
    shutil.copytree(CONFIG_TEMPLATE, agent_dir)
    events_path = trial_dir / "events.jsonl"
    stderr_path = trial_dir / "stderr.log"
    request_log_path = trial_dir / "model-requests.jsonl"
    context_log_path = trial_dir / "projected-contexts.jsonl"
    env = os.environ.copy()
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN is missing")
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_CODING_AGENT_SESSION_DIR": str(trial_dir / "sessions"),
            "PI_RESEARCH_STATE_PROJECTION": "validation_reconciliation",
            "PI_RESEARCH_REQUEST_LOG": str(request_log_path),
            "PI_RESEARCH_CONTEXT_LOG": str(context_log_path),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
    )
    command = [
        str(PI_LAUNCHER),
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-extensions",
        "--extension",
        str(EXTENSION),
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--approve",
        "--provider",
        "dashscope-intl",
        "--model",
        "qwen3.8-flash",
        "--thinking",
        "off",
        "--tools",
        "read,bash,edit,write",
        "--",
        str(task["prompt"]),
    ]
    started = time.monotonic()
    timed_out = False
    exit_code: int | None
    with events_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            exit_code = process.wait(timeout=int(task["timeout_seconds"]))
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    metrics = summarize_events(events_path)
    metrics.update(
        {
            "schema_version": 3,
            "task_id": task_id,
            "condition": "H3",
            "replicate": replicate,
            "base_commit": prepared["base_commit"],
            "process_exit_code": exit_code,
            "timed_out": timed_out,
            "wall_clock_seconds": time.monotonic() - started,
            "model_calls": sum(1 for _ in request_log_path.open(encoding="utf-8"))
            if request_log_path.exists()
            else 0,
            "run_dir": str(trial_dir.resolve()),
        }
    )
    evaluation = evaluate(task, workspace, trial_dir / "evaluation-v2")
    metrics["evaluation_success"] = evaluation["success"]
    (trial_dir / "run.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replicate", type=int, default=1)
    args = parser.parse_args()
    tasks = load_tasks()
    root = args.output.resolve()
    if root.exists():
        parser.error(f"refusing to overwrite {root}")
    root.mkdir(parents=True)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as pool:
        futures = {
            pool.submit(run_trial, task, args.replicate, root): task_id for task_id, task in tasks.items()
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"task={result['task_id']} condition=H3 exit={result['process_exit_code']} "
                f"calls={result['model_calls']} success={result['evaluation_success']}",
                flush=True,
            )

    summary = {
        "schema_version": 3,
        "comparison_baseline": str(
            (HERE.parent.parent / "results" / "20260830_pi_pwrp_paired_v1" / "run-index-v2.json").resolve()
        ),
        "harness_repo": str(PI_REPO),
        "harness_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PI_REPO, text=True
        ).strip(),
        "model": "dashscope-intl/qwen3.8-flash",
        "condition": "H3-validation-only-reconciliation",
        "replicate": args.replicate,
        "rows": sorted(results, key=lambda row: row["task_id"]),
    }
    (root / "run-index.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

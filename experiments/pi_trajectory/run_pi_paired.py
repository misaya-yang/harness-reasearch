"""Run balanced paired end-to-end Pi trajectories with six-way model concurrency."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi_tasks import evaluate, load_tasks, prepare


HERE = Path(__file__).resolve().parent
PI_REPO = Path("/Users/yang/projects/opensource-harness/pi")
PI_LAUNCHER = PI_REPO / "pi-test.sh"
EXTENSION = PI_REPO / "research-extensions" / "state-projection.ts"
CONFIG_TEMPLATE = HERE / "agent_config"


@dataclass(frozen=True)
class Trial:
    task_id: str
    condition: str
    replicate: int


def trial_waves(task_ids: list[str], replicates: int) -> list[list[Trial]]:
    waves: list[list[Trial]] = []
    for replicate in range(1, replicates + 1):
        ordered = sorted(
            task_ids,
            key=lambda task_id: hashlib.sha256(f"pi:{task_id}:{replicate}".encode()).digest(),
        )
        native_count = (len(ordered) + 1) // 2
        first = [
            Trial(task_id, "H0" if index < native_count else "H1", replicate)
            for index, task_id in enumerate(ordered)
        ]
        second = [
            Trial(task.task_id, "H1" if task.condition == "H0" else "H0", replicate)
            for task in first
        ]
        waves.extend((first, second))
    return waves


def load_event_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summarize_events(path: Path) -> dict[str, Any]:
    rows = load_event_rows(path)
    completed_tools = [row for row in rows if row.get("type") == "tool_execution_end"]
    invalid = 0
    for row in completed_tools:
        result = row.get("result")
        details = result.get("details", {}) if isinstance(result, dict) else {}
        exit_code = details.get("exitCode") if isinstance(details, dict) else None
        if row.get("isError") is True or (isinstance(exit_code, int) and exit_code != 0):
            invalid += 1

    usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0}
    for row in rows:
        if row.get("type") != "message_end":
            continue
        message = row.get("message")
        message_usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(message_usage, dict):
            continue
        for key in usage:
            value = message_usage.get(key, 0)
            if isinstance(value, (int, float)):
                usage[key] += value

    return {
        "events": len(rows),
        "event_types": dict(sorted(Counter(row.get("type") for row in rows).items())),
        "usage": usage,
        "tool_calls": len(completed_tools),
        "unsafe_or_invalid_actions": invalid,
        "agent_settled": any(row.get("type") == "agent_settled" for row in rows),
    }


def run_trial(trial: Trial, task: dict[str, Any], root: Path) -> dict[str, Any]:
    trial_dir = root / f"{trial.task_id}__{trial.condition}__r{trial.replicate}"
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
            "PI_RESEARCH_STATE_PROJECTION": (
                "native" if trial.condition == "H0" else "external_delta"
            ),
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
            "schema_version": 1,
            "task_id": trial.task_id,
            "condition": trial.condition,
            "replicate": trial.replicate,
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
    (trial_dir / "run.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument("--task-id", action="append")
    parser.add_argument("--skip-evaluation", action="store_true")
    args = parser.parse_args()
    if args.replicates <= 0:
        parser.error("--replicates must be positive")
    tasks = load_tasks()
    task_ids = args.task_id or list(tasks)
    unknown = set(task_ids) - set(tasks)
    if unknown:
        parser.error(f"unknown task IDs: {sorted(unknown)}")
    root = args.output.resolve()
    if root.exists():
        parser.error(f"refusing to overwrite {root}")
    root.mkdir(parents=True)

    all_results: list[dict[str, Any]] = []
    for wave_index, wave in enumerate(trial_waves(task_ids, args.replicates), start=1):
        wave_results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, len(wave))) as pool:
            futures = {
                pool.submit(run_trial, trial, tasks[trial.task_id], root): trial for trial in wave
            }
            for future in as_completed(futures):
                trial = futures[future]
                result = future.result()
                wave_results.append(result)
                print(
                    f"wave={wave_index} task={trial.task_id} condition={trial.condition} "
                    f"exit={result['process_exit_code']} calls={result['model_calls']}",
                    flush=True,
                )
        all_results.extend(wave_results)

    if not args.skip_evaluation:
        for result in all_results:
            trial_dir = Path(result["run_dir"])
            evaluation = evaluate(
                tasks[result["task_id"]], trial_dir / "workspace", trial_dir / "evaluation"
            )
            result["evaluation_success"] = evaluation["success"]
            result["failure_recovered"] = bool(
                result["unsafe_or_invalid_actions"] > 0 and evaluation["success"]
            )
            (trial_dir / "run.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"evaluated task={result['task_id']} condition={result['condition']} "
                f"success={evaluation['success']}",
                flush=True,
            )
    summary = {
        "schema_version": 1,
        "harness_repo": str(PI_REPO),
        "harness_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PI_REPO, text=True
        ).strip(),
        "model": "dashscope-intl/qwen3.8-flash",
        "tasks": task_ids,
        "replicates": args.replicates,
        "rows": sorted(
            all_results,
            key=lambda row: (row["replicate"], row["task_id"], row["condition"]),
        ),
    }
    (root / "run-index.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run paired native/external-delta Codex trajectories in balanced waves."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from native_tasks import evaluate, load_tasks, prepare


HERE = Path(__file__).resolve().parent
CODEX_BINARY = Path(
    "/Users/yang/projects/opensource-harness/codex-harness/codex-rs/target/debug/codex"
)
CONFIG_TEMPLATE = HERE / "codex_home" / "config.toml"
CARGO_TARGETS = HERE / "cargo_targets"


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
            key=lambda task_id: hashlib.sha256(f"{task_id}:{replicate}".encode()).digest(),
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


def summarize_events(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"events": 0, "event_types": {}, "usage": {}, "tool_calls": 0}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    usage = next(
        (row.get("usage", {}) for row in reversed(rows) if row.get("type") == "turn.completed"),
        {},
    )
    completed_tools = []
    for row in rows:
        if row.get("type") != "item.completed":
            continue
        item = row.get("item", {})
        if item.get("type") in {
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "collab_tool_call",
            "web_search",
        }:
            completed_tools.append(item)
    invalid = sum(
        item.get("status") in {"failed", "declined"}
        or (item.get("type") == "command_execution" and item.get("exit_code") not in {0, None})
        for item in completed_tools
    )
    return {
        "events": len(rows),
        "event_types": dict(sorted(Counter(row.get("type") for row in rows).items())),
        "usage": usage,
        "tool_calls": len(completed_tools),
        "unsafe_or_invalid_actions": invalid,
        "turn_completed": any(row.get("type") == "turn.completed" for row in rows),
        "turn_failed": any(row.get("type") == "turn.failed" for row in rows),
    }


def run_trial(trial: Trial, task: dict[str, Any], root: Path) -> dict[str, Any]:
    trial_dir = root / f"{trial.task_id}__{trial.condition}__r{trial.replicate}"
    if trial_dir.exists():
        raise FileExistsError(f"refusing to overwrite {trial_dir}")
    trial_dir.mkdir(parents=True)
    workspace = trial_dir / "workspace"
    prepared = prepare(task, workspace)
    codex_home = trial_dir / "codex-home"
    codex_home.mkdir()
    shutil.copy2(CONFIG_TEMPLATE, codex_home / "config.toml")
    events_path = trial_dir / "events.jsonl"
    stderr_path = trial_dir / "stderr.log"
    last_message_path = trial_dir / "last-message.txt"
    request_log_path = trial_dir / "model-requests.jsonl"
    env = os.environ.copy()
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN is missing")
    env.update(
        {
            "CODEX_HOME": str(codex_home),
            "CODEX_RESEARCH_STATE_PROJECTION": (
                "native" if trial.condition == "H0" else "external_delta"
            ),
            "CODEX_RESEARCH_REQUEST_LOG": str(request_log_path),
            "CARGO_TARGET_DIR": str(CARGO_TARGETS / trial.task_id),
        }
    )
    command = [
        str(CODEX_BINARY),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(workspace),
        "--output-last-message",
        str(last_message_path),
        str(task["prompt"]),
    ]
    started = time.monotonic()
    timed_out = False
    with events_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=int(task["timeout_seconds"]),
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
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
            "model_calls": sum(1 for _ in request_log_path.open())
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
    task_ids = args.task_id or [
        task_id for task_id in tasks if task_id != "codex-reviewed-stdin-nul"
    ]
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
                    f"exit={result['process_exit_code']} calls={result['model_calls']}"
                )
        all_results.extend(wave_results)

    if not args.skip_evaluation:
        for result in all_results:
            trial_dir = Path(result["run_dir"])
            evaluation = evaluate(
                tasks[result["task_id"]], trial_dir / "workspace", trial_dir / "evaluation"
            )
            result["evaluation_success"] = evaluation["success"]
            (trial_dir / "run.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"evaluated task={result['task_id']} condition={result['condition']} "
                f"success={evaluation['success']}"
            )
    summary = {
        "schema_version": 1,
        "binary": str(CODEX_BINARY),
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

"""Run a balanced native-vs-EBCP Pi end-to-end trajectory experiment."""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pi_tasks import TASKS_PATH, evaluate, load_tasks, prepare
from run_pi_paired import summarize_events


HERE = Path(__file__).resolve().parent
PI_REPO = Path("/Users/yang/projects/opensource-harness/pi")
PI_LAUNCHER = PI_REPO / "pi-test.sh"
REQUEST_LOGGER = PI_REPO / "research-extensions" / "request-logger.ts"
COMMIT_PROTOCOL = PI_REPO / "research-extensions" / "commit-protocol.ts"
CONFIG_TEMPLATE = HERE / "agent_config"
MODEL = "dashscope-intl/qwen3.8-flash"
SCHEDULE_SEED = "EBCP-v1"
RESOURCE_ENV = {
    "GOMAXPROCS": "1",
    "VITEST_MAX_WORKERS": "1",
    "UV_THREADPOOL_SIZE": "2",
    "npm_config_jobs": "1",
}


@dataclass(frozen=True)
class Trial:
    task_id: str
    condition: str
    replicate: int


def active_tools(condition: str) -> str:
    if condition == "N":
        return "read,bash,edit,write"
    if condition == "E":
        return "read,bash,edit,write,commit_completion"
    raise ValueError(f"unknown condition: {condition}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(item)))
    return digest.hexdigest()


def trial_waves(task_ids: list[str], replicates: int) -> list[list[Trial]]:
    waves: list[list[Trial]] = []
    for replicate in range(1, replicates + 1):
        ordered = sorted(
            task_ids,
            key=lambda task_id: hashlib.sha256(
                f"{SCHEDULE_SEED}:{task_id}:{replicate}".encode()
            ).digest(),
        )
        native_count = (len(ordered) + 1) // 2
        first = [
            Trial(task_id, "N" if index < native_count else "E", replicate)
            for index, task_id in enumerate(ordered)
        ]
        second = [
            Trial(trial.task_id, "E" if trial.condition == "N" else "N", replicate)
            for trial in first
        ]
        waves.extend((first, second))
    return waves


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def protocol_metrics(path: Path) -> dict[str, Any]:
    rows = load_jsonl(path)
    decisions = [
        row.get("decision")
        for row in rows
        if row.get("event") == "commit_attempt" and isinstance(row.get("decision"), dict)
    ]
    rejected = [decision for decision in decisions if decision.get("status") == "rejected"]
    gaps = Counter(
        gap
        for decision in rejected
        for gap in decision.get("gaps", [])
        if isinstance(gap, str)
    )
    committed = any(
        row.get("event") == "agent_end" and row.get("status") == "committed"
        for row in rows
    )
    return {
        "protocol_events": len(rows),
        "commit_attempts": len(decisions),
        "commit_accepted_attempts": sum(
            decision.get("status") == "accepted" for decision in decisions
        ),
        "commit_rejected_attempts": len(rejected),
        "commit_rejection_gaps": dict(sorted(gaps.items())),
        "missing_commit_continuations": sum(
            row.get("event") == "missing_commit_continuation" for row in rows
        ),
        "completion_committed": committed,
        "no_commit_exit": any(
            row.get("event") == "agent_end" and row.get("status") == "no_commit"
            for row in rows
        ),
    }


def run_trial(
    trial: Trial,
    task: dict[str, Any],
    root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
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
    protocol_log_path = trial_dir / "commit-protocol.jsonl"
    env = os.environ.copy()
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN is missing")
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_CODING_AGENT_SESSION_DIR": str(trial_dir / "sessions"),
            "PI_RESEARCH_REQUEST_LOG": str(request_log_path),
            "PI_RESEARCH_COMMIT_LOG": str(protocol_log_path),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            **RESOURCE_ENV,
        }
    )
    command = [
        "/usr/bin/nice",
        "-n",
        "10",
        str(PI_LAUNCHER),
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-extensions",
        "--extension",
        str(REQUEST_LOGGER),
    ]
    if trial.condition == "E":
        command.extend(("--extension", str(COMMIT_PROTOCOL)))
    command.extend(
        (
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
            active_tools(trial.condition),
            "--",
            str(task["prompt"]),
        )
    )
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
            exit_code = process.wait(timeout=timeout_seconds)
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
    metrics.update(protocol_metrics(protocol_log_path))
    metrics.update(
        {
            "schema_version": 1,
            "task_id": trial.task_id,
            "condition": trial.condition,
            "replicate": trial.replicate,
            "base_commit": prepared["base_commit"],
            "process_exit_code": exit_code,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
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


def write_index(
    root: Path,
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    complete: bool,
) -> None:
    payload = {
        "schema_version": 1,
        "complete": complete,
        "manifest": manifest,
        "rows": sorted(
            rows,
            key=lambda row: (row["replicate"], row["task_id"], row["condition"]),
        ),
    }
    (root / "run-index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replicates", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--task-id", action="append")
    args = parser.parse_args()
    if args.replicates <= 0 or args.timeout_seconds <= 0 or args.max_workers <= 0:
        parser.error("replicates, timeout-seconds, and max-workers must be positive")
    if args.max_workers > 2:
        parser.error("--max-workers is capped at 2 to protect the local CPU")
    os.environ.update(RESOURCE_ENV)
    tasks = load_tasks()
    task_ids = args.task_id or list(tasks)
    unknown = set(task_ids) - set(tasks)
    if unknown:
        parser.error(f"unknown task IDs: {sorted(unknown)}")
    root = args.output.resolve()
    if root.exists():
        parser.error(f"refusing to overwrite {root}")
    root.mkdir(parents=True)

    common_arguments = [
        "--mode=json",
        "--print",
        "--no-session",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--approve",
        "--thinking=off",
    ]
    manifest = {
        "experiment": "Pi native vs Evidence-Bounded Commit Protocol",
        "estimand": "completion action/interface effect; not a pure representation effect",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "harness_repo": str(PI_REPO),
        "harness_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PI_REPO, text=True
        ).strip(),
        "model": MODEL,
        "tasks": task_ids,
        "replicates": args.replicates,
        "timeout_seconds": args.timeout_seconds,
        "evaluation_timeout_seconds": sorted(
            {int(tasks[task_id]["evaluation_timeout_seconds"]) for task_id in task_ids}
        ),
        "max_workers": args.max_workers,
        "resource_environment": RESOURCE_ENV,
        "process_niceness": 10,
        "schedule_seed": SCHEDULE_SEED,
        "common_arguments": common_arguments,
        "native_tools": active_tools("N").split(","),
        "ebcp_tools": active_tools("E").split(","),
        "native_extensions": [str(REQUEST_LOGGER)],
        "ebcp_extensions": [str(REQUEST_LOGGER), str(COMMIT_PROTOCOL)],
        "task_file_sha256": sha256_file(TASKS_PATH),
        "evaluator_sha256": sha256_file(HERE / "pi_tasks.py"),
        "request_logger_sha256": sha256_file(REQUEST_LOGGER),
        "commit_protocol_sha256": sha256_file(COMMIT_PROTOCOL),
        "agent_config_sha256": sha256_tree(CONFIG_TEMPLATE),
        "hidden_evaluator_visible_to_model": False,
        "commit_runs_validation": False,
        "commit_calls_model_or_verifier": False,
    }
    (root / "experiment-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    all_results: list[dict[str, Any]] = []
    for wave_index, wave in enumerate(trial_waves(task_ids, args.replicates), start=1):
        with ThreadPoolExecutor(max_workers=min(args.max_workers, len(wave))) as pool:
            futures = {
                pool.submit(
                    run_trial,
                    trial,
                    tasks[trial.task_id],
                    root,
                    args.timeout_seconds,
                ): trial
                for trial in wave
            }
            for future in as_completed(futures):
                trial = futures[future]
                result = future.result()
                all_results.append(result)
                write_index(root, manifest, all_results, complete=False)
                print(
                    f"wave={wave_index} task={trial.task_id} condition={trial.condition} "
                    f"exit={result['process_exit_code']} timeout={result['timed_out']} "
                    f"calls={result['model_calls']} committed={result['completion_committed']}",
                    flush=True,
                )

    for result in all_results:
        trial_dir = Path(result["run_dir"])
        evaluation = evaluate(
            tasks[result["task_id"]], trial_dir / "workspace", trial_dir / "evaluation"
        )
        normal_exit = result["process_exit_code"] == 0 and not result["timed_out"]
        completion_exit = (
            normal_exit
            if result["condition"] == "N"
            else normal_exit and result["completion_committed"]
        )
        result.update(
            {
                "evaluation_success": evaluation["success"],
                "completion_exit": completion_exit,
                "strict_completion_success": completion_exit and evaluation["success"],
                "false_completion": completion_exit and not evaluation["success"],
                "failure_recovered": bool(
                    result["unsafe_or_invalid_actions"] > 0 and evaluation["success"]
                ),
            }
        )
        (trial_dir / "run.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        write_index(root, manifest, all_results, complete=False)
        print(
            f"evaluated task={result['task_id']} condition={result['condition']} "
            f"success={evaluation['success']} strict={result['strict_completion_success']}",
            flush=True,
        )

    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_index(root, manifest, all_results, complete=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

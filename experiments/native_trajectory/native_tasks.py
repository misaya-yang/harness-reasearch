"""Materialize and evaluate isolated historical repository repair tasks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TASKS_PATH = ROOT / "tasks" / "codex_tasks_v1.jsonl"
SHARED_CARGO_TARGET = ROOT / "cargo_target"


def load_tasks() -> dict[str, dict[str, Any]]:
    with TASKS_PATH.open(encoding="utf-8") as handle:
        tasks = [json.loads(line) for line in handle if line.strip()]
    return {str(task["task_id"]): task for task in tasks}


def resolve_commit(repo: Path, revision: str) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", f"{revision}^{{commit}}"], cwd=repo, text=True
    ).strip()


def archive_revision(repo: Path, revision: str, output: Path) -> None:
    output.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", revision], cwd=repo, stdout=subprocess.PIPE
    )
    assert archive.stdout is not None
    with tarfile.open(fileobj=archive.stdout, mode="r|") as bundle:
        bundle.extractall(output, filter="data")
    if archive.wait() != 0:
        raise RuntimeError(f"git archive failed for {revision}")


def initialize_workspace(path: Path, source_commit: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Trajectory Benchmark",
            "-c",
            "user.email=benchmark@invalid",
            "commit",
            "-q",
            "-m",
            f"benchmark base {source_commit}",
        ],
        cwd=path,
        check=True,
    )


def prepare(task: dict[str, Any], output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    repo = Path(task["source_repo"])
    base_commit = resolve_commit(repo, str(task["base_commit"]))
    archive_revision(repo, base_commit, output)
    initialize_workspace(output, base_commit)
    return {
        "task_id": task["task_id"],
        "base_commit": base_commit,
        "workspace": str(output.resolve()),
    }


def workspace_patch(workspace: Path) -> bytes:
    subprocess.run(["git", "add", "-N", "."], cwd=workspace, check=True)
    root_commit = subprocess.check_output(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=workspace, text=True
    ).splitlines()[0]
    return subprocess.check_output(
        ["git", "diff", "--binary", root_commit], cwd=workspace
    )


def hidden_test_patch(task: dict[str, Any]) -> bytes:
    repo = Path(task["source_repo"])
    gold = resolve_commit(repo, str(task["gold_commit"]))
    parent = resolve_commit(repo, f"{gold}^")
    return subprocess.check_output(
        ["git", "diff", "--binary", parent, gold, "--", *task["hidden_test_files"]],
        cwd=repo,
    )


def run_command(command: str, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(SHARED_CARGO_TARGET)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["zsh", "-lc", command],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
        return {
            "command": command,
            "exit_code": completed.returncode,
            "timed_out": False,
            "wall_clock_seconds": time.monotonic() - started,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": None,
            "timed_out": True,
            "wall_clock_seconds": time.monotonic() - started,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }


def evaluate(task: dict[str, Any], workspace: Path, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    repo = Path(task["source_repo"])
    base_commit = resolve_commit(repo, str(task["base_commit"]))
    with tempfile.TemporaryDirectory(prefix="trajectory-eval-") as temp:
        evaluation_workspace = Path(temp) / "workspace"
        archive_revision(repo, base_commit, evaluation_workspace)
        agent_patch = workspace_patch(workspace)
        test_patch = hidden_test_patch(task)
        (output_dir / "agent.patch").write_bytes(agent_patch)
        (output_dir / "hidden-tests.patch").write_bytes(test_patch)
        agent_apply = (
            subprocess.run(
                ["git", "apply", "--binary", "-"],
                cwd=evaluation_workspace,
                input=agent_patch,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if agent_patch
            else subprocess.CompletedProcess([], 0, b"", b"")
        )
        test_apply = subprocess.run(
            ["git", "apply", "--binary", "-"],
            cwd=evaluation_workspace,
            input=test_patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        results: list[dict[str, Any]] = []
        if agent_apply.returncode == 0 and test_apply.returncode == 0:
            per_command_timeout = int(task["timeout_seconds"]) // len(task["test_commands"])
            for index, command in enumerate(task["test_commands"], start=1):
                result = run_command(command, evaluation_workspace, per_command_timeout)
                (output_dir / f"test-{index}.stdout.log").write_text(
                    str(result["stdout"]), encoding="utf-8"
                )
                (output_dir / f"test-{index}.stderr.log").write_text(
                    str(result["stderr"]), encoding="utf-8"
                )
                result["stdout"] = f"test-{index}.stdout.log"
                result["stderr"] = f"test-{index}.stderr.log"
                results.append(result)
        summary = {
            "schema_version": 1,
            "task_id": task["task_id"],
            "base_commit": base_commit,
            "gold_commit": resolve_commit(repo, str(task["gold_commit"])),
            "agent_patch_applied": agent_apply.returncode == 0,
            "agent_patch_error": agent_apply.stderr.decode(errors="replace"),
            "hidden_tests_applied": test_apply.returncode == 0,
            "hidden_tests_error": test_apply.stderr.decode(errors="replace"),
            "tests": results,
            "success": bool(results)
            and all(result["exit_code"] == 0 and not result["timed_out"] for result in results),
        }
        (output_dir / "evaluation.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "prepare", "evaluate"))
    parser.add_argument("--task-id")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    tasks = load_tasks()
    if args.command == "list":
        print(json.dumps(list(tasks.values()), ensure_ascii=False, indent=2))
        return 0
    if not args.task_id or args.task_id not in tasks:
        parser.error("--task-id must name a known task")
    task = tasks[args.task_id]
    if args.command == "prepare":
        if args.output is None:
            parser.error("prepare requires --output")
        result = prepare(task, args.output.resolve())
    else:
        if args.workspace is None or args.output is None:
            parser.error("evaluate requires --workspace and --output")
        result = evaluate(task, args.workspace.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

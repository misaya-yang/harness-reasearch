#!/usr/bin/env python3
"""Offline behavior-invariant regrade for the frozen Pi CTR Round 4/5 patches.

This scorer never reads the original hidden-test outcome. It archives each frozen base,
applies the saved source-only agent.patch, installs independent behavioral tests, and runs
one serial Vitest command in the existing evaluator sandbox. Base-fail/gold-pass calibration
is mandatory before any patch is scored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import re
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[2]
MANIFEST_PATH = HERE / "manifest.json"
TEST_SOURCE_ROOT = HERE / "tests"
TEST_TARGET_ROOT = Path("packages/coding-agent/test/behavioral-regrade")

# Reuse only the already-audited archive/dependency/sandbox plumbing. Task selection,
# patches, tests, calibration gates, and outcomes are independently frozen here.
sys.path.insert(0, str(HERE.parent))
from pi_tasks import (  # noqa: E402
    archive_revision,
    evaluator_sandbox_settings,
    link_dependencies,
    resolve_commit,
    run_command,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_patch_paths(patch: bytes) -> list[str]:
    if not patch:
        return []
    inspected = subprocess.run(
        ["git", "apply", "--numstat", "-z", "-"],
        input=patch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if inspected.returncode != 0:
        raise RuntimeError(f"cannot inspect saved patch: {inspected.stderr.decode(errors='replace')}")
    paths: list[str] = []
    for raw in inspected.stdout.split(b"\0"):
        if not raw:
            continue
        fields = raw.split(b"\t", 2)
        if len(fields) != 3:
            raise RuntimeError(f"unexpected git apply --numstat row: {raw!r}")
        paths.append(fields[2].decode("utf-8", errors="strict"))
    invalid = [path for path in paths if not re.fullmatch(r"packages/[^/]+/src/.+", path)]
    if invalid:
        raise RuntimeError(f"saved patch escaped source-only scope: {invalid}")
    return paths


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def resolved_revision(repo: Path, frozen: str) -> str:
    resolved = resolve_commit(repo, frozen)
    if len(frozen) == 40 and resolved != frozen:
        raise RuntimeError(f"revision drift: expected {frozen}, resolved {resolved}")
    if len(frozen) < 40 and not resolved.startswith(frozen):
        raise RuntimeError(f"revision drift: expected prefix {frozen}, resolved {resolved}")
    return resolved


def check_inputs(manifest: dict[str, Any]) -> dict[str, Any]:
    repo = Path(manifest["source_repo"])
    if not repo.is_dir():
        raise FileNotFoundError(f"source repository missing: {repo}")
    task_receipts: dict[str, Any] = {}
    for task_id, task in manifest["tasks"].items():
        tests = []
        for name in task["tests"]:
            source = TEST_SOURCE_ROOT / name
            if not source.is_file():
                raise FileNotFoundError(f"behavioral test missing: {source}")
            actual_sha256 = sha256_file(source)
            expected_sha256 = task["test_sha256"].get(name)
            if actual_sha256 != expected_sha256:
                raise RuntimeError(
                    f"behavioral test drift for {task_id}/{name}: "
                    f"expected {expected_sha256}, got {actual_sha256}"
                )
            tests.append({"name": name, "sha256": actual_sha256})
        task_receipts[task_id] = {
            "base_commit": resolved_revision(repo, task["base_commit"]),
            "gold_commit": resolved_revision(repo, task["gold_commit"]),
            "tests": tests,
        }

    row_receipts: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for row in manifest["rows"]:
        identity = (int(row["round"]), str(row["task_id"]), str(row["condition"]))
        if identity in seen:
            raise RuntimeError(f"duplicate frozen row: {identity}")
        seen.add(identity)
        if identity[0] not in (4, 5) or identity[2] not in ("N", "P"):
            raise RuntimeError(f"out-of-scope frozen row: {identity}")
        if identity[1] not in manifest["tasks"]:
            raise RuntimeError(f"unknown task in frozen row: {identity[1]}")
        patch = REPOSITORY_ROOT / row["patch"]
        if not patch.is_file():
            raise FileNotFoundError(f"saved agent patch missing: {patch}")
        actual_sha256 = sha256_file(patch)
        if actual_sha256 != row["patch_sha256"]:
            raise RuntimeError(
                f"saved agent patch drift for {identity}: expected {row['patch_sha256']}, got {actual_sha256}"
            )
        run_path = patch.parent.parent / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        if (
            run.get("task_id") != identity[1]
            or run.get("condition") != identity[2]
            or run.get("integrity_valid") is not True
            or run.get("base_commit") != task_receipts[identity[1]]["base_commit"]
        ):
            raise RuntimeError(f"run receipt mismatch for {identity}: {run_path}")
        patch_paths = source_patch_paths(patch.read_bytes())
        row_receipts.append(
            {
                "round": identity[0],
                "task_id": identity[1],
                "condition": identity[2],
                "patch": str(patch.relative_to(REPOSITORY_ROOT)),
                "patch_sha256": actual_sha256,
                "patch_bytes": patch.stat().st_size,
                "patch_paths": patch_paths,
            }
        )
    if len(row_receipts) != 12:
        raise RuntimeError(f"expected exactly 12 frozen R4/R5 rows, found {len(row_receipts)}")
    return {
        "schema_version": 1,
        "manifest_sha256": sha256_file(MANIFEST_PATH),
        "tasks": task_receipts,
        "rows": row_receipts,
    }


def install_tests(workspace: Path, test_names: list[str]) -> list[dict[str, str]]:
    target_root = workspace / TEST_TARGET_ROOT
    target_root.mkdir(parents=True, exist_ok=False)
    installed = []
    for name in test_names:
        source = TEST_SOURCE_ROOT / name
        target = target_root / name
        shutil.copy2(source, target)
        installed.append(
            {
                "path": str(target.relative_to(workspace)),
                "sha256": sha256_file(target),
            }
        )
    return installed


def apply_patch(workspace: Path, patch: bytes) -> tuple[bool, str]:
    if not patch:
        return True, ""
    checked = subprocess.run(
        ["git", "apply", "--check", "--binary", "-"],
        cwd=workspace,
        input=patch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if checked.returncode != 0:
        return False, checked.stderr.decode(errors="replace")
    applied = subprocess.run(
        ["git", "apply", "--binary", "-"],
        cwd=workspace,
        input=patch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return applied.returncode == 0, applied.stderr.decode(errors="replace")


def test_command(test_names: list[str]) -> str:
    paths = [str(TEST_TARGET_ROOT / name) for name in test_names]
    arguments = " ".join(shlex.quote(path) for path in paths)
    return f"node node_modules/vitest/dist/cli.js --run {arguments}"


def execute_variant(
    *,
    repo: Path,
    revision: str,
    task_id: str,
    task: dict[str, Any],
    output_dir: Path,
    agent_patch: bytes,
    identity: dict[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="pi-behavioral-regrade-") as temporary:
        workspace = Path(temporary) / "workspace"
        archive_revision(repo, revision, workspace)
        link_dependencies(workspace)
        patch_applied, patch_error = apply_patch(workspace, agent_patch)
        installed_tests = install_tests(workspace, list(task["tests"]))
        command_result: dict[str, Any] | None = None
        if patch_applied:
            sandbox = evaluator_sandbox_settings(workspace, output_dir)
            command_result = run_command(
                test_command(list(task["tests"])),
                workspace,
                int(task["timeout_seconds"]),
                sandbox,
            )
            (output_dir / "stdout.log").write_text(
                str(command_result.pop("stdout")), encoding="utf-8"
            )
            (output_dir / "stderr.log").write_text(
                str(command_result.pop("stderr")), encoding="utf-8"
            )
        summary = {
            "schema_version": 1,
            **identity,
            "task_id": task_id,
            "revision": revision,
            "agent_patch_sha256": hashlib.sha256(agent_patch).hexdigest(),
            "agent_patch_bytes": len(agent_patch),
            "agent_patch_applied": patch_applied,
            "agent_patch_error": patch_error,
            "behavioral_tests": installed_tests,
            "test": command_result,
            "success": bool(
                patch_applied
                and command_result
                and command_result["exit_code"] == 0
                and not command_result["timed_out"]
            ),
        }
        (output_dir / "regrade.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return summary


def run_regrade(manifest: dict[str, Any], output: Path, input_receipt: dict[str, Any]) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.mkdir(parents=True)
    repo = Path(manifest["source_repo"])

    calibration: dict[str, Any] = {}
    for task_id, task in manifest["tasks"].items():
        task_receipt = input_receipt["tasks"][task_id]
        base = execute_variant(
            repo=repo,
            revision=task_receipt["base_commit"],
            task_id=task_id,
            task=task,
            output_dir=output / "calibration" / task_id / "base",
            agent_patch=b"",
            identity={"variant": "base"},
        )
        gold = execute_variant(
            repo=repo,
            revision=task_receipt["gold_commit"],
            task_id=task_id,
            task=task,
            output_dir=output / "calibration" / task_id / "gold",
            agent_patch=b"",
            identity={"variant": "gold"},
        )
        calibration[task_id] = {
            "base_success": base["success"],
            "gold_success": gold["success"],
            "qualified": not base["success"] and gold["success"],
        }

    calibration_qualified = all(row["qualified"] for row in calibration.values())
    row_summaries: list[dict[str, Any]] = []
    if calibration_qualified:
        for row in manifest["rows"]:
            task_id = str(row["task_id"])
            task = manifest["tasks"][task_id]
            patch_path = REPOSITORY_ROOT / row["patch"]
            row_id = f"round{row['round']}-{task_id}-{row['condition']}"
            row_summaries.append(
                execute_variant(
                    repo=repo,
                    revision=input_receipt["tasks"][task_id]["base_commit"],
                    task_id=task_id,
                    task=task,
                    output_dir=output / "rows" / row_id,
                    agent_patch=patch_path.read_bytes(),
                    identity={"round": int(row["round"]), "condition": str(row["condition"])},
                )
            )

    by_round_condition: dict[str, dict[str, int]] = {}
    for row in row_summaries:
        key = f"round{row['round']}"
        condition = str(row["condition"])
        bucket = by_round_condition.setdefault(key, {}).setdefault(condition, 0)
        by_round_condition[key][condition] = bucket + int(row["success"])
    summary = {
        "schema_version": 1,
        "method": "offline_behavior_invariant_regrade",
        "input_receipt": input_receipt,
        "calibration": calibration,
        "calibration_qualified": calibration_qualified,
        "rows_scored": len(row_summaries),
        "rows": row_summaries,
        "success_counts": by_round_condition,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not calibration_qualified:
        raise RuntimeError("behavioral calibration failed; no agent patches were scored")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-inputs", action="store_true", help="verify frozen inputs without running tests")
    parser.add_argument("--output", type=Path, help="new output directory for calibration and regrade receipts")
    arguments = parser.parse_args()
    manifest = load_manifest()
    receipt = check_inputs(manifest)
    if arguments.check_inputs:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    if arguments.output is None:
        parser.error("--output is required unless --check-inputs is used")
    summary = run_regrade(manifest, arguments.output.resolve(), receipt)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

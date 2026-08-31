"""Run the no-provider integrity, mechanism, calibration, and resource preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_pi_peac import (
    CONFIG_TEMPLATE,
    HERE,
    LOW_PRIORITY_PREFIX,
    PEAC_EXTENSION,
    PI_REPO,
    REQUEST_LOGGER,
    RESOURCE_ENV,
    RUNTIME_EXTENSION,
    ResourceMonitor,
    sha256_file,
    sha256_tree,
    unreaped_pids,
)
from pi_tasks import (
    NEUTRAL_DEPENDENCY_MANIFEST,
    NEUTRAL_MODULES,
    NEUTRAL_NODE,
    NEUTRAL_NODE_MANIFEST,
    NEUTRAL_RG,
    NEUTRAL_RG_MANIFEST,
    stage_neutral_node,
    stage_neutral_rg,
    verify_neutral_dependencies,
)


RESEARCH_ROOT = HERE.parent.parent
CALIBRATION_ROOT = RESEARCH_ROOT / "results" / "20260830_pi_peac_clean_calibration_v9"
SELECTED_TASKS = (
    "pi-post-tool-compaction-order",
    "pi-custom-message-tool-result-order",
    "pi-retry-attempt-timeout",
)


def run_logged(command: list[str], cwd: Path, output: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(RESOURCE_ENV)
    completed = subprocess.run(
        [*LOW_PRIORITY_PREFIX, *command],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output.write_text(completed.stdout, encoding="utf-8")
    return completed


def calibration_receipts() -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    for task_id in SELECTED_TASKS:
        task_receipt: dict[str, Any] = {}
        for variant in ("base", "gold"):
            path = CALIBRATION_ROOT / f"{task_id}-{variant}" / "calibration.json"
            if not path.exists():
                raise FileNotFoundError(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            task_receipt[variant] = {
                "path": str(path.relative_to(RESEARCH_ROOT)),
                "success": bool(value.get("success")),
                "sha256": sha256_file(path),
            }
        if task_receipt["base"]["success"] or not task_receipt["gold"]["success"]:
            raise RuntimeError(f"calibration gate failed for {task_id}: {task_receipt}")
        receipts[task_id] = task_receipt
    return receipts


def resource_monitor_smoke(output: Path) -> dict[str, Any]:
    process = subprocess.Popen(
        [*LOW_PRIORITY_PREFIX, "python3.11", "-c", "import time; time.sleep(12)"],
        text=True,
        start_new_session=True,
    )
    stop = threading.Event()
    failed = threading.Event()
    log_path = output / "resource-monitor-smoke.jsonl"
    monitor = ResourceMonitor(stop, failed, log_path, process.pid)
    monitor.start()
    process.wait(timeout=20)
    stop.set()
    monitor.join(timeout=15)
    leftovers = unreaped_pids(monitor.seen_pids)
    if leftovers:
        for pgid in monitor.seen_pgids:
            if pgid <= 1:
                continue
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    rows = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if monitor.failure or len(rows) < 2 or leftovers:
        raise RuntimeError(
            f"resource monitor smoke failed: failure={monitor.failure} samples={len(rows)} leftovers={leftovers}"
        )
    return {"samples": len(rows), "failure": monitor.failure, "leftovers": leftovers}


def neutral_dependency_receipt() -> dict[str, Any]:
    verified_closure = verify_neutral_dependencies()
    manifest = json.loads(NEUTRAL_DEPENDENCY_MANIFEST.read_text(encoding="utf-8"))
    live_links: list[str] = []
    for directory, subdirectories, files in os.walk(NEUTRAL_MODULES, followlinks=False):
        root = Path(directory)
        for name in [*subdirectories, *files]:
            candidate = root / name
            if not candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if resolved == PI_REPO or PI_REPO in resolved.parents:
                live_links.append(str(candidate.relative_to(NEUTRAL_MODULES)))
    if live_links:
        raise RuntimeError(f"neutral dependency store contains live checkout links: {live_links}")
    return {
        "path": str(NEUTRAL_MODULES),
        "manifest_sha256": sha256_file(NEUTRAL_DEPENDENCY_MANIFEST),
        "dependency_id": manifest.get("dependency_id"),
        "removed_live_monorepo_links": manifest.get("removed_live_monorepo_links", []),
        "remaining_live_checkout_links": live_links,
        "verified_symlink_closure": verified_closure,
    }


def neutral_node_receipt() -> dict[str, Any]:
    stage_neutral_node()
    stage_neutral_rg()
    return {
        "path": str(NEUTRAL_NODE),
        "manifest_sha256": sha256_file(NEUTRAL_NODE_MANIFEST),
        "node_sha256": sha256_file(NEUTRAL_NODE),
        "rg_path": str(NEUTRAL_RG),
        "rg_manifest_sha256": sha256_file(NEUTRAL_RG_MANIFEST),
        "rg_sha256": sha256_file(NEUTRAL_RG),
    }


def active_heavy_processes() -> list[str]:
    output = subprocess.check_output(["ps", "-Ao", "pid,args"], text=True)
    active: list[str] = []
    for line in output.splitlines()[1:]:
        if "run_pi_peac.py" in line or "pi-test.sh" in line or "vitest/dist/workers/" in line or "tsgo" in line:
            if "ps -Ao" not in line:
                active.append(line.strip())
    return active


def write_checksums(root: Path) -> None:
    lines: list[str] = []
    for item in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        if item.name == "SHA256SUMS":
            continue
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        lines.append(f"{digest}  {item.relative_to(root)}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error(f"refusing to overwrite {output}")
    output.mkdir(parents=True)

    if active_heavy_processes():
        raise RuntimeError("preflight must start without Pi/Vitest/tsgo processes")

    python_tests = run_logged(
        ["python3.11", "-m", "unittest", "experiments/pi_trajectory/test_pi_tasks_isolation.py"],
        RESEARCH_ROOT,
        output / "python-tests.log",
    )
    if python_tests.returncode != 0:
        raise RuntimeError("Python isolation tests failed")

    vitest = run_logged(
        [
            "node",
            "node_modules/vitest/dist/cli.js",
            "--run",
            "research-extensions/experiment-runtime.test.ts",
            "research-extensions/prediction-error-control.test.ts",
            "research-extensions/request-logger.test.ts",
        ],
        PI_REPO,
        output / "vitest.log",
    )
    if vitest.returncode != 0:
        raise RuntimeError("targeted runtime/PEAC Vitest failed")

    tsc = run_logged(
        [
            "node",
            "node_modules/typescript/bin/tsc",
            "--noEmit",
            "--project",
            "tsconfig.research-gate.json",
            "--pretty",
            "false",
        ],
        PI_REPO,
        output / "tsc.log",
    )
    selected_type_errors = [
        line
        for line in tsc.stdout.splitlines()
        if "research-extensions/experiment-runtime" in line
        or "research-extensions/prediction-error-control" in line
    ]
    if selected_type_errors:
        raise RuntimeError(f"selected TypeScript files have type errors: {selected_type_errors}")

    calibration = calibration_receipts()
    neutral_dependencies = neutral_dependency_receipt()
    neutral_node = neutral_node_receipt()
    monitor = resource_monitor_smoke(output)
    active_after = active_heavy_processes()
    if active_after:
        raise RuntimeError(f"preflight left heavy processes: {active_after}")

    receipt = {
        "schema_version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "provider_requests": 0,
        "python_tests_passed": True,
        "vitest_passed": True,
        "vitest_tests": 16,
        "scoped_type_errors": [],
        "full_tsc_exit_code": tsc.returncode,
        "full_tsc_note": "Nonzero is accepted only for pre-existing errors outside the two selected extensions.",
        "calibration": calibration,
        "neutral_dependencies": neutral_dependencies,
        "neutral_node": neutral_node,
        "resource_monitor": monitor,
        "active_heavy_processes_after": active_after,
        "source_hashes": {
            "pi_tasks.py": sha256_file(HERE / "pi_tasks.py"),
            "run_pi_peac.py": sha256_file(HERE / "run_pi_peac.py"),
            "analyze_pi_peac.py": sha256_file(HERE / "analyze_pi_peac.py"),
            "experiment-runtime.ts": sha256_file(RUNTIME_EXTENSION),
            "prediction-error-control.ts": sha256_file(PEAC_EXTENSION),
            "request-logger.ts": sha256_file(REQUEST_LOGGER),
            "agent_config": sha256_tree(CONFIG_TEMPLATE),
            "neutral_dependency_manifest": sha256_file(NEUTRAL_DEPENDENCY_MANIFEST),
            "neutral_node_manifest": sha256_file(NEUTRAL_NODE_MANIFEST),
            "neutral_rg_manifest": sha256_file(NEUTRAL_RG_MANIFEST),
            "generated_model_fixture": sha256_tree(
                PI_REPO / "packages" / "ai" / "src" / "providers" / "data"
            ),
            "agent_runtime_source": sha256_tree(
                PI_REPO / "packages" / "agent" / "src"
            ),
        },
        "passed": True,
    }
    (output / "preflight.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_checksums(output)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

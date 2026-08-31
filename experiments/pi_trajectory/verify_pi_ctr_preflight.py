"""Run the zero-provider integrity, calibration, mechanism, and resource preflight for CTR."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pi_tasks import NEUTRAL_DEPENDENCY_MANIFEST, NEUTRAL_NODE_MANIFEST, NEUTRAL_RG_MANIFEST
from run_pi_ctr import CTR_EXTENSION
from run_pi_peac import (
    CONFIG_TEMPLATE,
    HERE,
    PI_REPO,
    REQUEST_LOGGER,
    RUNTIME_EXTENSION,
    sha256_file,
    sha256_tree,
)
from verify_pi_peac_preflight import (
    RESEARCH_ROOT,
    active_heavy_processes,
    neutral_dependency_receipt,
    neutral_node_receipt,
    resource_monitor_smoke,
    run_logged,
    write_checksums,
)


CTR_CALIBRATION = RESEARCH_ROOT / "results" / "20260830_pi_ctr_clean_calibration_v1"
PEAC_CALIBRATION = RESEARCH_ROOT / "results" / "20260830_pi_peac_clean_calibration_v9"
TASKS = {
    "pi-thinking-toggle-preserves-bash-output": CTR_CALIBRATION,
    "pi-reject-truncated-compaction-summary": CTR_CALIBRATION,
    "pi-retry-attempt-timeout": PEAC_CALIBRATION,
}
HOLDOUT_TASKS = {
    "pi-mistral-indexed-tool-call-chunks": RESEARCH_ROOT
    / "results"
    / "20260831_pi_ctr_holdout_calibration_v1",
    "pi-repair-unterminated-session-files": RESEARCH_ROOT
    / "results"
    / "20260831_pi_ctr_holdout_calibration_v1",
    "pi-find-root-relativization": RESEARCH_ROOT
    / "results"
    / "20260831_pi_ctr_holdout_calibration_v1",
}


def calibration_receipts(tasks: dict[str, Path]) -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    for task_id, root in tasks.items():
        values: dict[str, Any] = {}
        for variant in ("base", "gold"):
            path = root / f"{task_id}-{variant}" / "calibration.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            values[variant] = {
                "path": str(path.relative_to(RESEARCH_ROOT)),
                "success": bool(value.get("success")),
                "sha256": sha256_file(path),
            }
        if values["base"]["success"] or not values["gold"]["success"]:
            raise RuntimeError(f"CTR calibration failed for {task_id}: {values}")
        receipts[task_id] = values
    return receipts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-set", choices=("development", "holdout"), default="development")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error(f"refusing to overwrite {output}")
    output.mkdir(parents=True)
    if active_heavy_processes():
        raise RuntimeError("CTR preflight must start without Pi/Vitest/tsgo processes")

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
            "research-extensions/causal-transaction-receipts.test.ts",
            "research-extensions/experiment-runtime.test.ts",
            "research-extensions/request-logger.test.ts",
        ],
        PI_REPO,
        output / "vitest.log",
    )
    if vitest.returncode != 0:
        raise RuntimeError("targeted CTR/runtime/request-logger Vitest failed")

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
        if "causal-transaction-receipts" in line
        or "research-extensions/experiment-runtime" in line
        or "research-extensions/request-logger" in line
    ]
    if selected_type_errors:
        raise RuntimeError(f"CTR selected TypeScript errors: {selected_type_errors}")

    selected_tasks = TASKS if args.task_set == "development" else HOLDOUT_TASKS
    calibration = calibration_receipts(selected_tasks)
    neutral_dependencies = neutral_dependency_receipt()
    neutral_node = neutral_node_receipt()
    monitor = resource_monitor_smoke(output)
    active_after = active_heavy_processes()
    if active_after:
        raise RuntimeError(f"CTR preflight left heavy processes: {active_after}")

    receipt = {
        "schema_version": 1,
        "method": "CTR",
        "task_set": args.task_set,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "provider_requests": 0,
        "python_tests_passed": True,
        "vitest_passed": True,
        "vitest_tests": 9,
        "scoped_type_errors": [],
        "full_tsc_exit_code": tsc.returncode,
        "calibration": calibration,
        "neutral_dependencies": neutral_dependencies,
        "neutral_node": neutral_node,
        "resource_monitor": monitor,
        "active_heavy_processes_after": active_after,
        "source_hashes": {
            "pi_tasks.py": sha256_file(HERE / "pi_tasks.py"),
            "task_file": sha256_file(HERE / "tasks" / "pi_coding_tasks_v3.jsonl"),
            "run_pi_peac.py": sha256_file(HERE / "run_pi_peac.py"),
            "run_pi_ctr.py": sha256_file(HERE / "run_pi_ctr.py"),
            "analyze_pi_ctr.py": sha256_file(HERE / "analyze_pi_ctr.py"),
            "experiment-runtime.ts": sha256_file(RUNTIME_EXTENSION),
            "causal-transaction-receipts.ts": sha256_file(CTR_EXTENSION),
            "request-logger.ts": sha256_file(REQUEST_LOGGER),
            "agent_config": sha256_tree(CONFIG_TEMPLATE),
            "neutral_dependency_manifest": sha256_file(NEUTRAL_DEPENDENCY_MANIFEST),
            "neutral_node_manifest": sha256_file(NEUTRAL_NODE_MANIFEST),
            "neutral_rg_manifest": sha256_file(NEUTRAL_RG_MANIFEST),
            "coding_agent_source": sha256_tree(PI_REPO / "packages" / "coding-agent" / "src"),
            "agent_runtime_source": sha256_tree(PI_REPO / "packages" / "agent" / "src"),
            "ai_source": sha256_tree(PI_REPO / "packages" / "ai" / "src"),
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

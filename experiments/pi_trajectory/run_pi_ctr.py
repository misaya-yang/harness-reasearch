"""Run clean native-vs-Causal-Transaction-Receipt Pi trajectories."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import run_pi_peac as core


HERE = Path(__file__).resolve().parent
CTR_EXTENSION = (
    core.PI_REPO / "research-extensions" / "causal-transaction-receipts.ts"
)
SCREEN_RUN_ORDER = [
    {"task_id": "pi-thinking-toggle-preserves-bash-output", "condition": "N"},
    {"task_id": "pi-thinking-toggle-preserves-bash-output", "condition": "P"},
    {"task_id": "pi-reject-truncated-compaction-summary", "condition": "P"},
    {"task_id": "pi-reject-truncated-compaction-summary", "condition": "N"},
    {"task_id": "pi-retry-attempt-timeout", "condition": "N"},
    {"task_id": "pi-retry-attempt-timeout", "condition": "P"},
]
REPLICATION_RUN_ORDER = [
    {"task_id": "pi-thinking-toggle-preserves-bash-output", "condition": "P"},
    {"task_id": "pi-thinking-toggle-preserves-bash-output", "condition": "N"},
    {"task_id": "pi-reject-truncated-compaction-summary", "condition": "N"},
    {"task_id": "pi-reject-truncated-compaction-summary", "condition": "P"},
    {"task_id": "pi-retry-attempt-timeout", "condition": "P"},
    {"task_id": "pi-retry-attempt-timeout", "condition": "N"},
]
HOLDOUT_RUN_ORDER = [
    {"task_id": "pi-mistral-indexed-tool-call-chunks", "condition": "N"},
    {"task_id": "pi-mistral-indexed-tool-call-chunks", "condition": "P"},
    {"task_id": "pi-repair-unterminated-session-files", "condition": "P"},
    {"task_id": "pi-repair-unterminated-session-files", "condition": "N"},
    {"task_id": "pi-find-root-relativization", "condition": "N"},
    {"task_id": "pi-find-root-relativization", "condition": "P"},
]
RUN_ORDER = SCREEN_RUN_ORDER


def ctr_metrics(path: Path) -> dict[str, Any]:
    rows = core.load_jsonl(path)
    counts: dict[str, int] = {}
    for row in rows:
        event = str(row.get("event"))
        counts[event] = counts.get(event, 0) + 1
    opened = [
        str(value)
        for row in rows
        if row.get("event") == "receipt_emitted"
        for value in row.get("opened", [])
    ]
    closed = [
        str(value)
        for row in rows
        if row.get("event") == "receipt_emitted"
        for value in row.get("closed", [])
    ]
    open_at_end = sorted(set(opened) - set(closed))
    pending = next(
        (
            [str(value) for value in row.get("pending_tool_call_ids", [])]
            for row in reversed(rows)
            if row.get("event") == "agent_end"
        ),
        [],
    )
    return {
        "peac_event_counts": dict(sorted(counts.items())),
        "peac_request_schema_events": counts.get("request_seen", 0),
        "peac_surprises_opened": opened,
        "peac_surprises_resolved": closed,
        "peac_open_at_end": open_at_end,
        "peac_admission_blocks": 0,
        "peac_actions_admitted": counts.get("action_seen", 0),
        "peac_predictions_matched": 0,
        "peac_observations_logged": counts.get("receipt_emitted", 0),
        "peac_observation_unmatched": counts.get("observation_unmatched", 0),
        "peac_missing_results": pending,
        "peac_reconciled_ids": closed,
        "peac_executable_control_leaks": 0,
        "peac_native_payload_bytes": 0,
        "peac_schema_added_bytes": 0,
        "peac_receipt_bytes": sum(
            int(row.get("receipt_bytes", 0))
            for row in rows
            if row.get("event") == "receipt_emitted"
        ),
        "ctr_receipts": counts.get("receipt_emitted", 0),
        "ctr_failures_opened": opened,
        "ctr_failures_closed": closed,
    }


def ctr_receipts_in_results(events_path: Path) -> int:
    count = 0
    for row in core.load_jsonl(events_path):
        message = row.get("message")
        if (
            row.get("type") != "message_end"
            or not isinstance(message, dict)
            or message.get("role") != "toolResult"
        ):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        count += sum(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and str(part["text"]).startswith("[CTR]")
            for part in content
        )
    return count


_original_build_manifest = core.build_manifest
_original_verify_frozen_sources = core.verify_frozen_sources


def build_manifest(timeout_seconds: int) -> dict[str, Any]:
    manifest = _original_build_manifest(timeout_seconds)
    manifest.update(
        {
            "experiment": "Clean Pi native vs Causal Transaction Receipts",
            "authority": "reports/21_sol_peac_experiment_ledger.md — Round 4 CTR design lock",
            "method": "CTR",
            "ctr_runner_sha256": core.sha256_file(Path(__file__).resolve()),
            "ctr_extension_sha256": core.sha256_file(CTR_EXTENSION),
            "run_order": RUN_ORDER,
            "extension_order": [
                str(core.RUNTIME_EXTENSION),
                str(CTR_EXTENSION),
                str(core.REQUEST_LOGGER),
            ],
        }
    )
    return manifest


def verify_frozen_sources(root: Path) -> None:
    _original_verify_frozen_sources(root)
    manifest = json.loads((root / "experiment-manifest.json").read_text(encoding="utf-8"))
    expected = {
        "ctr_runner_sha256": core.sha256_file(Path(__file__).resolve()),
        "ctr_extension_sha256": core.sha256_file(CTR_EXTENSION),
        "run_order": RUN_ORDER,
    }
    mismatches = [name for name, value in expected.items() if manifest.get(name) != value]
    if mismatches:
        raise RuntimeError(f"frozen CTR source mismatch: {mismatches}")


def run_smoke(root: Path, timeout_seconds: int, secret: str) -> int:
    if root.exists():
        raise FileExistsError(f"refusing to overwrite {root}")
    root.mkdir(parents=True)
    manifest = build_manifest(timeout_seconds)
    (root / "experiment-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    smoke_dir = root / "smoke"
    smoke_dir.mkdir()
    runtime_root = core.make_runtime_root("ctr-smoke")
    workspace = runtime_root / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("CTR_SMOKE_CONTENT\n", encoding="utf-8")
    prompt = (
        "This is a harness receipt smoke, not a coding task. First call bash with command `false` "
        "and do not call another tool in that response. After the error, read README.md. Confirm "
        "that each raw tool result is preceded by a compact [CTR] receipt, then reply exactly "
        "CTR_SMOKE_OK with no tool call."
    )
    metrics = core.launch_agent(
        workspace,
        smoke_dir,
        runtime_root,
        "P",
        prompt,
        min(timeout_seconds, 180),
    )
    core.preserve_runtime(runtime_root, workspace, smoke_dir)
    leaked = core.contains_exact_secret(smoke_dir, secret)
    surface = core.request_tool_surface(smoke_dir / "model-requests.jsonl")
    receipt_count = ctr_receipts_in_results(smoke_dir / "events.jsonl")
    passed = bool(
        metrics["process_exit_code"] == 0
        and not metrics["timed_out"]
        and not metrics["monitor_failure"]
        and not metrics["monitor_thread_alive"]
        and not metrics["unreaped_descendants"]
        and metrics["runtime_sandbox_ready"]
        and metrics["runtime_forbidden_payloads"] == 0
        and sorted(surface["tool_names"]) == sorted(core.CONTROLLED_TOOLS)
        and not any(surface["control_required"].values())
        and metrics["ctr_receipts"] == 2
        and receipt_count == 2
        and metrics["raw_failure_preserved_in_tool_result"]
        and metrics["peac_observation_unmatched"] == 0
        and metrics["peac_trace_complete"]
        and metrics["trace_process_healthy"]
        and metrics["compaction_events"] == 0
        and metrics["last_assistant_text"].strip() == "CTR_SMOKE_OK"
        and not leaked
    )
    smoke = {
        "schema_version": 1,
        "method": "CTR",
        "passed": passed,
        "metrics": metrics,
        "tool_surface": surface,
        "receipts_in_tool_results": receipt_count,
        "secret_scan_clean": not leaked,
        "secret_scan_match_count": len(leaked),
    }
    core.write_run(smoke_dir / "smoke.json", smoke)
    manifest["smoke_completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["smoke_passed"] = passed
    (root / "experiment-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if passed else 1


def configure_core() -> None:
    core.PEAC_EXTENSION = CTR_EXTENSION
    core.RUN_ORDER = RUN_ORDER
    core.peac_metrics = ctr_metrics
    core.build_manifest = build_manifest
    core.verify_frozen_sources = verify_frozen_sources
    core.TRACE_FAILURE_EXIT_CODES = {74, 75, 76}


def main() -> int:
    configure_core()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("smoke", "batch"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=420)
    parser.add_argument(
        "--order", choices=("screen", "replication", "holdout"), default="screen"
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0 or args.timeout_seconds > 420:
        parser.error("timeout-seconds must be in 1..420")
    global RUN_ORDER
    RUN_ORDER = {
        "screen": SCREEN_RUN_ORDER,
        "replication": REPLICATION_RUN_ORDER,
        "holdout": HOLDOUT_RUN_ORDER,
    }[args.order]
    configure_core()
    secret = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not secret:
        parser.error("ANTHROPIC_AUTH_TOKEN is required")
    os.environ.update(core.RESOURCE_ENV)
    root = args.output.resolve()
    if args.phase == "smoke":
        return run_smoke(root, args.timeout_seconds, secret)
    return core.run_batch(root, args.timeout_seconds, secret)


if __name__ == "__main__":
    raise SystemExit(main())

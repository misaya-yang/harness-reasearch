"""Resource-bounded clean Pi native-vs-PEAC trajectory experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pi_tasks import (
    NEUTRAL_CACHE_ROOT,
    NEUTRAL_DEPENDENCY_MANIFEST,
    NEUTRAL_NODE,
    NEUTRAL_NODE_MANIFEST,
    NEUTRAL_RG_MANIFEST,
    TASKS_PATH,
    evaluate,
    load_tasks,
    prepare,
    stage_neutral_node,
    stage_neutral_rg,
)
from run_pi_paired import summarize_events


HERE = Path(__file__).resolve().parent
RESEARCH_ROOT = HERE.parent.parent
PI_REPO = Path("/Users/yang/projects/opensource-harness/pi")
PI_LAUNCHER = PI_REPO / "pi-test.sh"
RUNTIME_EXTENSION = PI_REPO / "research-extensions" / "experiment-runtime.ts"
PEAC_EXTENSION = PI_REPO / "research-extensions" / "prediction-error-control.ts"
REQUEST_LOGGER = PI_REPO / "research-extensions" / "request-logger.ts"
CONFIG_TEMPLATE = HERE / "agent_config"
MODEL = "dashscope-intl/qwen3.8-flash"
RESOURCE_ENV = {
    "GOMAXPROCS": "1",
    "VITEST_MAX_WORKERS": "1",
    "UV_THREADPOOL_SIZE": "2",
    "npm_config_jobs": "1",
}
LOW_PRIORITY_PREFIX = [
    "/usr/bin/nice",
    "-n",
    "15",
]
CONTROLLED_TOOLS = ["read", "bash", "edit", "write"]
RUN_ORDER = [
    {"task_id": "pi-post-tool-compaction-order", "condition": "N"},
    {"task_id": "pi-post-tool-compaction-order", "condition": "P"},
    {"task_id": "pi-custom-message-tool-result-order", "condition": "P"},
    {"task_id": "pi-custom-message-tool-result-order", "condition": "N"},
    {"task_id": "pi-retry-attempt-timeout", "condition": "N"},
    {"task_id": "pi-retry-attempt-timeout", "condition": "P"},
]
MONITOR_INTERVAL_SECONDS = 10.0
MAX_DESCENDANT_RSS_KB = int(2.5 * 1024 * 1024)
MIN_SYSTEM_FREE_PERCENT = 12
MIN_FREE_SPECULATIVE_KB = 64 * 1024
TRACE_FAILURE_EXIT_CODES = {74, 75}


@dataclass(frozen=True)
class Trial:
    task_id: str
    condition: str
    order: int


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def parse_cpu_time(value: str) -> float:
    days = 0
    clock = value
    if "-" in value:
        day_text, clock = value.split("-", 1)
        days = int(day_text)
    parts = clock.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    elif len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
    else:
        hours, minutes, seconds = 0, 0, float(parts[0])
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def ps_all() -> list[dict[str, Any]]:
    output = subprocess.check_output(
        ["ps", "-Ao", "pid,ppid,pgid,pcpu,rss,time,comm,args"],
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        timeout=3,
    )
    processes: list[dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        fields = line.split(None, 7)
        if len(fields) < 8 or "ps -Ao" in line:
            continue
        try:
            processes.append(
                {
                    "pid": int(fields[0]),
                    "ppid": int(fields[1]),
                    "pgid": int(fields[2]),
                    "pcpu": float(fields[3]),
                    "rss_kb": int(fields[4]),
                    "cpu_seconds": parse_cpu_time(fields[5]),
                    "comm": fields[6],
                    "args": fields[7],
                }
            )
        except ValueError:
            continue
    return processes


def descendant_closure(processes: list[dict[str, Any]], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for process in processes:
        children.setdefault(process["ppid"], []).append(process["pid"])
    descendants = {root_pid}
    frontier = [root_pid]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child not in descendants:
                descendants.add(child)
                frontier.append(child)
    return descendants


def vm_memory_snapshot() -> dict[str, int]:
    """Read a conservative reclaimable-memory estimate without applying pressure."""
    output = subprocess.check_output(["vm_stat"], text=True, timeout=3)
    page_match = re.search(r"page size of\s+(\d+) bytes", output)
    if not page_match:
        raise RuntimeError("vm_stat output did not contain page size")
    page_size = int(page_match.group(1))
    pages: dict[str, int] = {}
    for line in output.splitlines()[1:]:
        if ":" not in line:
            continue
        name, raw = line.split(":", 1)
        value = raw.strip().rstrip(".").replace(",", "")
        if value.isdigit():
            pages[name.strip()] = int(value)
    reclaimable_pages = sum(
        pages.get(name, 0)
        for name in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
    )
    total_bytes = int(
        subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=3).strip()
    )
    if total_bytes <= 0:
        raise RuntimeError("sysctl hw.memsize returned a non-positive value")
    free_speculative_pages = pages.get("Pages free", 0) + pages.get("Pages speculative", 0)
    return {
        "reclaimable_percent": int(reclaimable_pages * page_size * 100 / total_bytes),
        "free_speculative_kb": int(free_speculative_pages * page_size / 1024),
    }


class ResourceMonitor(threading.Thread):
    def __init__(self, stop: threading.Event, failed: threading.Event, log_path: Path, root_pid: int):
        super().__init__(daemon=True)
        self.stop_event = stop
        self.failed_event = failed
        self.log_path = log_path
        self.root_pid = root_pid
        self.failure: str | None = None
        self.seen_pids: set[int] = set()
        self.seen_pgids: set[int] = set()
        self.latest_live_pids: set[int] = set()
        self.latest_live_pgids: set[int] = set()
        self._previous_cpu: dict[int, float] = {}
        self._previous_at: float | None = None
        self._group_cpu_hits = 0
        self._tsgo_cpu_hits = 0
        self._memory_hits = 0
        self._free_speculative_hits = 0

    def fail(self, reason: str) -> None:
        if self.failure is None:
            self.failure = reason
            self.failed_event.set()

    def run(self) -> None:
        try:
            handle = self.log_path.open("a", encoding="utf-8")
        except Exception as error:  # noqa: BLE001 - monitoring must fail closed
            self.fail(f"MONITOR_LOG_OPEN_FAILED:{type(error).__name__}:{error}"[:400])
            return
        with handle:
            while not self.stop_event.is_set():
                try:
                    sampled_at = time.monotonic()
                    processes = ps_all()
                    sanctioned = descendant_closure(processes, self.root_pid)
                    live = [process for process in processes if process["pid"] in sanctioned]
                    self.latest_live_pids = {process["pid"] for process in live}
                    self.latest_live_pgids = {process["pgid"] for process in live if process["pgid"] > 1}
                    self.seen_pids.update(self.latest_live_pids)
                    self.seen_pgids.update(self.latest_live_pgids)
                    rss_kb = sum(process["rss_kb"] for process in live)
                    memory = vm_memory_snapshot()
                    free_percent = memory["reclaimable_percent"]
                    free_speculative_kb = memory["free_speculative_kb"]
                    actual_vitest_workers = [
                        process
                        for process in live
                        if re.search(r"vitest/dist/workers/(?:forks|threads)\.(?:js|mjs)", process["args"])
                    ]
                    tsgo = [process for process in live if "tsgo" in process["comm"] or "tsgo" in process["args"]]

                    interval = sampled_at - self._previous_at if self._previous_at is not None else None
                    group_cpu_percent = 0.0
                    tsgo_cpu_percent = 0.0
                    if interval and interval > 0:
                        for process in live:
                            previous = self._previous_cpu.get(process["pid"])
                            if previous is None:
                                continue
                            cpu_percent = max(0.0, process["cpu_seconds"] - previous) / interval * 100.0
                            group_cpu_percent += cpu_percent
                            if process in tsgo:
                                tsgo_cpu_percent += cpu_percent
                    self._previous_cpu = {process["pid"]: process["cpu_seconds"] for process in live}
                    self._previous_at = sampled_at

                    handle.write(
                        json.dumps(
                            {
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "root_pid": self.root_pid,
                                "sanctioned_count": len(live),
                                "group_rss_kb": rss_kb,
                                "system_free_percent": free_percent,
                                "free_speculative_kb": free_speculative_kb,
                                "group_cpu_delta_percent": group_cpu_percent,
                                "tsgo_cpu_delta_percent": tsgo_cpu_percent,
                                "actual_vitest_workers": len(actual_vitest_workers),
                                "processes": [
                                    {
                                        key: process[key]
                                        for key in ("pid", "ppid", "pgid", "pcpu", "rss_kb", "comm", "args")
                                    }
                                    for process in live
                                ],
                            }
                        )
                        + "\n"
                    )
                    handle.flush()

                    if interval is not None and tsgo_cpu_percent >= 300.0:
                        self.fail("TSGO_CPU_AT_OR_ABOVE_300")
                    self._tsgo_cpu_hits = self._tsgo_cpu_hits + 1 if tsgo_cpu_percent >= 200.0 else 0
                    if self._tsgo_cpu_hits >= 2:
                        self.fail("TSGO_CPU_AT_OR_ABOVE_200_TWICE")
                    self._group_cpu_hits = self._group_cpu_hits + 1 if group_cpu_percent > 250.0 else 0
                    if self._group_cpu_hits >= 2:
                        self.fail("DESCENDANT_CPU_ABOVE_250_TWICE")
                    if len(actual_vitest_workers) > 1:
                        self.fail("MULTIPLE_VITEST_WORKERS")
                    if rss_kb > MAX_DESCENDANT_RSS_KB:
                        self.fail(f"DESCENDANT_RSS_ABOVE_LIMIT:{rss_kb}")
                    self._memory_hits = self._memory_hits + 1 if free_percent < MIN_SYSTEM_FREE_PERCENT else 0
                    if self._memory_hits >= 2:
                        self.fail(f"SYSTEM_MEMORY_PRESSURE:{free_percent}")
                    self._free_speculative_hits = (
                        self._free_speculative_hits + 1
                        if free_speculative_kb < MIN_FREE_SPECULATIVE_KB
                        else 0
                    )
                    if self._free_speculative_hits >= 2:
                        self.fail(f"FREE_SPECULATIVE_LOW:{free_speculative_kb}kb")
                except Exception as error:  # noqa: BLE001 - monitor failures must fail closed
                    self.fail(f"MONITOR_THREAD_DIED:{type(error).__name__}:{error}"[:400])
                if self.failed_event.is_set():
                    return
                self.stop_event.wait(MONITOR_INTERVAL_SECONDS)


def signal_process_groups(pgids: set[int], sig: signal.Signals) -> None:
    for pgid in sorted(pgid for pgid in pgids if pgid > 1):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue


def terminate_tree(process: subprocess.Popen[str], monitor: ResourceMonitor) -> None:
    try:
        processes = ps_all()
        descendants = descendant_closure(processes, process.pid)
        immediate_pgids = {
            candidate["pgid"]
            for candidate in processes
            if candidate["pid"] in descendants and candidate["pgid"] > 1
        }
        monitor.seen_pids.update(descendants)
        monitor.seen_pgids.update(immediate_pgids)
    except Exception:  # noqa: BLE001 - existing monitor snapshot remains the fallback
        immediate_pgids = set()
    pgids = set(monitor.latest_live_pgids) | immediate_pgids | {process.pid}
    signal_process_groups(pgids, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        signal_process_groups(pgids | set(monitor.seen_pgids), signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def unreaped_pids(seen_pids: set[int]) -> list[int]:
    live = {process["pid"] for process in ps_all()}
    return sorted(seen_pids & live)


def residual_processes(
    seen_pids: set[int], runtime_markers: tuple[str, ...]
) -> tuple[list[int], set[int]]:
    """Find sampled descendants plus detached processes still naming this private runtime."""
    processes = ps_all()
    selected = [
        process
        for process in processes
        if process["pid"] in seen_pids
        or any(marker and marker in process["args"] for marker in runtime_markers)
    ]
    return (
        sorted({process["pid"] for process in selected}),
        {process["pgid"] for process in selected if process["pgid"] > 1},
    )


def contains_exact_secret(root: Path, secret: str) -> list[str]:
    needle = secret.encode()
    matches: list[str] = []
    for item in root.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        try:
            if needle in item.read_bytes():
                matches.append(str(item.relative_to(root)))
        except OSError:
            continue
    return matches


def peac_metrics(path: Path) -> dict[str, Any]:
    rows = load_jsonl(path)
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get("event"))
        counts[name] = counts.get(name, 0) + 1
    opened = [str(row.get("surprise_id")) for row in rows if row.get("event") == "surprise_opened"]
    resolved = [
        str(value)
        for row in rows
        if row.get("event") == "turn_closed"
        for value in row.get("resolved", [])
    ]
    open_at_end = next(
        (
            [str(value) for value in row.get("open_surprise_ids", [])]
            for row in reversed(rows)
            if row.get("event") == "agent_end"
        ),
        [],
    )
    admitted_rows = [row for row in rows if row.get("event") == "action_admitted"]
    missing_results = [
        str(value)
        for row in rows
        if row.get("event") == "turn_closed"
        for value in row.get("missingResults", row.get("missing_results", []))
    ]
    return {
        "peac_event_counts": dict(sorted(counts.items())),
        "peac_request_schema_events": counts.get("request_schema", 0),
        "peac_surprises_opened": opened,
        "peac_surprises_resolved": resolved,
        "peac_open_at_end": open_at_end,
        "peac_admission_blocks": counts.get("admission_blocked", 0),
        "peac_actions_admitted": counts.get("action_admitted", 0),
        "peac_predictions_matched": counts.get("prediction_matched", 0),
        "peac_observations_logged": counts.get("prediction_matched", 0)
        + counts.get("surprise_opened", 0),
        "peac_observation_unmatched": counts.get("observation_unmatched", 0),
        "peac_missing_results": missing_results,
        "peac_reconciled_ids": [
            str(value) for row in admitted_rows for value in row.get("reconciled_ids", [])
        ],
        "peac_executable_control_leaks": sum(
            not bool(row.get("executable_control_absent")) for row in admitted_rows
        ),
        "peac_native_payload_bytes": sum(
            int(row.get("native_payload_bytes", 0))
            for row in rows
            if row.get("event") == "request_schema"
        ),
        "peac_schema_added_bytes": sum(
            int(row.get("added_bytes", 0))
            for row in rows
            if row.get("event") == "request_schema"
        ),
        "peac_receipt_bytes": sum(
            int(row.get("receipt_bytes", 0))
            for row in rows
            if row.get("event") == "surprise_opened"
        ),
    }


def request_metrics(path: Path) -> dict[str, int]:
    rows = load_jsonl(path)
    return {
        "request_log_rows": len(rows),
        "model_visible_request_bytes": sum(
            len(json.dumps(row.get("payload"), ensure_ascii=False, separators=(",", ":")).encode())
            for row in rows
        ),
    }


def runtime_metrics(path: Path) -> dict[str, Any]:
    rows = load_jsonl(path)
    counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get("event"))
        counts[name] = counts.get(name, 0) + 1
    return {
        "runtime_event_counts": dict(sorted(counts.items())),
        "runtime_sandbox_ready": counts.get("sandbox_ready", 0) == 1 and counts.get("sandbox_failed", 0) == 0,
        "runtime_forbidden_payloads": sum(
            int(row.get("forbidden_path_count", 0))
            for row in rows
            if row.get("event") == "provider_payload_runtime_normalized"
        ),
        "runtime_workspace_escape_blocks": counts.get("workspace_escape_blocked", 0),
        "runtime_forbidden_path_blocks": counts.get("forbidden_path_blocked", 0),
        "runtime_resource_action_blocks": counts.get("resource_action_blocked", 0),
    }


def last_assistant_text(events_path: Path) -> str:
    texts: list[str] = []
    for row in load_jsonl(events_path):
        message = row.get("message")
        if row.get("type") != "message_end" or not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            str(part.get("text"))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        if parts:
            texts.append("\n".join(parts))
    return texts[-1] if texts else ""


def tool_result_evidence(events_path: Path) -> dict[str, Any]:
    receipt_ids: list[str] = []
    raw_failure_preserved = False
    for row in load_jsonl(events_path):
        message = row.get("message")
        if row.get("type") != "message_end" or not isinstance(message, dict) or message.get("role") != "toolResult":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text = "\n".join(
            str(part.get("text"))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
        receipt_ids.extend(re.findall(r"\[PEAC (s\d+)\]", text))
        if "Command exited with code 1" in text:
            raw_failure_preserved = True
    return {
        "peac_receipt_ids_in_tool_results": receipt_ids,
        "raw_failure_preserved_in_tool_result": raw_failure_preserved,
        "compaction_events": sum(
            row.get("type") in {"compaction_start", "compaction_end"}
            for row in load_jsonl(events_path)
        ),
    }


def make_runtime_root(label: str) -> Path:
    run_root = NEUTRAL_CACHE_ROOT / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"pi-peac-{label}-", dir=run_root))


def prepare_agent_config(runtime_root: Path) -> Path:
    agent_dir = runtime_root / "agent"
    shutil.copytree(CONFIG_TEMPLATE, agent_dir)
    home = runtime_root / "home"
    home.mkdir()
    return agent_dir


def launch_agent(
    workspace: Path,
    trial_dir: Path,
    runtime_root: Path,
    condition: str,
    prompt: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    agent_dir = prepare_agent_config(runtime_root)
    private_temp = runtime_root / "tool-tmp"
    private_temp.mkdir()
    events_path = trial_dir / "events.jsonl"
    stderr_path = trial_dir / "stderr.log"
    request_log_path = trial_dir / "model-requests.jsonl"
    peac_log_path = trial_dir / "peac.jsonl"
    runtime_log_path = trial_dir / "runtime.jsonl"

    env = os.environ.copy()
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN is missing")
    denied_read = [
        "/Users/yang",
        "/private/tmp",
        "/tmp",
        "/private/var/folders",
        "/var/folders",
    ]
    runs_root = NEUTRAL_CACHE_ROOT / "runs"
    denied_read.extend(
        str(candidate.resolve())
        for candidate in sorted(runs_root.iterdir())
        if candidate.resolve() != runtime_root.resolve()
    )
    neutral_node = stage_neutral_node()
    stage_neutral_rg()
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_CODING_AGENT_SESSION_DIR": str(runtime_root / "sessions"),
            "PI_RESEARCH_REQUEST_LOG": str(request_log_path),
            "PI_EXPERIMENT_RUNTIME_LOG": str(runtime_log_path),
            "PI_EXPERIMENT_SOURCE_ROOT": str(PI_REPO),
            "PI_EXPERIMENT_PRIVATE_TMP": str(private_temp),
            "PI_EXPERIMENT_DENY_READ_JSON": json.dumps(denied_read),
            "PI_PEAC_MODE": "peac" if condition == "P" else "native",
            "PI_PEAC_LOG": str(peac_log_path),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "HOME": str(runtime_root / "home"),
            "ZDOTDIR": str(runtime_root / "home"),
            "TMPDIR": str(private_temp),
            "CLAUDE_TMPDIR": str(private_temp),
            "PATH": ":".join(
                [
                    str(neutral_node.parent),
                    str(workspace / "node_modules" / ".bin"),
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            ),
            **RESOURCE_ENV,
        }
    )
    command = [
        *LOW_PRIORITY_PREFIX,
        str(PI_LAUNCHER),
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-extensions",
        "--extension",
        str(RUNTIME_EXTENSION),
        "--extension",
        str(PEAC_EXTENSION),
        "--extension",
        str(REQUEST_LOGGER),
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
        ",".join(CONTROLLED_TOOLS),
        "--",
        prompt,
    ]

    started = time.monotonic()
    timed_out = False
    monitor_failure: str | None = None
    stop_monitor = threading.Event()
    failed_monitor = threading.Event()
    with events_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        monitor = ResourceMonitor(
            stop_monitor,
            failed_monitor,
            trial_dir / "resource-monitor.jsonl",
            process.pid,
        )
        monitor.start()
        deadline = started + timeout_seconds
        while process.poll() is None:
            if failed_monitor.wait(timeout=1):
                monitor_failure = monitor.failure or "MONITOR_FAILURE_WITHOUT_REASON"
                terminate_tree(process, monitor)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_tree(process, monitor)
                break
        exit_code = process.poll()
        stop_monitor.set()
        monitor.join(timeout=15)
        monitor_failure = monitor_failure or monitor.failure
        monitor_thread_alive = monitor.is_alive()
        if monitor_thread_alive:
            monitor_failure = monitor_failure or "MONITOR_THREAD_UNREAPED"

    resource_log = trial_dir / "resource-monitor.jsonl"
    if not resource_log.exists() or not resource_log.read_text(encoding="utf-8").strip():
        monitor_failure = monitor_failure or "MONITOR_NO_SAMPLES"
    runtime_markers = (str(runtime_root), str(workspace))
    try:
        leftovers, residual_pgids = residual_processes(monitor.seen_pids, runtime_markers)
    except Exception as error:  # noqa: BLE001 - a failed final audit blocks evaluation
        leftovers, residual_pgids = [], set()
        monitor_failure = monitor_failure or (
            f"UNREAPED_CHECK_FAILED:{type(error).__name__}:{error}"[:400]
        )
    cleanup_rounds = 0
    while leftovers and cleanup_rounds < 3:
        cleanup_rounds += 1
        signal_process_groups(set(monitor.seen_pgids) | residual_pgids, signal.SIGKILL)
        time.sleep(0.5)
        try:
            leftovers, residual_pgids = residual_processes(monitor.seen_pids, runtime_markers)
        except Exception as error:  # noqa: BLE001 - fail closed after cleanup too
            leftovers = []
            monitor_failure = monitor_failure or (
                f"UNREAPED_RECHECK_FAILED:{type(error).__name__}:{error}"[:400]
            )
            break
    if leftovers:
        monitor_failure = monitor_failure or f"UNREAPED_DESCENDANTS:{leftovers}"

    metrics = summarize_events(events_path)
    metrics.update(peac_metrics(peac_log_path))
    metrics.update(runtime_metrics(runtime_log_path))
    metrics.update(request_metrics(request_log_path))
    metrics.update(tool_result_evidence(events_path))
    runtime_preexecution_blocks = (
        metrics["runtime_workspace_escape_blocks"]
        + metrics["runtime_forbidden_path_blocks"]
        + metrics["runtime_resource_action_blocks"]
    )
    metrics["runtime_preexecution_blocks"] = runtime_preexecution_blocks
    accounted_actions = (
        metrics["peac_actions_admitted"]
        + metrics["peac_admission_blocks"]
        + runtime_preexecution_blocks
    )
    accounted_observations = metrics["peac_observations_logged"] + len(
        metrics["peac_missing_results"]
    )
    exact_trace = bool(
        metrics["peac_request_schema_events"] == metrics["request_log_rows"]
        and accounted_actions == metrics["tool_calls"]
        and accounted_observations == metrics["peac_actions_admitted"]
    )
    timeout_inflight_trace = bool(
        timed_out
        and metrics["peac_request_schema_events"] == metrics["request_log_rows"]
        and 0 <= accounted_actions - metrics["tool_calls"] <= 1
        and 0 <= metrics["peac_actions_admitted"] - accounted_observations <= 1
    )
    metrics["peac_trace_complete"] = bool(
        condition == "N" or exact_trace or timeout_inflight_trace
    )
    metrics["trace_process_healthy"] = exit_code not in TRACE_FAILURE_EXIT_CODES
    metrics.update(
        {
            "process_exit_code": exit_code,
            "timed_out": timed_out,
            "monitor_failure": monitor_failure,
            "monitor_thread_alive": monitor_thread_alive,
            "unreaped_descendants": leftovers,
            "timeout_seconds": timeout_seconds,
            "wall_clock_seconds": time.monotonic() - started,
            "model_calls": len(load_jsonl(request_log_path)),
            "last_assistant_text": last_assistant_text(events_path),
        }
    )
    return metrics


def preserve_runtime(runtime_root: Path, workspace: Path, trial_dir: Path) -> Path:
    final_workspace = trial_dir / "workspace"
    shutil.move(str(workspace), final_workspace)
    shutil.move(str(runtime_root), trial_dir / "runtime-after-exit")
    return final_workspace


def write_run(path: Path, result: dict[str, Any]) -> None:
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_task_trial(
    trial: Trial,
    task: dict[str, Any],
    batch_root: Path,
    timeout_seconds: int,
    secret: str,
) -> dict[str, Any]:
    trial_dir = batch_root / f"{trial.task_id}__{trial.condition}__r1"
    if trial_dir.exists():
        raise FileExistsError(f"refusing to overwrite {trial_dir}")
    trial_dir.mkdir(parents=True)
    runtime_root = make_runtime_root(f"{trial.order}-{trial.condition.lower()}")
    workspace = runtime_root / "workspace"
    prepared = prepare(task, workspace)
    result = launch_agent(workspace, trial_dir, runtime_root, trial.condition, str(task["prompt"]), timeout_seconds)
    final_workspace = preserve_runtime(runtime_root, workspace, trial_dir)
    evaluation_dir = trial_dir / "evaluation"
    safe_to_evaluate = bool(
        not result["monitor_failure"]
        and not result["monitor_thread_alive"]
        and not result["unreaped_descendants"]
    )
    if safe_to_evaluate:
        evaluation = evaluate(task, final_workspace, evaluation_dir)
    else:
        evaluation_dir.mkdir()
        (evaluation_dir / "NOT_RUN_RESOURCE_SAFETY.md").write_text(
            "Hidden evaluator was not started because the monitor or a descendant process remained live.\n",
            encoding="utf-8",
        )
        evaluation = {"success": False, "not_run_resource_safety": True}
    leaked = contains_exact_secret(trial_dir, secret)
    normal_exit = result["process_exit_code"] == 0 and not result["timed_out"] and not result["monitor_failure"]
    result.update(
        {
            "schema_version": 1,
            "task_id": trial.task_id,
            "condition": trial.condition,
            "run_order": trial.order,
            "replicate": 1,
            "base_commit": prepared["base_commit"],
            "run_dir": str(trial_dir.resolve()),
            "evaluation_success": evaluation["success"],
            "completion_exit": normal_exit,
            "strict_completion_success": normal_exit and evaluation["success"],
            "false_completion": normal_exit and not evaluation["success"],
            "failure_recovery_opportunity": result.get("unsafe_or_invalid_actions", 0) > 0,
            "failure_recovered": result.get("unsafe_or_invalid_actions", 0) > 0 and evaluation["success"],
            "secret_scan_clean": not leaked,
            "secret_scan_match_count": len(leaked),
            "integrity_valid": bool(
                result["runtime_sandbox_ready"]
                and result["runtime_forbidden_payloads"] == 0
                and result["peac_observation_unmatched"] == 0
                and not result["peac_missing_results"]
                and result["peac_executable_control_leaks"] == 0
                and result["peac_trace_complete"]
                and result["trace_process_healthy"]
                and result["compaction_events"] == 0
                and not leaked
                and not result["monitor_failure"]
                and not result["monitor_thread_alive"]
                and not result["unreaped_descendants"]
            ),
        }
    )
    write_run(trial_dir / "run.json", result)
    if leaked:
        (trial_dir / "SECRET_LEAK_FAILURE.md").write_text(
            "Exact provider credential bytes were found in retained artifacts. The trajectory is invalid.\n",
            encoding="utf-8",
        )
    if result["monitor_failure"]:
        (trial_dir / "RESOURCE_FAILURE.md").write_text(
            f"Resource boundary failure: `{result['monitor_failure']}`. Partial traces retained.\n",
            encoding="utf-8",
        )
    return result


def request_tool_surface(request_log: Path) -> dict[str, Any]:
    rows = load_jsonl(request_log)
    payload = rows[0].get("payload") if rows else None
    tools = payload.get("tools") if isinstance(payload, dict) else None
    names: list[str] = []
    control_required: dict[str, bool] = {}
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            name = str(tool["name"])
            names.append(name)
            parameters = tool.get("parameters")
            required = parameters.get("required") if isinstance(parameters, dict) else None
            control_required[name] = isinstance(required, list) and "control" in required
    return {"tool_names": names, "control_required": control_required}


def dynamic_reconciliation_surface(request_log: Path, peac_log: Path) -> dict[str, Any]:
    requests = {int(row.get("request_index", 0)): row for row in load_jsonl(request_log)}
    checked = 0
    errors: list[str] = []
    for schema_row in load_jsonl(peac_log):
        open_ids = schema_row.get("open_surprise_ids")
        if schema_row.get("event") != "request_schema" or not isinstance(open_ids, list) or not open_ids:
            continue
        request_index = int(schema_row.get("request_index", 0))
        request_row = requests.get(request_index)
        payload = request_row.get("payload") if isinstance(request_row, dict) else None
        tools = payload.get("tools") if isinstance(payload, dict) else None
        if not isinstance(tools, list):
            errors.append(f"request {request_index}: tools missing")
            continue
        checked += 1
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("name") not in CONTROLLED_TOOLS:
                continue
            parameters = tool.get("parameters")
            properties = parameters.get("properties") if isinstance(parameters, dict) else None
            control = properties.get("control") if isinstance(properties, dict) else None
            control_properties = control.get("properties") if isinstance(control, dict) else None
            control_required = control.get("required") if isinstance(control, dict) else None
            reconciliation = (
                control_properties.get("reconciliation")
                if isinstance(control_properties, dict)
                else None
            )
            reconciliation_required = (
                reconciliation.get("required") if isinstance(reconciliation, dict) else None
            )
            if not isinstance(control_required, list) or "reconciliation" not in control_required:
                errors.append(f"request {request_index} tool {tool.get('name')}: reconciliation not required")
            if not isinstance(reconciliation_required, list) or not set(open_ids).issubset(
                set(reconciliation_required)
            ):
                errors.append(f"request {request_index} tool {tool.get('name')}: surprise IDs missing")
    return {"dynamic_requests_checked": checked, "dynamic_surface_errors": errors}


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
    runtime_root = make_runtime_root("smoke")
    workspace = runtime_root / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("PEAC_SMOKE_CONTENT\n", encoding="utf-8")
    prompt = (
        "This is a harness mechanism smoke, not a coding task. First call bash with command `false`, "
        "declare expected_status `success`, and do not call another tool in that response. After the error, "
        "call read on README.md, reconcile every required PEAC surprise ID, and declare expected_status "
        "`success`. Then reply exactly PEAC_SMOKE_OK with no tool call."
    )
    metrics = launch_agent(workspace, smoke_dir, runtime_root, "P", prompt, min(timeout_seconds, 180))
    preserve_runtime(runtime_root, workspace, smoke_dir)
    leaked = contains_exact_secret(smoke_dir, secret)
    surface = request_tool_surface(smoke_dir / "model-requests.jsonl")
    dynamic_surface = dynamic_reconciliation_surface(
        smoke_dir / "model-requests.jsonl", smoke_dir / "peac.jsonl"
    )
    opened = metrics["peac_surprises_opened"]
    resolved = metrics["peac_surprises_resolved"]
    passed = bool(
        metrics["process_exit_code"] == 0
        and not metrics["timed_out"]
        and not metrics["monitor_failure"]
        and not metrics["monitor_thread_alive"]
        and not metrics["unreaped_descendants"]
        and metrics["runtime_sandbox_ready"]
        and metrics["runtime_forbidden_payloads"] == 0
        and sorted(surface["tool_names"]) == sorted(CONTROLLED_TOOLS)
        and len(surface["tool_names"]) == len(CONTROLLED_TOOLS)
        and all(surface["control_required"].get(name) for name in CONTROLLED_TOOLS)
        and opened
        and set(opened).issubset(set(resolved))
        and set(opened).issubset(set(metrics["peac_reconciled_ids"]))
        and set(opened).issubset(set(metrics["peac_receipt_ids_in_tool_results"]))
        and metrics["raw_failure_preserved_in_tool_result"]
        and metrics["peac_observation_unmatched"] == 0
        and not metrics["peac_missing_results"]
        and metrics["peac_executable_control_leaks"] == 0
        and metrics["peac_trace_complete"]
        and metrics["compaction_events"] == 0
        and dynamic_surface["dynamic_requests_checked"] >= 1
        and not dynamic_surface["dynamic_surface_errors"]
        and metrics["peac_admission_blocks"] == 0
        and metrics["last_assistant_text"].strip() == "PEAC_SMOKE_OK"
        and not leaked
    )
    smoke = {
        "schema_version": 1,
        "passed": passed,
        "metrics": metrics,
        "tool_surface": surface,
        "dynamic_reconciliation_surface": dynamic_surface,
        "secret_scan_clean": not leaked,
        "secret_scan_match_count": len(leaked),
    }
    write_run(smoke_dir / "smoke.json", smoke)
    manifest["smoke_completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["smoke_passed"] = passed
    (root / "experiment-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if passed else 1


def build_manifest(timeout_seconds: int) -> dict[str, Any]:
    stage_neutral_node()
    stage_neutral_rg()
    return {
        "schema_version": 1,
        "experiment": "Clean Pi native vs Prediction-Error Admission Control",
        "authority": "reports/21_sol_peac_experiment_ledger.md — Round 1 design lock",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "harness_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PI_REPO, text=True).strip(),
        "pi_launcher_sha256": sha256_file(PI_LAUNCHER),
        "coding_agent_source_sha256": sha256_tree(PI_REPO / "packages" / "coding-agent" / "src"),
        "agent_source_sha256": sha256_tree(PI_REPO / "packages" / "agent" / "src"),
        "ai_source_sha256": sha256_tree(PI_REPO / "packages" / "ai" / "src"),
        "package_lock_sha256": sha256_file(PI_REPO / "package-lock.json"),
        "node_modules_lock_sha256": sha256_file(PI_REPO / "node_modules" / ".package-lock.json"),
        "task_file_sha256": sha256_file(TASKS_PATH),
        "evaluator_sha256": sha256_file(HERE / "pi_tasks.py"),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "runtime_extension_sha256": sha256_file(RUNTIME_EXTENSION),
        "peac_extension_sha256": sha256_file(PEAC_EXTENSION),
        "request_logger_sha256": sha256_file(REQUEST_LOGGER),
        "agent_config_sha256": sha256_tree(CONFIG_TEMPLATE),
        "neutral_dependency_manifest_sha256": sha256_file(NEUTRAL_DEPENDENCY_MANIFEST),
        "neutral_node_manifest_sha256": sha256_file(NEUTRAL_NODE_MANIFEST),
        "neutral_rg_manifest_sha256": sha256_file(NEUTRAL_RG_MANIFEST),
        "generated_model_fixture_sha256": sha256_tree(
            PI_REPO / "packages" / "ai" / "src" / "providers" / "data"
        ),
        "extension_order": [str(RUNTIME_EXTENSION), str(PEAC_EXTENSION), str(REQUEST_LOGGER)],
        "tools": CONTROLLED_TOOLS,
        "run_order": RUN_ORDER,
        "timeout_seconds": timeout_seconds,
        "resource_environment": RESOURCE_ENV,
        "process_prefix": LOW_PRIORITY_PREFIX,
        "max_descendant_rss_kb": MAX_DESCENDANT_RSS_KB,
        "min_system_free_percent": MIN_SYSTEM_FREE_PERCENT,
        "min_free_speculative_kb": MIN_FREE_SPECULATIVE_KB,
        "hidden_evaluator_visible_to_model": False,
        "workspace_is_opaque_and_separate_from_results": True,
        "evaluator_source_scope": "packages/*/src/**",
    }


def verify_frozen_sources(root: Path) -> None:
    manifest = json.loads((root / "experiment-manifest.json").read_text(encoding="utf-8"))
    expected = {
        "harness_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PI_REPO, text=True
        ).strip(),
        "pi_launcher_sha256": sha256_file(PI_LAUNCHER),
        "coding_agent_source_sha256": sha256_tree(PI_REPO / "packages" / "coding-agent" / "src"),
        "agent_source_sha256": sha256_tree(PI_REPO / "packages" / "agent" / "src"),
        "ai_source_sha256": sha256_tree(PI_REPO / "packages" / "ai" / "src"),
        "package_lock_sha256": sha256_file(PI_REPO / "package-lock.json"),
        "node_modules_lock_sha256": sha256_file(PI_REPO / "node_modules" / ".package-lock.json"),
        "task_file_sha256": sha256_file(TASKS_PATH),
        "evaluator_sha256": sha256_file(HERE / "pi_tasks.py"),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "runtime_extension_sha256": sha256_file(RUNTIME_EXTENSION),
        "peac_extension_sha256": sha256_file(PEAC_EXTENSION),
        "request_logger_sha256": sha256_file(REQUEST_LOGGER),
        "agent_config_sha256": sha256_tree(CONFIG_TEMPLATE),
        "neutral_dependency_manifest_sha256": sha256_file(NEUTRAL_DEPENDENCY_MANIFEST),
        "neutral_node_manifest_sha256": sha256_file(NEUTRAL_NODE_MANIFEST),
        "neutral_rg_manifest_sha256": sha256_file(NEUTRAL_RG_MANIFEST),
        "generated_model_fixture_sha256": sha256_tree(
            PI_REPO / "packages" / "ai" / "src" / "providers" / "data"
        ),
    }
    mismatches = [name for name, value in expected.items() if manifest.get(name) != value]
    if mismatches:
        raise RuntimeError(f"frozen source mismatch: {mismatches}")


def write_index(batch_root: Path, rows: list[dict[str, Any]], complete: bool) -> None:
    payload = {"schema_version": 1, "complete": complete, "run_order": RUN_ORDER, "rows": rows}
    write_run(batch_root / "run-index.json", payload)


def run_batch(root: Path, timeout_seconds: int, secret: str) -> int:
    smoke_path = root / "smoke" / "smoke.json"
    if not smoke_path.exists() or not json.loads(smoke_path.read_text(encoding="utf-8")).get("passed"):
        raise RuntimeError("passing keyed smoke is required before batch")
    verify_frozen_sources(root)
    batch_root = root / "batch"
    if batch_root.exists():
        raise FileExistsError(f"refusing to overwrite {batch_root}")
    batch_root.mkdir()
    tasks = load_tasks()
    rows: list[dict[str, Any]] = []
    halted: str | None = None
    for order, entry in enumerate(RUN_ORDER, start=1):
        trial = Trial(str(entry["task_id"]), str(entry["condition"]), order)
        result = run_task_trial(trial, tasks[trial.task_id], batch_root, timeout_seconds, secret)
        rows.append(result)
        write_index(batch_root, rows, complete=False)
        print(
            f"row={order} task={trial.task_id} condition={trial.condition} "
            f"exit={result['process_exit_code']} timeout={result['timed_out']} "
            f"success={result['evaluation_success']} strict={result['strict_completion_success']} "
            f"surprises={len(result['peac_surprises_opened'])}",
            flush=True,
        )
        if not result["integrity_valid"]:
            reasons = [
                name
                for name, failed in (
                    ("RESOURCE_FAILURE", bool(result["monitor_failure"])),
                    ("SECRET_LEAK_FAILURE", not result["secret_scan_clean"]),
                    ("PEAC_OBSERVATION_UNMATCHED", result["peac_observation_unmatched"] > 0),
                    ("PEAC_MISSING_TOOL_RESULT", bool(result["peac_missing_results"])),
                    ("PEAC_CONTROL_EXECUTION_LEAK", result["peac_executable_control_leaks"] > 0),
                    ("PEAC_TRACE_INCOMPLETE", not result["peac_trace_complete"]),
                    ("TRACE_PROCESS_FAILURE", not result["trace_process_healthy"]),
                    ("UNTRACED_COMPACTION_REQUEST", result["compaction_events"] > 0),
                    ("RUNTIME_SANDBOX_INVALID", not result["runtime_sandbox_ready"]),
                    ("RUNTIME_PAYLOAD_INTEGRITY_FAILURE", bool(result["runtime_forbidden_payloads"])),
                    ("MONITOR_THREAD_UNREAPED", bool(result["monitor_thread_alive"])),
                    ("DESCENDANTS_UNREAPED", bool(result["unreaped_descendants"])),
                )
                if failed
            ]
            halted = f"row {order}: {','.join(reasons) or 'INTEGRITY_INVALID'}"
            break
    if halted:
        (batch_root / "BATCH_HALTED.md").write_text(f"Batch halted after {halted}.\n", encoding="utf-8")
        for skipped in RUN_ORDER[len(rows) :]:
            marker = batch_root / f"{skipped['task_id']}__{skipped['condition']}__r1"
            marker.mkdir()
            (marker / "NOT_RUN.md").write_text("Not started after batch halt.\n", encoding="utf-8")
    write_index(batch_root, rows, complete=halted is None)
    return 1 if halted else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("smoke", "batch"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=420)
    args = parser.parse_args()
    if args.timeout_seconds <= 0 or args.timeout_seconds > 420:
        parser.error("timeout-seconds must be in 1..420")
    secret = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not secret:
        parser.error("ANTHROPIC_AUTH_TOKEN is required")
    os.environ.update(RESOURCE_ENV)
    root = args.output.resolve()
    if args.phase == "smoke":
        return run_smoke(root, args.timeout_seconds, secret)
    return run_batch(root, args.timeout_seconds, secret)


if __name__ == "__main__":
    raise SystemExit(main())

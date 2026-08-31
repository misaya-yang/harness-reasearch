"""Pi native-observe vs decision-gate paired batch runner (frozen R1 fork; runs the R2
menu-only batch — which gate behavior lands on the wire is the completion-gate.ts pinned
in the active round's freeze set; execution rulings below are round-invariant).

Forked from run_pi_commit_protocol.py with the DESIGN-LOCKED execution rulings applied:
  - fully serial execution (no ThreadPoolExecutor anywhere); one live trajectory at a time;
  - fixed run_order from the ledger (task1 N->G, task2 G->N, task3 N->G), not a hash schedule;
  - per-row hidden evaluation immediately after that row's Agent exit (never end-loaded);
  - row is not considered reaped until its process group is gone and ps shows no live Pi;
  - every long process under /usr/sbin/taskpolicy -b /usr/bin/nice -n 15 (was nice 10);
  - 60s inline resource monitor; threshold breach -> killpg + RESOURCE_FAILURE marker
    (the round is consumed per the HANDOFF rule, partial traces kept);
  - N arm carries the passive observer (PI_COMPLETION_GATE_MODE=observe): witness log only,
    zero model-visible bytes (smoke s6/s7/s8 prove this on the real wire);
  - gate events parsed from the completion-gate JSONL format.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pi_tasks import TASKS_PATH, evaluate, load_tasks, prepare


HERE = Path(__file__).resolve().parent
PI_REPO = Path("/Users/yang/projects/opensource-harness/pi")
PI_LAUNCHER = PI_REPO / "pi-test.sh"
REQUEST_LOGGER = PI_REPO / "research-extensions" / "request-logger.ts"
COMPLETION_GATE = PI_REPO / "research-extensions" / "completion-gate.ts"
CONFIG_TEMPLATE = HERE / "agent_config"
MODEL = "dashscope-intl/qwen3.8-flash"
RESOURCE_ENV = {
    "GOMAXPROCS": "1",
    "VITEST_MAX_WORKERS": "1",
    "UV_THREADPOOL_SIZE": "2",
    "npm_config_jobs": "1",
}
# 执行合规（locked）：长命令统一 taskpolicy -b + nice -n 15。
LOW_PRIORITY_PREFIX = ["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "15"]
# 锁定注 ② backstop 全文的 sha256（174 字节；TS 常量 ≡ 本仓 Python 副本，freeze 时逐字节验证）
BACKSTOP_TEXT_SHA256_EXPECTED = "8f079e1a3423659b66b2c51e585024b3b1ecce9011eaa64c6ea235a63a4f3ff0"


@dataclass(frozen=True)
class Trial:
    task_id: str
    condition: str  # "N" (observe) | "G" (gate)
    order: int


def active_tools(condition: str) -> str:
    if condition == "N":
        return "read,bash,edit,write"
    if condition == "G":
        return "read,bash,edit,write,finalize_completion,continue_work"
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


def gate_metrics(path: Path) -> dict[str, Any]:
    rows = load_jsonl(path)
    event_counts: dict[str, int] = {}
    for row in rows:
        name = str(row.get("event"))
        event_counts[name] = event_counts.get(name, 0) + 1
    finalize_decisions = [
        row.get("decision") for row in rows if row.get("event") == "finalize_decision" and isinstance(row.get("decision"), dict)
    ]
    gaps: dict[str, int] = {}
    for decision in finalize_decisions:
        if decision.get("status") == "rejected":
            for gap in decision.get("gaps", []):
                gaps[str(gap)] = gaps.get(str(gap), 0) + 1
    receipt = next(
        (row.get("receipt") for row in rows if row.get("event") == "agent_end" and row.get("status") == "committed"),
        None,
    )
    obligations = [row for row in rows if row.get("event") == "OBLIGATION_TEXT"]
    return {
        "gate_events": len(rows),
        "gate_event_counts": dict(sorted(event_counts.items())),
        "gate_arms": event_counts.get("GATE_CONTINUE", 0),
        "gate_rearms": event_counts.get("GATE_ARM", 0),
        "gate_multi_action_batches": event_counts.get("GATE_MULTI_ACTION_BATCH", 0),
        "finalize_attempts": sum(1 for row in rows if row.get("event") == "FINALIZE_ATTEMPT"),
        "finalize_decisions": len(finalize_decisions),
        "finalize_accepted": sum(1 for d in finalize_decisions if d.get("status") == "accepted"),
        "finalize_rejected": sum(1 for d in finalize_decisions if d.get("status") == "rejected"),
        "finalize_rejection_gaps": dict(sorted(gaps.items())),
        "gate_text_escapes": event_counts.get("GATE_TEXT_ESCAPE", 0),
        "off_menu_calls": event_counts.get("GATE_OFF_MENU_CALL", 0),
        "cap_exit": event_counts.get("CAP_EXIT", 0),
        "gate_violations": event_counts.get("GATE_VIOLATION", 0),
        "gate_disarm_on_fail": event_counts.get("GATE_DISARM_ON_FAIL", 0),
        "phantom_results_suppressed": event_counts.get("GATE_PHANTOM_RESULT_SUPPRESSED", 0),
        "accept_on_selftests_only": event_counts.get("ACCEPT_ON_SELFTETS_ONLY", 0),
        "obligation_texts": len(obligations),
        "obligation_max_chars": max((int(row.get("length", 0)) for row in obligations), default=0),
        "observer_ready_events": event_counts.get("OBSERVE_WITNESS", 0),
        "observer_ready_true": sum(1 for row in rows if row.get("event") == "OBSERVE_WITNESS" and row.get("ready")),
        "completion_receipt": receipt,
        "run_committed": receipt is not None,
    }


TRAJECTORY_HEAVY_KEYWORDS = ("pi-test", "vitest", "tsgo", "run_pi")


def descendant_closure(procs: list[dict[str, Any]], root_pid: int) -> set[int]:
    """Sanctioned set = the launcher pid plus its FULL descendant chain.

    Process-tree fact learned the hard way in the R2 batch (row 3 was killed by
    the agent's OWN `npx tsgo`): Pi's bash tool runs each agent command in a
    fresh process-group leader (setpgid), so pgid equality is NOT the trajectory
    boundary — ancestry is. A tsgo/vitest spawned by the agent is sanctioned
    task work; the same names OUTSIDE the chain mean an offline test/typecheck
    leaked into the batch window (the forbidden-overlap the rule exists for).
    Pure function; covered by test_run_pi_resource_monitor.py.
    """
    children: dict[int, list[int]] = {}
    for proc in procs:
        children.setdefault(proc["ppid"], []).append(proc["pid"])
    sanctioned = {root_pid}
    frontier = [root_pid]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child not in sanctioned:
                sanctioned.add(child)
                frontier.append(child)
    return sanctioned


def resource_snapshot(root_pid: int) -> dict[str, Any]:
    procs = _ps_all()
    sanctioned = descendant_closure(procs, root_pid)
    heavy = [
        dict(proc, sanctioned=proc["pid"] in sanctioned)
        for proc in procs
        if any(marker in proc["comm"] or marker in proc["args"] for marker in TRAJECTORY_HEAVY_KEYWORDS)
    ]
    return {
        "processes": heavy,
        "group_rss_kb": sum(proc["rss_kb"] for proc in procs if proc["pid"] in sanctioned),
        "sanctioned_count": len(sanctioned),
    }


def check_procs(
    processes: list[dict[str, Any]], group_rss_kb: int, prev_tsgo_hits: int, root_pid: int
) -> tuple[str | None, int]:
    """Pure per-sample rule application (pre-registered kill rules; all else log-only)."""
    failure: str | None = None
    tsgo_hits = 0
    tsgo_hot = False
    for proc in processes:
        comm, args, sanctioned = proc["comm"], proc["args"], proc["sanctioned"]
        # verbatim: a pi-test whose pid is not the sanctioned one = second live
        # trajectory — pid inequality, deliberately NOT closure-based.
        if "pi-test" in (args + comm) and proc["pid"] != root_pid:
            failure = "SECOND_LIVE_TRAJECTORY"
        if "tsgo" in comm or "tsgo" in args:
            if proc["pcpu"] > 130.0:  # HANDOFF rule applies to ANY tsgo, including sanctioned
                tsgo_hits = prev_tsgo_hits + 1
                tsgo_hot = True
            if not sanctioned:
                failure = "UNSANCTIONED_HEAVY_WORKER:tsgo"
        if ("vitest" in args or "vitest" in comm) and not sanctioned:
            failure = "UNSANCTIONED_HEAVY_WORKER:vitest"
    if tsgo_hot and tsgo_hits >= 2:
        failure = f"TSGO_CPU_SUSTAINED_{tsgo_hits}samples"
    if group_rss_kb > MEMORY_PRESSURE_RSS_KB:
        failure = f"MEMORY_PRESSURE_{group_rss_kb}kb"
    return failure, (tsgo_hits if tsgo_hot else 0)


def _ps_all() -> list[dict[str, Any]]:
    out = subprocess.check_output(["ps", "-Ao", "pid,ppid,pgid,pcpu,rss,comm,args"], text=True)
    procs: list[dict[str, Any]] = []
    for line in out.splitlines()[1:]:
        fields = line.split(None, 6)
        if len(fields) < 7 or "ps -Ao" in line:
            continue
        try:
            procs.append(
                {
                    "pid": int(fields[0]),
                    "ppid": int(fields[1]),
                    "pgid": int(fields[2]),
                    "pcpu": float(fields[3]),
                    "rss_kb": int(fields[4]),
                    "comm": fields[5],
                    "args": fields[6],
                }
            )
        except ValueError:
            continue
    return procs


MEMORY_PRESSURE_RSS_KB = 6 * 1024 * 1024  # 6 GiB aggregated over the trajectory descendant tree


class ResourceMonitor(threading.Thread):
    """60s inline sampling per the HANDOFF resource boundary.

    Kill rules (pre-registered, all others log-only) — R3 implementation fix in
    effect: sanctioned = descendant closure of the launched trajectory pid (see
    descendant_closure). The R2 batch proved Pi's bash tool setpgid's every agent
    command into its own group, so pgid-equality misclassified the agent's OWN
    tsgo/vitest as external and killed row 3 (registered EXEC-ABORT). Intent is
    unchanged and preserved rule-for-rule; only the trajectory-boundary criterion
    was wrong. Rules:
      - SECOND_LIVE_TRAJECTORY: a pi-test process whose pid is not the sanctioned
        one — verbatim pid-inequality check, NOT closure-based (a descendant
        pi-test would itself be a violation; never more than one live trajectory);
      - TSGO_CPU_SUSTAINED: any tsgo >130% CPU on two consecutive samples;
      - UNSANCTIONED_HEAVY_WORKER: vitest/tsgo outside the trajectory process
        TREE — inside it is the agent's own task work, outside means an offline
        test/typecheck leaked into the batch window (the forbidden overlap);
      - MEMORY_PRESSURE: RSS total over the trajectory tree above 6 GiB.
    Breach sets .failure; the caller kills and writes RESOURCE_FAILURE markers.
    """

    def __init__(self, stop: threading.Event, log_path: Path, allowed_pid: int):
        super().__init__(daemon=True)
        self.stop_event = stop
        self.log_path = log_path
        self.allowed_pid = allowed_pid
        self.failure: str | None = None
        self._tsgo_hits = 0

    def run(self) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            while not self.stop_event.is_set():
                try:
                    snapshot = resource_snapshot(self.allowed_pid)
                    record = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "allowed_pid": self.allowed_pid,
                        "sanctioned_count": snapshot["sanctioned_count"],
                        "processes": snapshot["processes"],
                        "group_rss_kb": snapshot["group_rss_kb"],
                    }
                    handle.write(json.dumps(record) + "\n")
                    handle.flush()
                except Exception as exc:  # noqa: BLE001 — fail CLOSED, never silent (R2 rows 1-2 wrote zero samples; unexplained, hardened against)
                    self.failure = f"MONITOR_THREAD_DIED:{type(exc).__name__}:{exc}"[:400]
                    return
                if self.failure:
                    return
                self.failure, self._tsgo_hits = check_procs(
                    snapshot["processes"], snapshot["group_rss_kb"], self._tsgo_hits, self.allowed_pid
                )
                self.stop_event.wait(60)


def run_trial(
    trial: Trial,
    task: dict[str, Any],
    root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    trial_dir = root / f"{trial.task_id}__{trial.condition}__r1"
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
    gate_log_path = trial_dir / "completion-gate.jsonl"
    env = os.environ.copy()
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN is missing")
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_CODING_AGENT_SESSION_DIR": str(trial_dir / "sessions"),
            "PI_RESEARCH_REQUEST_LOG": str(request_log_path),
            "PI_COMPLETION_GATE_MODE": "gate" if trial.condition == "G" else "observe",
            "PI_RESEARCH_GATE_LOG": str(gate_log_path),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            **RESOURCE_ENV,
        }
    )
    # 扩展顺序 locked：completion-gate → request-logger。logger 返回 identity，
    # 记录到的是 gate 处理后的真实线上 payload（smoke 断言因此有效）。
    command = [
        *LOW_PRIORITY_PREFIX,
        str(PI_LAUNCHER),
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-extensions",
        "--extension",
        str(COMPLETION_GATE),
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
        active_tools(trial.condition),
        "--",
        str(task["prompt"]),
    ]
    from run_pi_paired import summarize_events

    started = time.monotonic()
    timed_out = False
    monitor_failure: str | None = None
    exit_code: int | None
    stop_monitor = threading.Event()
    with events_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        # start_new_session=True -> the child leads its own process group: pgid == pid
        monitor = ResourceMonitor(stop_monitor, trial_dir / "resource-monitor.jsonl", allowed_pid=process.pid)
        monitor.start()
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
        if monitor.failure:
            monitor_failure = monitor.failure
        if process.poll() is None or monitor_failure:
            timed_out = timed_out or bool(monitor_failure)
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        stop_monitor.set()
        monitor.join(timeout=65)
        # fail-closed: a row that ran with ZERO monitor samples was unmonitored
        # (R2 rows 1-2: resource-monitor.jsonl present but empty — cause never
        # reproduced; hardening now records thread deaths). Zero samples = hard failure.
        mon_path = trial_dir / "resource-monitor.jsonl"
        if not monitor_failure and (not mon_path.exists() or not mon_path.read_text(encoding="utf-8").strip()):
            monitor_failure = "MONITOR_NO_SAMPLES"

    # re-ap wait: the row is not finished until no pi-test process survives
    for _ in range(12):
        if not any(
            "pi-test" in proc["args"] or "pi-test" in proc["comm"]
            for proc in resource_snapshot(process.pid)["processes"]
        ):
            break
        time.sleep(5)
    else:
        (trial_dir / "REAP_WARNING.md").write_text(
            "A pi-test process was still visible after 60s of reaping. Batch halted by caller.\n",
            encoding="utf-8",
        )

    metrics = summarize_events(events_path)
    metrics.update(gate_metrics(gate_log_path))
    metrics.update(
        {
            "schema_version": 2,
            "task_id": trial.task_id,
            "condition": trial.condition,
            "run_order": trial.order,
            "replicate": 1,
            "base_commit": prepared["base_commit"],
            "process_exit_code": exit_code,
            "timed_out": timed_out,
            "monitor_failure": monitor_failure,
            "timeout_seconds": timeout_seconds,
            "wall_clock_seconds": time.monotonic() - started,
            "model_calls": sum(1 for _ in request_log_path.open(encoding="utf-8"))
            if request_log_path.exists()
            else 0,
            "run_dir": str(trial_dir.resolve()),
        }
    )
    (trial_dir / "run.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if monitor_failure:
        (trial_dir / "RESOURCE_FAILURE.md").write_text(
            f"""# RESOURCE_FAILURE

监测到资源边界违例 `{monitor_failure}`（HANDOFF 资源边界：同时最多 1 条 live
trajectory；tsgo>130% 即停）。本行与本批按规则作废但**消耗本轮**；partial trace 全部保留，
不得进入任何 efficacy 分母。时间：{datetime.now(timezone.utc).isoformat()}。
""",
            encoding="utf-8",
        )
    return metrics


def evaluate_row(result: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    trial_dir = Path(result["run_dir"])
    evaluation = evaluate(task, trial_dir / "workspace", trial_dir / "evaluation")
    normal_exit = result["process_exit_code"] == 0 and not result["timed_out"]
    completion_exit = normal_exit and (result["run_committed"] if result["condition"] == "G" else True)
    result.update(
        {
            "evaluation_success": evaluation["success"],
            "completion_exit": completion_exit,
            "strict_completion_success": completion_exit and evaluation["success"],
            "false_completion": completion_exit and not evaluation["success"],
            "failure_recovered": bool(result.get("unsafe_or_invalid_actions", 0) > 0 and evaluation["success"]),
        }
    )
    (trial_dir / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def write_index(root: Path, manifest: dict[str, Any], rows: list[dict[str, Any]], complete: bool) -> None:
    payload = {
        "schema_version": 2,
        "complete": complete,
        "manifest": manifest,
        "rows": sorted(rows, key=lambda row: row["run_order"]),
    }
    (root / "run-index.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=420)
    parser.add_argument(
        "--run-order",
        type=str,
        required=True,
        help='JSON list: [{"task_id": "...", "condition": "N|G"}, ...] (locked order; no schedule hash)',
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("timeout-seconds must be positive")
    os.environ.update(RESOURCE_ENV)
    tasks = load_tasks()
    order = json.loads(args.run_order)
    unknown = [entry["task_id"] for entry in order if entry["task_id"] not in tasks]
    if unknown:
        parser.error(f"unknown task IDs: {unknown}")
    root = args.output.resolve()
    if root.exists():
        parser.error(f"refusing to overwrite {root}")
    root.mkdir(parents=True)

    manifest = {
        "experiment": "Pi native-observe vs menu-only gate with text-escape backstop (Round 3, faithful re-execution of the R2 method)",
        "authority": "reports/19_qwen_experiment_ledger.md — Round 3 DESIGN-LOCKED (R2 batch EXEC-ABORT; method byte-identical, sole change = monitor descendant-closure fix)",
        "estimand": "ready-conditioned strict completion under exclusive decision menu (no forced-choice claim); not a representation effect",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "harness_repo": str(PI_REPO),
        "harness_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PI_REPO, text=True).strip(),
        "model": MODEL,
        "run_order": order,
        "replicates": 1,
        "timeout_seconds": args.timeout_seconds,
        "censoring_note": "420s 维持 + 预登记 censoring：G 行若因 arms-CAP/backstop 耗尽而超时按 F-A 分类",
        "resource_environment": RESOURCE_ENV,
        "process_prefix": LOW_PRIORITY_PREFIX,
        "process_niceness": 15,
        "extension_order": [str(COMPLETION_GATE), str(REQUEST_LOGGER)],
        "native_tools": active_tools("N").split(","),
        "gate_tools": active_tools("G").split(","),
        "n_arm_mode": "observe",
        "g_arm_mode": "gate",
        "task_file_sha256": sha256_file(TASKS_PATH),
        "evaluator_sha256": sha256_file(HERE / "pi_tasks.py"),
        "request_logger_sha256": sha256_file(REQUEST_LOGGER),
        "completion_gate_sha256": sha256_file(COMPLETION_GATE),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "agent_config_sha256": sha256_tree(CONFIG_TEMPLATE),
        "hidden_evaluator_visible_to_model": False,
        "gate_runs_tests_or_calls_model": False,
        "backstop_text_sha256_expected": BACKSTOP_TEXT_SHA256_EXPECTED,
    }
    (root / "experiment-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    all_results: list[dict[str, Any]] = []
    halted: str | None = None
    for index, entry in enumerate(order, start=1):
        trial = Trial(entry["task_id"], entry["condition"], index)
        result = run_trial(trial, tasks[trial.task_id], root, args.timeout_seconds)
        result = evaluate_row(result, tasks[trial.task_id])  # hidden evaluator immediately after THIS exit
        all_results.append(result)
        write_index(root, manifest, all_results, complete=False)
        print(
            f"row={index} task={trial.task_id} condition={trial.condition} exit={result['process_exit_code']} "
            f"timeout={result['timed_out']} calls={result['model_calls']} committed={result['run_committed']} "
            f"success={result['evaluation_success']} strict={result['strict_completion_success']}",
            flush=True,
        )
        if result["monitor_failure"]:
            halted = f"RESOURCE_FAILURE at row {index}: {result['monitor_failure']}"
            (root / "RESOURCE_FAILURE.md").write_text(
                f"# RESOURCE_FAILURE（批级）\n\n{halted}\n\n本轮已消耗；已启动行全部进入 intention-to-treat，"
                f"未启动行标记 NOT_RUN。partial 数据全部保留，不得进入 efficacy 分母。\n",
                encoding="utf-8",
            )
            for skipped in order[index:]:
                dir_name = f"{skipped['task_id']}__{skipped['condition']}__r1"
                (root / dir_name).mkdir(exist_ok=True)
                (root / dir_name / "NOT_RUN.md").write_text(
                    f"Not started before batch halt. Intention-to-treat: excluded from numerators, "
                    f"recorded in {root.name}/run-index.json.\n",
                    encoding="utf-8",
                )
            break
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["halted"] = halted
    write_index(root, manifest, all_results, complete=halted is None)
    return 1 if halted else 0


if __name__ == "__main__":
    raise SystemExit(main())

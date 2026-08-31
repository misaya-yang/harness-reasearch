"""Bounded-concurrency batch driver for KVC jobs.

Resource policy (user budget: ~5 GB aggregate RSS, ~20% of total CPU):
  - at most MAX_CAL calibrations and MAX_NATIVE native runs at once
    (calibrations are CPU/disk-bound vitest runs; native runs are mostly
    network-wait, so the two kinds complement each other);
  - at least STAGGER_SECONDS between any two launches, because workspace
    materialization spikes CPU and disk;
  - a new job launches only if the measured RSS of all running KVC process
    trees plus the new slot's estimate still fits under RSS_BUDGET_MB;
  - per-job watchdog kills the whole process group (killpg).

The gate only delays launches; it never kills a running job mid-flight.

Usage (key only via environment, never in a file):
  KVC_API_KEY=... python3 -m kvc.harness.run_batch \
      --calibrate-uncalibrated --native pi-retry-attempt-timeout --reps 3
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from kvc.harness.pi_bridge import KVC_ROOT, list_tasks
from kvc.harness.providers import KEY_ENV_NAME

HARNESS_ROOT = KVC_ROOT.parent
RESULTS_ROOT = HARNESS_ROOT / "results" / "kvc"
CAL_ROOT = KVC_ROOT / ".cache" / "calibration"

MAX_CAL = 2
# With the V layer actually running tests, a native run's process tree peaks
# near 2.1 GB (overlay + vitest). User-granted headroom (2026-08-31 evening):
# other heavy processes closed on the machine -> four actor runs at once
# under a 10 GB gate (14 cores / 36 GB machine).
MAX_NATIVE = 4
RSS_BUDGET_MB = 10000
STAGGER_SECONDS = 25.0
CAL_RSS_ESTIMATE_MB = 900
NATIVE_RSS_ESTIMATE_MB = 2100
# KAC actor plus a possible fresh-context probe subprocess.
KAC_RSS_ESTIMATE_MB = 2600
# Fork children: the kac arm generates a card probe before the child starts.
FORK_RSS_ESTIMATE_MB = 2100
FORK_KAC_RSS_ESTIMATE_MB = 2600
CAL_WATCHDOG_SECONDS = 1500.0
NATIVE_WATCHDOG_SECONDS = 720.0


@dataclass
class Job:
    kind: str  # "calibrate" | "native"
    name: str
    argv: list[str]
    watchdog_seconds: float
    rss_estimate_mb: int
    proc: subprocess.Popen | None = None
    started: float = 0.0
    exit_code: int | None = None
    log_path: Path = field(default_factory=Path)
    report: dict = field(default_factory=dict)

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else -1


def tree_rss_mb(root_pid: int) -> float:
    """RSS in MB of a process plus all its descendants."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True
    ).stdout
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid, kb = int(parts[0]), int(parts[1]), int(parts[2])
        rss[pid] = kb
        children.setdefault(ppid, []).append(pid)
    total_kb = 0
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        total_kb += rss.get(pid, 0)
        stack.extend(children.get(pid, []))
    return total_kb / 1024.0


def batch_rss_mb(jobs: list[Job]) -> float:
    return sum(tree_rss_mb(job.pid) for job in jobs if job.proc and job.exit_code is None)


def uncalibrated_tasks(suite: str) -> list[str]:
    pending = []
    for task_id in list_tasks(suite):
        states = []
        for variant in ("base", "gold"):
            path = CAL_ROOT / task_id / variant / "calibration.json"
            if not path.exists():
                states.append("missing")
                continue
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                states.append("missing")
                continue
            states.append("pass" if summary.get("success") else ("error" if summary.get("error") else "fail"))
        calibrated = states == ["fail", "pass"]
        if not calibrated:
            pending.append(task_id)
    return pending


def build_jobs(args: argparse.Namespace) -> list[Job]:
    jobs: list[Job] = []
    if args.calibrate_uncalibrated:
        for task_id in uncalibrated_tasks(args.suite):
            jobs.append(
                Job(
                    kind="calibrate",
                    name=f"cal:{task_id}",
                    argv=[
                        sys.executable,
                        "-m",
                        "kvc.harness.calibrate_worker",
                        "--task",
                        task_id,
                        "--suite",
                        args.suite,
                    ],
                    watchdog_seconds=CAL_WATCHDOG_SECONDS,
                    rss_estimate_mb=CAL_RSS_ESTIMATE_MB,
                )
            )
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    if args.native:
        for task_id in [t.strip() for t in args.native.split(",") if t.strip()]:
            for i in range(1, args.reps + 1):
                run_id = f"{task_id}-native-r{i}-{stamp}"
                jobs.append(
                    Job(
                        kind="native",
                        name=f"native:{run_id}",
                        argv=[
                            sys.executable,
                            "-m",
                            "kvc.harness.run_native",
                            "--task",
                            task_id,
                            "--suite",
                            args.suite,
                            "--run-id",
                            run_id,
                            "--budget",
                            str(args.budget),
                        ],
                        watchdog_seconds=NATIVE_WATCHDOG_SECONDS,
                        rss_estimate_mb=NATIVE_RSS_ESTIMATE_MB,
                    )
                )
    if args.kac:
        for task_id in [t.strip() for t in args.kac.split(",") if t.strip()]:
            for i in range(1, args.reps + 1):
                run_id = f"{task_id}-kac-r{i}-{stamp}"
                jobs.append(
                    Job(
                        kind="kac",
                        name=f"kac:{run_id}",
                        argv=[
                            sys.executable,
                            "-m",
                            "kvc.harness.run_kac",
                            "--task",
                            task_id,
                            "--suite",
                            args.suite,
                            "--run-id",
                            run_id,
                            "--budget",
                            str(args.budget),
                        ],
                        watchdog_seconds=NATIVE_WATCHDOG_SECONDS,
                        rss_estimate_mb=KAC_RSS_ESTIMATE_MB,
                    )
                )
    if args.fork_specs:
        # Trigger-time forks: every fork-spec.json under the given root spawns
        # one child per arm per replicate. Kind "fork" shares the actor slot.
        specs = sorted(Path(args.fork_specs).rglob("fork-spec.json"))
        arms = [a.strip() for a in args.fork_arms.split(",") if a.strip()]
        for spec_path in specs:
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            safe_key = spec["key"].replace("@", "-").replace("/", "-")
            for arm in arms:
                for i in range(1, args.fork_children + 1):
                    run_id = f"{spec['donor_run_id']}-fork-{safe_key}-{arm}-c{i}"
                    jobs.append(
                        Job(
                            kind="fork",
                            name=f"fork:{run_id}",
                            argv=[
                                sys.executable,
                                "-m",
                                "kvc.harness.run_fork_child",
                                "--spec",
                                str(spec_path),
                                "--arm",
                                arm,
                                "--child",
                                str(i),
                                "--run-id",
                                run_id,
                                "--suite",
                                args.suite,
                            ],
                            watchdog_seconds=NATIVE_WATCHDOG_SECONDS,
                            rss_estimate_mb=(
                                FORK_KAC_RSS_ESTIMATE_MB if arm == "kac" else FORK_RSS_ESTIMATE_MB
                            ),
                        )
                    )
    return jobs


def launch(job: Job, batch_dir: Path) -> None:
    job.log_path = batch_dir / f"{job.name.replace(':', '_')}.log"
    log = job.log_path.open("w", encoding="utf-8")
    job.proc = subprocess.Popen(
        job.argv,
        cwd=HARNESS_ROOT,
        env=os.environ.copy(),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    job.started = time.monotonic()


def collect(job: Job) -> None:
    assert job.proc is not None
    job.exit_code = job.proc.poll()
    tail = ""
    try:
        tail = job.log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        pass
    if job.kind in ("native", "kac", "fork"):
        # run_native/run_kac persist the full report; run_id is the job name suffix
        run_id = job.name.split(":", 1)[1]
        report_path = RESULTS_ROOT / run_id / "report.json"
        if report_path.exists():
            try:
                job.report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                pass
    elif job.kind == "calibrate":
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    job.report = json.loads(line)
                except Exception:
                    continue
                break
    job.report.setdefault("exit_code", job.exit_code)


def watchdog(job: Job, batch_dir: Path) -> None:
    time.sleep(job.watchdog_seconds)
    if job.proc and job.exit_code is None and job.proc.poll() is None:
        try:
            os.killpg(os.getpgid(job.proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        (batch_dir / f"{job.name.replace(':', '_')}.watchdog").write_text(
            f"killed after {job.watchdog_seconds:.0f}s\n", encoding="utf-8"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="v3")
    parser.add_argument("--calibrate-uncalibrated", action="store_true")
    parser.add_argument("--native", default=None, help="comma-separated task ids for native replicates")
    parser.add_argument("--kac", default=None, help="comma-separated task ids for KAC-arm replicates")
    parser.add_argument("--fork-specs", default=None, help="root dir scanned for fork-spec.json (trigger-time fork children)")
    parser.add_argument("--fork-arms", default="none,sham,kac", help="comma-separated fork arms")
    parser.add_argument("--fork-children", type=int, default=2, help="children per spec per arm")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--budget", type=float, default=420.0)
    parser.add_argument("--batch-id", default=None)
    args = parser.parse_args()

    jobs = build_jobs(args)
    if not jobs:
        print("nothing to do (all tasks already calibrated, no --native/--kac given)")
        return 0
    if any(job.kind in ("native", "kac", "fork") for job in jobs) and not os.environ.get(KEY_ENV_NAME):
        print(f"FAIL: actor jobs need {KEY_ENV_NAME} in the environment", file=sys.stderr)
        return 2

    batch_id = args.batch_id or time.strftime("batch-%Y%m%d-%H%M%S", time.gmtime())
    batch_dir = RESULTS_ROOT / "_batch" / batch_id
    batch_dir.mkdir(parents=True)

    print(f"batch {batch_id}: {len(jobs)} jobs -> {batch_dir}")
    for job in jobs:
        print(f"  queued {job.name}")
    print(
        f"policy: max_cal={MAX_CAL} max_native={MAX_NATIVE} rss_budget={RSS_BUDGET_MB}MB "
        f"stagger={STAGGER_SECONDS:.0f}s",
        flush=True,
    )

    pending = list(jobs)
    running: list[Job] = []
    done: list[Job] = []
    last_launch = 0.0
    while pending or running:
        # collect finished jobs
        for job in list(running):
            assert job.proc is not None
            if job.proc.poll() is not None:
                collect(job)
                running.remove(job)
                done.append(job)
                elapsed = time.monotonic() - job.started
                print(
                    f"done  {job.name}  exit={job.exit_code}  {elapsed:.0f}s  "
                    f"{json.dumps({k: v for k, v in job.report.items() if k in ('calibrated', 'reason', 'delivered', 'mutation_epochs', 'validation_calls', 'triggers_fired', 'cards_injected', 'cards_accepted', 'final_pass', 'arm', 'fork_key', 'base', 'gold')}, ensure_ascii=False)}",
                    flush=True,
                )
        # launch what fits
        if pending:
            running_cal = sum(1 for j in running if j.kind == "calibrate")
            # native, kac and fork children are actor runs sharing the slot cap
            running_native = sum(1 for j in running if j.kind in ("native", "kac", "fork"))
            rss_now = batch_rss_mb(running)
            for job in list(pending):
                slot_free = (
                    job.kind == "calibrate" and running_cal < MAX_CAL
                ) or (job.kind in ("native", "kac", "fork") and running_native < MAX_NATIVE)
                if not slot_free:
                    continue
                if time.monotonic() - last_launch < STAGGER_SECONDS:
                    break
                if rss_now + job.rss_estimate_mb > RSS_BUDGET_MB:
                    print(
                        f"gate  {job.name} deferred: rss_now={rss_now:.0f}MB + "
                        f"estimate={job.rss_estimate_mb}MB > {RSS_BUDGET_MB}MB",
                        flush=True,
                    )
                    continue
                pending.remove(job)
                launch(job, batch_dir)
                running.append(job)
                last_launch = time.monotonic()
                rss_now += job.rss_estimate_mb
                if job.kind == "calibrate":
                    running_cal += 1
                else:
                    running_native += 1
                threading.Thread(
                    target=watchdog, args=(job, batch_dir), daemon=True
                ).start()
                print(
                    f"start {job.name}  pid={job.pid}  rss_now~{batch_rss_mb(running):.0f}MB",
                    flush=True,
                )
        time.sleep(3.0)

    summary = {
        "batch_id": batch_id,
        "policy": {
            "max_cal": MAX_CAL,
            "max_native": MAX_NATIVE,
            "rss_budget_mb": RSS_BUDGET_MB,
            "stagger_seconds": STAGGER_SECONDS,
        },
        "jobs": [
            {
                "name": job.name,
                "kind": job.kind,
                "exit_code": job.exit_code,
                "duration_seconds": round(time.monotonic() - job.started, 1) if job.started else None,
                "log": str(job.log_path),
                "report": job.report,
            }
            for job in done
        ],
    }
    (batch_dir / "batch.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    failed = [job.name for job in done if job.exit_code != 0]
    print(f"batch {batch_id} finished: {len(done)} jobs, {len(failed)} nonzero-exit")
    if failed:
        print("nonzero: " + ", ".join(failed))
    print(f"summary: {batch_dir / 'batch.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

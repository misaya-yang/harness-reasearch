"""Trigger-time fork snapshot collection (Round 3 causal design).

When a deterministic trigger (T1/T2/T3) fires on a DONOR actor run, the
collector freezes everything needed to restart the run from that exact moment:

* workspace tree  — APFS clonefile copy, then committed + tagged inside the
                    copy (the donor workspace is never touched);
* event-log prefix, GPS render, machine state (gps.to_json()), read paths;
* remaining budget at the trigger instant.

Each snapshot is written to `<run_base>/forks/<key>/` as `fork-spec.json`.
Children are spawned later by run_fork_child (via run_batch --fork-specs), so
donor runs and fork children never contend for resources mid-flight.

The donor itself receives NO intervention: it is a pure native continuation.
All arms (kac card / sham card / none) exist only among fork children, which
removes the selection bias of "cards only land on struggling trajectories".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from kvc.harness.kact import collect_probe_inputs_from_state
from kvc.harness.kvc_run import KvcRunner, _fired_key

GIT_IDENTITY = ("-c", "user.name=KVC Fork", "-c", "user.email=kvc-fork@invalid")
SNAPSHOT_TAG_PREFIX = "kvc/fork-snapshot-"


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=workspace, capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


def clone_tree(src: Path, dst: Path) -> None:
    """APFS clonefile copy (O(1)); falls back to a full copy elsewhere."""
    result = subprocess.run(["cp", "-Rc", str(src), str(dst)], capture_output=True)
    if result.returncode != 0:
        shutil.copytree(src, dst, symlinks=True)


def snapshot_workspace(workspace: Path, key: str) -> dict[str, Any]:
    """Freeze the workspace tree into a sibling directory; commit + tag it.

    The snapshot commit becomes the fork children's HEAD, so their mutation
    tracker baselines on the exact tree at trigger time.
    """
    safe_key = key.replace("@", "-").replace("/", "-")
    snapshot = workspace.parent / f"snapshot-{safe_key}"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    clone_tree(workspace, snapshot)
    _git(snapshot, "add", "-A")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=snapshot, capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    if status:
        _git(snapshot, "commit", "-q", "-m", f"fork snapshot {key}")
    tag = f"{SNAPSHOT_TAG_PREFIX}{safe_key}"
    _git(snapshot, "tag", "-f", tag)
    sha = _git(snapshot, "rev-parse", "HEAD")
    return {"snapshot_path": str(snapshot), "snapshot_tag": tag, "snapshot_sha": sha}


class ForkCollector:
    """TriggerHook implementation: freeze a fork spec per trigger."""

    def __init__(self, task: dict[str, Any], task_id: str, run_id: str):
        self.task = task
        self.task_id = task_id
        self.run_id = run_id
        self._busy = threading.Event()

    def __call__(self, runner: KvcRunner, trigger: str) -> None:
        # Runs on the actor's reader thread: never block it.
        threading.Thread(
            target=self._collect, args=(runner, trigger), daemon=True
        ).start()

    def _collect(self, runner: KvcRunner, trigger: str) -> None:
        key = _fired_key(trigger, runner.gps)
        if self._busy.is_set():
            runner._record({"type": "kvc_fork_skipped", "key": key, "reason": "snapshot already running"})
            return
        self._busy.set()
        started = time.monotonic()
        try:
            snapshot = snapshot_workspace(runner.config.workspace, key)
            fork_dir = runner.config.run_dir.parent / "forks" / key.replace("@", "-").replace("/", "-")
            fork_dir.mkdir(parents=True, exist_ok=True)
            # Event-log prefix: everything recorded up to the trigger instant.
            events_src = runner.config.run_dir / "events" / "events.jsonl"
            if events_src.exists():
                shutil.copy(events_src, fork_dir / "events-prefix.jsonl")
            inputs = collect_probe_inputs_from_state(
                workspace=Path(snapshot["snapshot_path"]),
                events_path=fork_dir / "events-prefix.jsonl",
                gps_render=runner.gps.render(),
                read_paths=list(runner.read_paths),
                task=self.task,
            )
            (fork_dir / "probe-input.json").write_text(
                json.dumps(inputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            spec = {
                "schema": "kvc-fork-spec/1",
                "donor_run_id": self.run_id,
                "task_id": self.task_id,
                "trigger": trigger,
                "key": key,
                "gps": runner.gps.to_json(),
                "gps_render": runner.gps.render(),
                "remaining_budget_seconds": round(runner.gps.remaining(), 1),
                "elapsed_at_trigger_seconds": round(runner.gps.elapsed(), 1),
                "read_paths": list(runner.read_paths),
                "frozen_wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **snapshot,
            }
            (fork_dir / "fork-spec.json").write_text(
                json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            runner._record({
                "type": "kvc_fork_snapshot",
                "key": key,
                "trigger": trigger,
                "snapshot_sha": snapshot["snapshot_sha"],
                "remaining_budget_seconds": spec["remaining_budget_seconds"],
                "duration_seconds": round(time.monotonic() - started, 1),
                "fork_dir": str(fork_dir),
            })
        except Exception as error:  # snapshot failures must never harm the donor
            runner._record({"type": "kvc_fork_error", "key": key, "trigger": trigger, "error": repr(error)})
        finally:
            self._busy.clear()

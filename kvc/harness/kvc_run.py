"""KVC actor driver: external K/V/C loop around a Pi RPC subprocess.

The Pi process is the actor. This driver owns everything the plan assigns to
the harness side (DESIGN.md sections 3.1-3.6):

* event recording with wall-clock + monotonic stamps (events.jsonl)
* mutation-epoch detection from workspace git state (never from tool names)
* GPS state machine and deterministic T1/T2/T3 triggers
* incumbent save on passing validation, rescue on cutoff
* 420s watchdog with abort grace then process-group kill
* RSS resource cap (legacy runner discipline)
* isolated child environment (fake HOME/TMPDIR, curated PATH, key only in env)

The driver never edits Pi sources; it runs Pi from source via tsx against the
local clone and treats the materialized task workspace as the only mutable tree.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kvc.harness.gps import GpsState, TriggerConfig, evaluate_triggers
from kvc.harness.incumbent import IncumbentManager, RescueResult
from kvc.harness.mutation_tracker import MutationTracker

KVC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PI_REPO = Path("/Users/misaya.yanghejazfs.com.au/misaya_project/Agent_projects/pi")
DEFAULT_TOOLS = ("read", "bash", "edit", "write", "validate_current_patch")
DEFAULT_EXTENSIONS = (
    KVC_ROOT / "extensions" / "kvc-validate.ts",
    KVC_ROOT / "extensions" / "request-probe.ts",
)
MUTATING_TOOLS = {"bash", "edit", "write"}
VALIDATE_TOOL = "validate_current_patch"

TriggerHook = Callable[["KvcRunner", str], None]


@dataclass
class RunConfig:
    workspace: Path
    run_dir: Path
    task_prompt: str
    objective_anchor: str
    pi_repo: Path = DEFAULT_PI_REPO
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5"
    thinking_level: str = "off"
    tools: tuple[str, ...] = DEFAULT_TOOLS
    extensions: tuple[Path, ...] = DEFAULT_EXTENSIONS
    budget_seconds: float = 420.0
    abort_grace_seconds: float = 10.0
    key_env_name: str = "ANTHROPIC_API_KEY"  # name the child env receives the key under
    key_value: str = ""  # never persisted; lives only in child env
    proxy_env_names: tuple[str, ...] = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
    extra_path_dirs: tuple[str, ...] = ()
    validator_command: str | None = None
    validator_timeout_seconds: int = 90
    validator_counterexample_grep: str | None = None
    validation_budget: int = 2
    max_rss_mb: int = 2500
    resource_poll_seconds: float = 10.0
    trigger_config: TriggerConfig = field(default_factory=TriggerConfig)
    system_prompt_args: tuple[str, ...] = ()  # e.g. ("--append-system-prompt", "<text>")
    extra_env: dict[str, str] = field(default_factory=dict)  # test hooks; never keys
    # Custom provider catalog written to agent-dir/models.json. Keys must use
    # env interpolation ("$VAR"); a literal key is never written to this file.
    models_json: dict[str, Any] | None = None
    # Task row for the overlay verifier (validate_overlay.py); staged into
    # run_dir/validator/ together with the precomputed hidden-test patch.
    validator_task: dict[str, Any] | None = None


@dataclass
class RunOutcome:
    reason: str = "unknown"  # settled | budget | resource_cap | error
    delivered: bool = False
    rescued: RescueResult | None = None
    session_stats: dict[str, Any] | None = None
    peak_rss_mb: float = 0.0
    epochs: int = 0
    validations: int = 0
    triggers_fired: list[str] = field(default_factory=list)
    started_wall: str = ""
    ended_wall: str = ""
    duration_seconds: float = 0.0


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + (".%03dZ" % int((time.time() % 1) * 1000))


def _fired_key(trigger: str, gps: GpsState) -> str:
    if trigger == "T1":
        return "T1"
    if trigger == "T2" and gps.validation is not None:
        return f"T2@epoch{gps.validation.epoch}"
    if trigger == "T3":
        return f"T3@epoch{gps.mutation_epoch}"
    return f"{trigger}@?"


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    """Parse the stamped event log of a finished (or running) KVC run."""
    path = run_dir / "events" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def build_child_env(
    config: RunConfig, fake_home: Path, tmpdir: Path | None = None
) -> dict[str, str]:
    """Isolated environment per legacy runner discipline. Key only in env.

    tmpdir, when given, must be a SHORT path: tsx's IPC unix socket lives at
    $TMPDIR/tsx-<uid>/<pid>.pipe, and macOS truncates sockaddr_un paths to
    104 bytes — two runs whose TMPDIR shares its first 104 bytes collide on
    the same truncated kernel address (EADDRINUSE). Long run-dir paths are
    therefore never used as TMPDIR for concurrent runs.
    """
    path_dirs = [
        str(config.pi_repo / "node_modules" / ".bin"),
        str(config.workspace / "node_modules" / ".bin"),
        *config.extra_path_dirs,
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    env = {
        "HOME": str(fake_home),
        "TMPDIR": str(tmpdir if tmpdir is not None else fake_home),
        "PATH": os.pathsep.join(path_dirs),
        "PI_CODING_AGENT_DIR": str(config.run_dir / "agent-dir"),
        "KVC_VALIDATOR_DIR": str(config.run_dir / "validator"),
        "KVC_VALIDATION_BUDGET": str(config.validation_budget),
        "KVC_EPOCH_FILE": str(config.run_dir / "state" / "epoch.txt"),
        "KVC_PROBE_FILE": str(config.run_dir / "state" / "provider-requests.jsonl"),
        "KVC_RUN_DIR": str(config.run_dir),
        "KVC_ACTOR_WORKSPACE": str(config.workspace),
        # Deliberately NOT exported: the pi checkout contains post-fix source
        # for these regression tasks; an env var pointing at it is a solution
        # leak the actor can read with one `env` dump (observed in batch-2 r2).
        config.key_env_name: config.key_value,
    }
    for name in config.proxy_env_names:
        if os.environ.get(name):
            env[name] = os.environ[name]
    env.update(config.extra_env)
    return env


class ResourceMonitor(threading.Thread):
    """Sample total RSS of the actor process tree; legacy 2.5GB-cap discipline."""

    def __init__(self, root_pid: int, poll_seconds: float, on_breach: Callable[[float], None]):
        super().__init__(daemon=True)
        self.root_pid = root_pid
        self.poll_seconds = poll_seconds
        self.on_breach = on_breach
        self.peak_mb = 0.0
        self.cap_mb: float | None = None
        self._stop = threading.Event()

    def _tree_rss_mb(self) -> float:
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, timeout=10
            ).stdout
        except Exception:
            return 0.0
        children: dict[int, list[int]] = {}
        rss: dict[int, int] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            pid, ppid, rss_kb = int(parts[0]), int(parts[1]), int(parts[2])
            rss[pid] = rss_kb
            children.setdefault(ppid, []).append(pid)
        total = 0
        stack = [self.root_pid]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in seen or pid not in rss:
                continue
            seen.add(pid)
            total += rss[pid]
            stack.extend(children.get(pid, ()))
        return total / 1024.0

    def run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            mb = self._tree_rss_mb()
            self.peak_mb = max(self.peak_mb, mb)
            if self.cap_mb is not None and mb > self.cap_mb:
                self.on_breach(mb)

    def stop(self) -> None:
        self._stop.set()


class KvcRunner:
    def __init__(self, config: RunConfig, on_trigger: TriggerHook | None = None):
        self.config = config
        self.on_trigger = on_trigger
        self.gps = GpsState(objective_anchor=config.objective_anchor, budget_seconds=config.budget_seconds)
        self.tracker: MutationTracker | None = None
        self.incumbent = IncumbentManager(config.workspace)
        self.outcome = RunOutcome()
        self.fired: set[str] = set()
        self.read_paths: list[str] = []
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_file: Any = None
        self._reader: threading.Thread | None = None
        self._monitor: ResourceMonitor | None = None
        self._responses: dict[str, dict[str, Any]] = {}
        self._response_events: dict[str, threading.Event] = {}
        self._settled = threading.Event()
        self._agent_settled = False
        self._kill_requested = threading.Event()
        self._lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._cmd_seq = 0
        self._events_file: Any = None
        self._short_tmpdir: Path | None = None

    # ------------------------------------------------------------------ setup

    def _prepare_dirs(self) -> None:
        cfg = self.config
        for sub in ("state", "validator", "agent-dir", "events"):
            (cfg.run_dir / sub).mkdir(parents=True, exist_ok=True)
        (cfg.run_dir / "state" / "epoch.txt").write_text("0\n", encoding="utf-8")
        if cfg.models_json is not None:
            (cfg.run_dir / "agent-dir" / "models.json").write_text(
                json.dumps(cfg.models_json, indent=2), encoding="utf-8"
            )
        if cfg.validator_command:
            validator = {
                "command": cfg.validator_command,
                "timeout_seconds": cfg.validator_timeout_seconds,
            }
            if cfg.validator_counterexample_grep:
                validator["counterexample_grep"] = cfg.validator_counterexample_grep
            (cfg.run_dir / "validator" / "kvc-validator.json").write_text(
                json.dumps(validator, indent=2), encoding="utf-8"
            )
        if cfg.validator_task:
            from kvc.harness.pi_bridge import ensure_base_mirror, retarget

            (cfg.run_dir / "validator" / "hidden-tests.patch").write_bytes(
                retarget().hidden_test_patch(cfg.validator_task)
            )
            # task.json is actor-readable, so it must carry no reference to the
            # fix (gold_commit, hidden_test_files) and no pointer to the real Pi
            # checkout, whose history contains the fix. The overlay is built
            # from a synthetic mirror holding only the base tree, and hidden
            # tests come from the precomputed patch file above.
            mirror_repo, mirror_sha = ensure_base_mirror(cfg.validator_task["base_commit"])
            sanitized = {
                k: v
                for k, v in cfg.validator_task.items()
                if k not in ("gold_commit", "hidden_test_files")
            }
            sanitized["source_repo"] = str(mirror_repo)
            sanitized["base_commit"] = mirror_sha
            (cfg.run_dir / "validator" / "task.json").write_text(
                json.dumps(sanitized, indent=2), encoding="utf-8"
            )

    def _argv(self) -> list[str]:
        cfg = self.config
        tsx = cfg.pi_repo / "node_modules" / ".bin" / "tsx"
        cli = cfg.pi_repo / "packages" / "coding-agent" / "src" / "cli.ts"
        argv = [
            str(tsx),
            "--tsconfig", str(cfg.pi_repo / "tsconfig.json"),
            str(cli),
            "--mode", "rpc",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--approve",
            "--provider", cfg.provider,
            "--model", cfg.model,
            "--thinking", cfg.thinking_level,
            "--tools", ",".join(cfg.tools),
        ]
        for ext in cfg.extensions:
            argv += ["-e", str(ext)]
        argv += list(cfg.system_prompt_args)
        return argv

    # ------------------------------------------------------------ process io

    def start(self) -> None:
        cfg = self.config
        self._prepare_dirs()
        self.tracker = MutationTracker(workspace=cfg.workspace)
        self.gps.start_monotonic = time.monotonic()
        self.outcome.started_wall = _utcnow_iso()
        self._events_file = (cfg.run_dir / "events" / "events.jsonl").open("a", encoding="utf-8")
        # Short unique TMPDIR so tsx's IPC socket path stays below macOS's
        # 104-byte sockaddr_un limit and concurrent runs never collide.
        self._short_tmpdir = Path(tempfile.mkdtemp(prefix="kvc-tmp-", dir="/tmp"))
        env = build_child_env(cfg, fake_home=cfg.run_dir / "fake-home", tmpdir=self._short_tmpdir)
        (cfg.run_dir / "fake-home").mkdir(exist_ok=True)
        self._stderr_file = (cfg.run_dir / "events" / "stderr.log").open("w", encoding="utf-8")
        self._proc = subprocess.Popen(
            self._argv(),
            cwd=cfg.workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._monitor = ResourceMonitor(
            self._proc.pid, cfg.resource_poll_seconds, self._on_resource_breach
        )
        self._monitor.cap_mb = float(cfg.max_rss_mb)
        self._monitor.start()

    def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            stamp = {"_wall": _utcnow_iso(), "_mono": round(time.monotonic() - self.gps.start_monotonic, 3)}
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                self._record({"_raw": line, **stamp})
                continue
            self._record({**frame, **stamp})
            if frame.get("type") == "response":
                rid = frame.get("id")
                with self._lock:
                    self._responses[rid] = frame
                    event = self._response_events.get(rid)
                if event:
                    event.set()
            else:
                self._on_event(frame)
        self._settled.set()  # process stdout closed: unblock any waiter

    def _record(self, frame: dict[str, Any]) -> None:
        # _record is called from the reader thread, trigger-hook probe threads,
        # and teardown; serialize writes so JSONL lines never interleave.
        if self._events_file:
            if "_mono" not in frame:
                frame = {
                    **frame,
                    "_wall": _utcnow_iso(),
                    "_mono": round(time.monotonic() - self.gps.start_monotonic, 3),
                }
            with self._record_lock:
                self._events_file.write(json.dumps(frame, ensure_ascii=False) + "\n")
                self._events_file.flush()

    def send_command(self, command: dict[str, Any], timeout: float = 30.0) -> dict[str, Any] | None:
        proc = self._proc
        if not proc or not proc.stdin or self._kill_requested.is_set():
            return None
        with self._lock:
            self._cmd_seq += 1
            rid = f"c{self._cmd_seq}"
            event = threading.Event()
            self._response_events[rid] = event
        try:
            proc.stdin.write(json.dumps({**command, "id": rid}) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return None
        if not event.wait(timeout):
            return None
        with self._lock:
            return self._responses.pop(rid, None)

    # ----------------------------------------------------------- event hooks

    def _on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "tool_execution_end":
            self._on_tool_end(event)
        elif etype == "tool_execution_start":
            tool_name = event.get("toolName")
            if tool_name in MUTATING_TOOLS:
                self.gps.on_tool_call()
            elif tool_name == "read":
                path = (event.get("args") or {}).get("path")
                if isinstance(path, str) and path not in self.read_paths:
                    self.read_paths.append(path)
        elif etype == "agent_settled":
            self._agent_settled = True
            self._settled.set()
        self._check_triggers()

    def _on_tool_end(self, event: dict[str, Any]) -> None:
        tool = event.get("toolName")
        if tool in MUTATING_TOOLS and self.tracker:
            epoch_event = self.tracker.observe(tool)
            if epoch_event:
                self.gps.on_mutation()
                (self.config.run_dir / "state" / "epoch.txt").write_text(
                    f"{self.tracker.epoch}\n", encoding="utf-8"
                )
                self._record({
                    "type": "kvc_epoch",
                    "epoch": epoch_event.epoch,
                    "tool": tool,
                    "paths": epoch_event.paths_changed,
                    "diff_stat": epoch_event.diff_stat,
                })
        elif tool == VALIDATE_TOOL:
            self._on_validation_result(event)

    def _on_validation_result(self, event: dict[str, Any]) -> None:
        result = event.get("result")
        payload: dict[str, Any] | None = None
        if isinstance(result, dict):
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
            if payload is None:
                content = result.get("content")
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    try:
                        payload = json.loads(content[0].get("text", ""))
                    except (json.JSONDecodeError, TypeError):
                        payload = None
        self.outcome.validations += 1
        if payload and payload.get("result") in ("pass", "fail", "stale"):
            record = self.gps.on_validation(
                payload["result"], payload.get("scope", "focused_behavior"), payload.get("counterexample")
            )
            self._record({"type": "kvc_validation", **record.to_json(), "raw": payload})
            if payload["result"] == "pass":
                sha = self.incumbent.save(self.gps.mutation_epoch)
                self._record({
                    "type": "kvc_incumbent_saved",
                    "epoch": self.gps.mutation_epoch,
                    "sha": sha,
                })

    def _check_triggers(self) -> None:
        for trigger in evaluate_triggers(self.gps, self.config.trigger_config, self.fired):
            key = _fired_key(trigger, self.gps)
            self.fired.add(key)
            self.outcome.triggers_fired.append(f"{trigger}({key})")
            self._record({"type": "kvc_trigger", "trigger": trigger, "key": key, "gps": self.gps.to_json()})
            if self.on_trigger:
                try:
                    self.on_trigger(self, trigger)
                except Exception as error:  # a hook failure must never kill the run
                    self._record({"type": "kvc_trigger_hook_error", "trigger": trigger, "error": str(error)})

    def _on_resource_breach(self, rss_mb: float) -> None:
        if self._kill_requested.is_set():
            return
        self._record({"type": "kvc_resource_cap", "rss_mb": round(rss_mb, 1)})
        self._terminate("resource_cap")

    # ---------------------------------------------------------------- control

    def steer(self, text: str) -> bool:
        response = self.send_command({"type": "steer", "message": text})
        return bool(response and response.get("success"))

    def run_prompt(self, message: str) -> RunOutcome:
        cfg = self.config
        deadline = time.monotonic() + cfg.budget_seconds
        response = self.send_command({"type": "prompt", "message": message}, timeout=cfg.budget_seconds)
        if not response or not response.get("success"):
            self.outcome.reason = "error"
            self._record({"type": "kvc_prompt_error", "response": response})
            return self.finish()
        remaining = deadline - time.monotonic()
        settled = self._settled.wait(max(0.0, remaining))
        if not settled:
            self._terminate("budget")
        elif self.outcome.reason == "unknown":
            self.outcome.reason = "settled"
        return self.finish()

    def _terminate(self, reason: str) -> None:
        if self._kill_requested.is_set():
            return
        self._kill_requested.set()
        self.outcome.reason = reason
        self._record({"type": "kvc_terminate", "reason": reason, "gps": self.gps.to_json()})
        proc = self._proc
        if not proc:
            return
        try:
            # Capture usage before aborting; mid-stream stats still respond.
            stats = self.send_command({"type": "get_session_stats"}, timeout=3.0)
            if stats and stats.get("success"):
                self.outcome.session_stats = stats.get("data")
        except Exception:
            pass
        try:
            self.send_command({"type": "abort"}, timeout=5.0)
        except Exception:
            pass
        grace_end = time.monotonic() + self.config.abort_grace_seconds
        while proc.poll() is None and time.monotonic() < grace_end:
            time.sleep(0.2)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(3.0)
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def finish(self) -> RunOutcome:
        if self.outcome.reason == "unknown":
            # Manual flows (no run_prompt): infer from what actually happened.
            # _settled also fires on stdout EOF, so require the real event.
            if self._kill_requested.is_set():
                self.outcome.reason = "error"
            elif self._agent_settled:
                self.outcome.reason = "settled"
            else:
                self.outcome.reason = "error"
        proc = self._proc
        if proc and not self._kill_requested.is_set():
            stats = self.send_command({"type": "get_session_stats"}, timeout=10.0)
            if stats and stats.get("success"):
                self.outcome.session_stats = stats.get("data")
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._terminate("error")
        if self._monitor:
            self.outcome.peak_rss_mb = round(self._monitor.peak_mb, 1)
            self._monitor.stop()
        # Rescue policy (DESIGN.md 3.5): only cutoff (budget/resource/error)
        # rolls back to the latest validated incumbent; such outcomes are
        # scored separately as workspace rescue. Settled runs keep their tree:
        # a passing incumbent is already committed by save().
        if self.outcome.reason != "settled" and self.incumbent.latest() is not None:
            self.outcome.rescued = self.incumbent.rescue()
        self.outcome.delivered = self.gps.delivered
        self.outcome.epochs = self.tracker.epoch if self.tracker else 0
        self.outcome.ended_wall = _utcnow_iso()
        self.outcome.duration_seconds = round(self.gps.elapsed(), 1)
        self._write_manifest()
        if self._events_file:
            self._events_file.close()
            self._events_file = None
        stderr_file = getattr(self, "_stderr_file", None)
        if stderr_file:
            time.sleep(0.2)  # let the child's final stderr drain
            stderr_file.close()
            self._stderr_file = None
        if self._short_tmpdir:
            shutil.rmtree(self._short_tmpdir, ignore_errors=True)
            self._short_tmpdir = None
        return self.outcome

    def _write_manifest(self) -> None:
        cfg = self.config
        manifest = {
            "schema": "kvc-run-manifest/1",
            "task": {
                "workspace": str(cfg.workspace),
                "objective_anchor": cfg.objective_anchor,
            },
            "actor": {
                "provider": cfg.provider,
                "model": cfg.model,
                "thinking_level": cfg.thinking_level,
                "tools": list(cfg.tools),
                "extensions": [str(e) for e in cfg.extensions],
                # pi_repo deliberately absent: this file is actor-readable and
                # the Pi checkout's HEAD contains the fix for regression tasks.
                "budget_seconds": cfg.budget_seconds,
            },
            "outcome": {
                "reason": self.outcome.reason,
                "delivered": self.outcome.delivered,
                "duration_seconds": self.outcome.duration_seconds,
                "mutation_epochs": self.outcome.epochs,
                "validation_calls": self.outcome.validations,
                "triggers_fired": self.outcome.triggers_fired,
                "peak_rss_mb": self.outcome.peak_rss_mb,
                "rescued_to": self.outcome.rescued.rescued_to if self.outcome.rescued else None,
                "rescued_tag": self.outcome.rescued.rescued_tag if self.outcome.rescued else None,
                "session_stats": self.outcome.session_stats,
            },
            "gps_final": self.gps.to_json(),
            "started_wall": self.outcome.started_wall,
            "ended_wall": self.outcome.ended_wall,
        }
        (cfg.run_dir / "run-manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

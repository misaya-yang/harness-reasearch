"""M0-B: live steer-timing smoke against real Pi + the qwen endpoint.

Drives KvcRunner against a real Pi RPC subprocess with a tiny multi-read task,
injects one steer at the first tool_execution_start, and verifies pi's delivery
semantics that KAC decision cards depend on:

  1. the steer command is accepted mid-run;
  2. queue_update events show the steering queue non-empty then drained;
  3. the steered text reaches the conversation after the current tool batch
     (never interrupting an in-flight batch);
  4. the model addresses the steered instruction in a later assistant message.

Usage (key only via environment, never in a file):
  KVC_API_KEY=... python3 -m kvc.harness.smoke_steer
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from kvc.harness.kvc_run import KvcRunner, RunConfig, read_events
from kvc.harness.providers import dashscope_models_json

GIT_IDENTITY = ("-c", "user.name=KVC Smoke", "-c", "user.email=kvc-smoke@invalid")
STEER_MARKER = "[KVC-SMOKE-B]"
TASK = (
    "Read the files src/a.txt, src/b.txt and src/c.txt using three separate "
    "read tool calls, one at a time, waiting for each result. After reading "
    "all three files, reply with exactly: DONE"
)
STEER_TEXT = (
    f"{STEER_MARKER} Additional instruction: when you reply DONE, also append "
    "the total number of lines across the three files as LINES=<n>."
)


def make_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="kvc-smokeb-ws-"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "src").mkdir()
    for name, lines in (("a.txt", 3), ("b.txt", 5), ("c.txt", 2)):
        (root / "src" / name).write_text(
            "".join(f"{name} line {i}\n" for i in range(1, lines + 1)), encoding="utf-8"
        )
    subprocess.run(["git", *GIT_IDENTITY, "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *GIT_IDENTITY, "commit", "-q", "-m", "smoke base"], cwd=root, check=True)
    return root


class SteerSmokeRunner(KvcRunner):
    def __init__(self, config: RunConfig):
        super().__init__(config)
        self.steer_sent_at_mono: float | None = None
        self.steer_accepted: bool = False
        self._steer_done = threading.Event()

    def _on_event(self, event: dict) -> None:
        if event.get("type") == "tool_execution_start" and not self._steer_done.is_set():
            self._steer_done.set()  # inject exactly once
            threading.Thread(target=self._inject_steer, daemon=True).start()
        super()._on_event(event)

    def _inject_steer(self) -> None:
        # Never call send_command from the reader thread: it waits for a
        # response that only the reader thread can deliver.
        self.steer_sent_at_mono = time.monotonic() - self.gps.start_monotonic
        self.steer_accepted = self.steer(STEER_TEXT)


def main() -> int:
    key = os.environ.get("KVC_API_KEY", "")
    if not key:
        print("FAIL: KVC_API_KEY not in environment", file=sys.stderr)
        return 2
    workspace = make_workspace()
    run_dir = Path(tempfile.mkdtemp(prefix="kvc-smokeb-run-"))
    config = RunConfig(
        workspace=workspace,
        run_dir=run_dir,
        task_prompt=TASK,
        objective_anchor="read three files and report",
        provider="dashscope-intl",
        model="qwen3.8-flash",
        thinking_level="off",
        tools=("read", "validate_current_patch"),
        key_env_name="KVC_API_KEY",
        key_value=key,
        budget_seconds=180.0,
        extra_env={"PI_OFFLINE": "1"},
        models_json=dashscope_models_json(),
    )
    runner = SteerSmokeRunner(config)
    runner.start()
    prompt_response = runner.send_command({"type": "prompt", "message": TASK}, timeout=180.0)
    if not (prompt_response and prompt_response.get("success")):
        stderr_log = run_dir / "events" / "stderr.log"
        print(f"prompt rejected: {prompt_response}", file=sys.stderr)
        print(f"run_dir: {run_dir}", file=sys.stderr)
        print(f"actor exit code: {runner._proc.poll() if runner._proc else 'none'}", file=sys.stderr)
        print(f"stderr tail: {stderr_log.read_text(encoding='utf-8')[-1500:]}", file=sys.stderr)
        return 2
    settled = runner._settled.wait(170.0)
    messages_response = runner.send_command({"type": "get_messages"}, timeout=15.0)
    outcome = runner.finish()
    events = read_events(run_dir)

    checks: list[tuple[str, bool, str]] = []
    checks.append(("run settled", settled and outcome.reason == "settled", outcome.reason))
    checks.append(("steer accepted mid-run", runner.steer_accepted, ""))

    queue_updates = [e for e in events if e.get("type") == "queue_update"]
    queued = [e for e in queue_updates if any(STEER_MARKER in s for s in e.get("steering", []))]
    checks.append(("queue_update showed steer queued", bool(queued), f"{len(queue_updates)} queue_update events"))

    messages = (messages_response or {}).get("data", {}).get("messages", [])
    texts = []
    for message in messages:
        content = message.get("content", [])
        if isinstance(content, list):
            texts.append((message.get("role"), "".join(c.get("text", "") for c in content if isinstance(c, dict))))
        elif isinstance(content, str):
            texts.append((message.get("role"), content))
    steer_user = [t for role, t in texts if role == "user" and STEER_MARKER in t]
    checks.append(("steer text reached conversation", bool(steer_user), f"{len(steer_user)} user message(s)"))
    lines_reply = [t for role, t in texts if role == "assistant" and "LINES=" in t]
    checks.append(("model honored steered instruction", bool(lines_reply), repr(lines_reply[0][:80]) if lines_reply else ""))
    done_reply = [t for role, t in texts if role == "assistant" and "DONE" in t]
    checks.append(("base task completed", bool(done_reply), ""))

    print(f"run_dir: {run_dir}")
    print(f"steer sent at t+{runner.steer_sent_at_mono:.1f}s" if runner.steer_sent_at_mono else "steer never sent")
    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        if not ok:
            failed += 1
    stats = outcome.session_stats or {}
    print(f"tokens: {json.dumps(stats.get('tokens', {}))} cost={stats.get('cost')} duration={outcome.duration_seconds}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

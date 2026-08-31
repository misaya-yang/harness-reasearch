"""Fake Pi RPC actor for offline driver tests.

Speaks the same JSONL protocol as `pi --mode rpc`: responses for commands on
stdout, agent events for a scripted "prompt". Two scenarios via FAKE_PI_SCENARIO:

  settle           mutate once, pass validation, settle promptly
  hang_after_pass  mutate once, pass validation, then hang (budget test)

The mutation is REAL (writes src/main.ts in cwd) so the driver's git-based
MutationTracker observes it exactly as in production.
"""

from __future__ import annotations

import json
import os
import sys
import time


def emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def respond(cmd_id: str | None, command: str, data: dict | None = None) -> None:
    frame: dict = {"id": cmd_id, "type": "response", "command": command, "success": True}
    if data is not None:
        frame["data"] = data
    emit(frame)


def main() -> None:
    scenario = os.environ.get("FAKE_PI_SCENARIO", "settle")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        ctype = cmd.get("type")
        cid = cmd.get("id")
        if ctype == "prompt":
            respond(cid, "prompt")
            emit({"type": "agent_start"})
            emit({"type": "turn_start"})
            os.makedirs("src", exist_ok=True)
            with open("src/main.ts", "w", encoding="utf-8") as handle:
                handle.write("export const fixed = true;\n")
            emit({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "edit", "args": {}})
            emit({"type": "tool_execution_end", "toolCallId": "t1", "toolName": "edit", "result": {}, "isError": False})
            payload = {
                "mutation_epoch": 1,
                "validation_epoch": 1,
                "scope": "focused_behavior",
                "result": "pass",
                "counterexample": None,
                "applies_to_current_source": True,
            }
            emit({"type": "tool_execution_start", "toolCallId": "t2", "toolName": "validate_current_patch", "args": {}})
            emit({
                "type": "tool_execution_end",
                "toolCallId": "t2",
                "toolName": "validate_current_patch",
                "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
                "isError": False,
            })
            if scenario == "hang_after_pass":
                time.sleep(30)  # killed by the driver's watchdog long before this ends
            emit({"type": "agent_end", "messages": [], "willRetry": False})
            emit({"type": "agent_settled"})
        elif ctype == "abort":
            respond(cid, "abort")
            if scenario == "hang_after_pass":
                time.sleep(5)  # slow actor honoring abort: driver escalates to SIGTERM
            return
        elif ctype == "get_session_stats":
            respond(cid, "get_session_stats", {"tokens": {"total": 123}, "cost": 0.001})
        elif ctype in ("steer", "follow_up"):
            respond(cid, ctype)
        elif ctype == "get_state":
            respond(cid, "get_state", {"messageCount": 2})


if __name__ == "__main__":
    main()

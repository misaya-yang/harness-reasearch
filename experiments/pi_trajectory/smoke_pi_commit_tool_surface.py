"""Verify that Pi exposes the EBCP tool and prompt contribution to the provider request."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from run_pi_commit_protocol import (
    COMMIT_PROTOCOL,
    CONFIG_TEMPLATE,
    PI_LAUNCHER,
    REQUEST_LOGGER,
    RESOURCE_ENV,
    active_tools,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error(f"refusing to overwrite {output}")
    output.mkdir(parents=True)
    workspace = output / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("# EBCP tool-surface smoke\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=EBCP Smoke",
            "-c",
            "user.email=smoke@invalid",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=workspace,
        check=True,
    )
    agent_dir = output / "pi-agent"
    shutil.copytree(CONFIG_TEMPLATE, agent_dir)
    request_log = output / "model-requests.jsonl"
    protocol_log = output / "commit-protocol.jsonl"
    env = os.environ.copy()
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN is missing")
    env.update(
        {
            **RESOURCE_ENV,
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_RESEARCH_REQUEST_LOG": str(request_log),
            "PI_RESEARCH_COMMIT_LOG": str(protocol_log),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
    )
    command = [
        "/usr/bin/nice",
        "-n",
        "10",
        str(PI_LAUNCHER),
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--no-extensions",
        "--extension",
        str(REQUEST_LOGGER),
        "--extension",
        str(COMMIT_PROTOCOL),
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
        active_tools("E"),
        "--",
        "Read README.md, then call commit_completion alone with a concise smoke summary. Do not modify files or run tests.",
    ]
    with (output / "events.jsonl").open("w", encoding="utf-8") as stdout, (
        output / "stderr.log"
    ).open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    first = json.loads(request_log.read_text(encoding="utf-8").splitlines()[0])
    payload = first["payload"]
    tool_names = [tool.get("name") or tool.get("function", {}).get("name") for tool in payload["tools"]]
    system_prompt = payload["input"][0]["content"]
    checks = {
        "tool_names_exact": tool_names == active_tools("E").split(","),
        "commit_tool_visible": "commit_completion" in tool_names,
        "commit_prompt_visible": "commit_completion" in system_prompt,
        "completion_rule_visible": "task is complete only after commit_completion" in system_prompt.lower(),
    }
    result = {"schema_version": 1, "tool_names": tool_names, "checks": checks}
    (output / "smoke.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

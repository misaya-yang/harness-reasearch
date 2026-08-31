"""Round 2: the ONE keyed provider smoke for "menu-only gate with text-escape backstop".

R2 = pre-registered fallback of Round 1 (activated by the provider's 400 rejection of the
bare string tool_choice:"required" in the R1 smoke; ledger Round 1/2 sections). The ONLY
change vs the R1 artifact: the gate adds NOTHING to any payload — the armed turn's force
is the exclusive two-tool decision menu itself. All other constants (backstop text,
classifier, budgets, fail-closed paths) are inherited unchanged.

Pre-registered assertions (ledger Round 2 DESIGN-LOCKED 2026-08-30):
  ① G work-state first payload = native 4-tool surface AND system prompt byte-identical
     to the pure-N baseline (v5 N row at the same HEAD 853a80d);
  ②′ after a real synthetic `node_modules/.bin/vitest run` pass is witnessed, the NEXT
     provider request carries exactly {finalize_completion, continue_work} with ZERO
     parameter change: `tool_choice` appears on NO request, and armed non-input params
     equal the work-state non-input params (gate touches nothing but the tool surface);
  ③ HARD: the synthetic armed request itself returns a tool call. Text-escape +
     backstop recovery is recorded separately and, if it is the only path, the smoke
     FAILS — Finding-2 ⑤: backstop promoted to main channel ⇒ freeze refused until the
     re-entry text is rewritten and re-reviewed (rename clause);
  ④ a synthetic finalize_completion on an unchanged workspace reaches a TYPED rejection
     (gaps) without terminating the trajectory;
  ⑤ the re-entry bytes actually queued match the frozen BACKSTOP_TEXT (log hash +
     serialized wire input both checked);
  s6 observe first payload byte-identical to pure N; s7 observer never changes the tool
     surface on any request; s8 observer injects zero model-visible bytes (backstop text
     absent from the whole N wire stream; gate log is witness-only, no arm/continue).

Two Pi processes run FULLY SERIAL (gate arm first, then observe arm). This smoke's first
keyed request consumes Round 2 of the six-round budget even if the run is voided.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from run_pi_termination_control import (
    COMPLETION_GATE,
    CONFIG_TEMPLATE,
    LOW_PRIORITY_PREFIX,
    PI_LAUNCHER,
    PI_REPO,
    REQUEST_LOGGER,
    RESOURCE_ENV,
    active_tools,
)


HERE = Path(__file__).resolve().parent
RESULTS_ROOT = HERE.parent.parent / "results"
# pure-N baseline row: ebcp_paired_v5 ran at HEAD 853a80d with request-logger only.
BASELINE_REQUEST_LOG = (
    RESULTS_ROOT
    / "20260830_pi_ebcp_paired_v5"
    / "pi-custom-message-tool-result-order__N__r1"
    / "model-requests.jsonl"
)
# 锁定注 ②: exact bytes, verbatim.
BACKSTOP_TEXT = (
    "The completion gate is active. Respond with exactly one tool call: "
    "finalize_completion(summary) or continue_work(obligation). "
    "A text-only reply is not recorded as completion."
)
G_PROMPT = (
    "This workspace contains one trivially passing vitest test. Run EXACTLY one shell "
    "command, verbatim, and nothing else: node_modules/.bin/vitest run — with no pipes, "
    "no redirections, and no other commands. Do not edit any file. When your available "
    "tools change to completion tools, call finalize_completion with a one-sentence "
    "summary. If finalize_completion is rejected, call continue_work exactly once with a "
    "short obligation promising to re-read README.md, then read README.md, then call "
    "finalize_completion again."
)
N_PROMPT = (
    "This workspace contains one trivially passing vitest test. Run EXACTLY one shell "
    "command, verbatim, and nothing else: node_modules/.bin/vitest run — with no pipes, "
    "no redirections. Do not edit any file. Then reply with a one-sentence summary of the "
    "test result."
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def build_workspace(root: Path, name: str) -> Path:
    workspace = root / name
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("# Round 2 menu-only smoke workspace\n", encoding="utf-8")
    (workspace / "package.json").write_text(
        json.dumps({"name": "r1-termination-smoke", "private": True, "type": "module"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (workspace / "smoke.test.js").write_text(
        'import { expect, test } from "vitest";\n\ntest("synthetic smoke test passes", () => {\n  expect(2 + 2).toBe(4);\n});\n',
        encoding="utf-8",
    )
    (workspace / "node_modules").symlink_to(PI_REPO / "node_modules")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "README.md", "package.json", "smoke.test.js"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=R1 Smoke", "-c", "user.email=smoke@invalid", "commit", "-q", "-m", "base"],
        cwd=workspace,
        check=True,
    )
    return workspace


def run_phase(
    name: str,
    mode: str,
    condition: str,
    prompt: str,
    workspace: Path,
    root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    phase_dir = root / name
    phase_dir.mkdir()
    agent_dir = phase_dir / "pi-agent"
    shutil.copytree(CONFIG_TEMPLATE, agent_dir)
    request_log = phase_dir / "model-requests.jsonl"
    gate_log = phase_dir / "completion-gate.jsonl"
    env = os.environ.copy()
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        raise RuntimeError("ANTHROPIC_AUTH_TOKEN is missing")
    env.update(
        {
            **RESOURCE_ENV,
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_CODING_AGENT_SESSION_DIR": str(phase_dir / "sessions"),
            "PI_RESEARCH_REQUEST_LOG": str(request_log),
            "PI_COMPLETION_GATE_MODE": mode,
            "PI_RESEARCH_GATE_LOG": str(gate_log),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
    )
    # extension order locked: completion-gate BEFORE request-logger, so the logger
    # records the payload the gate chain actually put on the wire.
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
        active_tools(condition),
        "--",
        prompt,
    ]
    timed_out = False
    with (phase_dir / "events.jsonl").open("w", encoding="utf-8") as stdout, (phase_dir / "stderr.log").open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            command, cwd=workspace, env=env, stdout=stdout, stderr=stderr, start_new_session=True
        )
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    # wait for the process group to be gone before the next serial phase
    for _ in range(12):
        out = subprocess.check_output(["ps", "-Ao", "pid,args"], text=True)
        if not any("pi-test" in line for line in out.splitlines()[1:]):
            break
        time.sleep(5)
    else:
        (phase_dir / "REAP_WARNING.md").write_text(
            "A pi-test process survived 60s after this phase ended; the next serial phase "
            "started under that uncertainty. Treat the smoke evidence with caution.\n",
            encoding="utf-8",
        )
    return {
        "dir": phase_dir,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "requests": load_jsonl(request_log),
        "gate_rows": load_jsonl(gate_log),
        "events": load_jsonl(phase_dir / "events.jsonl"),
    }


def tool_names(payload: dict[str, Any]) -> list[str]:
    return [tool.get("name") or tool.get("function", {}).get("name") for tool in payload.get("tools", [])]


def system_content(payload: dict[str, Any]) -> str:
    for item in payload.get("input", []):
        if item.get("role") == "system":
            content = item.get("content")
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True)
    return ""


def normalize_paths(text: str, workspace: Path) -> str:
    """Byte-identity is defined modulo the absolute workspace path, which Pi's
    system prompt embeds verbatim (verified on the v5 baseline row). Nothing else
    may differ: same HEAD, same model, same day."""
    return text.replace(str(workspace.resolve()), "<WORKSPACE>")


def payload_fingerprint(payload: dict[str, Any], workspace: Path) -> str:
    parts = {
        "tools": normalize_paths(json.dumps(payload.get("tools", []), sort_keys=True), workspace),
        "system": normalize_paths(system_content(payload), workspace),
        "params": json.dumps(
            {k: v for k, v in payload.items() if k not in ("tools", "input", "prompt_cache_key")},
            sort_keys=True,
        ),
    }
    return sha256_text(json.dumps(parts, sort_keys=True))


def dump_for_forensics(output: Path, name: str, payload: dict[str, Any], workspace: Path) -> None:
    (output / f"{name}.json").write_text(
        json.dumps(
            {
                "tools": json.loads(normalize_paths(json.dumps(payload.get("tools", [])), workspace)),
                "system": normalize_paths(system_content(payload), workspace),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180, help="per-phase cap; 180s pre-registered")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error(f"refusing to overwrite {output}")
    if not BASELINE_REQUEST_LOG.exists():
        parser.error(f"pure-N baseline request log missing: {BASELINE_REQUEST_LOG}")
    output.mkdir(parents=True)
    os.environ.update(RESOURCE_ENV)

    baseline_first = json.loads(BASELINE_REQUEST_LOG.read_text(encoding="utf-8").splitlines()[0])
    baseline_payload = baseline_first["payload"]
    baseline_workspace = BASELINE_REQUEST_LOG.parent / "workspace"
    baseline_fingerprint = payload_fingerprint(baseline_payload, baseline_workspace)

    workspace_g = build_workspace(output, "workspace-g")
    workspace_n = build_workspace(output, "workspace-n")
    # G first (locked smoke order), then N observe — never overlapping.
    gate = run_phase("gate", "gate", "G", G_PROMPT, workspace_g, output, args.timeout_seconds)
    observe = run_phase("observe", "observe", "N", N_PROMPT, workspace_n, output, args.timeout_seconds)

    checks: dict[str, bool] = {}
    facts: dict[str, Any] = {
        "backstop_text_sha256": sha256_text(BACKSTOP_TEXT),
        "byte_identity_definition": (
            "sha256 over {tools array, system message, non-input params} after replacing the "
            "absolute workspace path with <WORKSPACE>; prompt_cache_key and user input excluded"
        ),
    }

    # ---- G arm ----
    g_requests = gate["requests"]
    g_gate = gate["gate_rows"]
    g_first = g_requests[0]["payload"] if g_requests else {}
    checks["G_ran_and_completed"] = bool(g_requests) and gate["exit_code"] == 0 and not gate["timed_out"]
    checks["G_work_surface_native_only"] = tool_names(g_first) == ["read", "bash", "edit", "write"]
    g_identity = payload_fingerprint(g_first, workspace_g) == baseline_fingerprint
    checks["G_first_payload_byte_identity_vs_pure_N"] = g_identity
    if not g_identity:
        dump_for_forensics(output, "payload_g_first_normalized", g_first, workspace_g)
        dump_for_forensics(output, "payload_baseline_normalized", baseline_payload, baseline_workspace)
    armed_requests = [r for r in g_requests if set(tool_names(r["payload"])) == {"finalize_completion", "continue_work"}]
    checks["G_armed_surface_decision_only"] = len(armed_requests) >= 1
    # ②′: the gate adds NOTHING to any payload — tool_choice must appear nowhere on the G wire.
    checks["G_no_tool_choice_anywhere_on_wire"] = all(
        "tool_choice" not in r["payload"] for r in g_requests
    )
    armed_ids = {id(r) for r in armed_requests}
    checks["G_work_requests_have_no_tool_choice"] = all(
        "tool_choice" not in r["payload"] for r in g_requests if id(r) not in armed_ids
    )
    # ②′ second half: armed requests differ from the work-state request ONLY in the tools
    # array — every other (non-input) parameter is identical (gate touches nothing else;
    # if Pi itself varies a param per request this fails loudly and we learn about it).
    first_params = (
        {k: v for k, v in g_requests[0]["payload"].items() if k not in ("tools", "input")}
        if g_requests
        else {}
    )
    checks["G_armed_requests_zero_param_change"] = bool(armed_requests) and all(
        {k: v for k, v in r["payload"].items() if k not in ("tools", "input")} == first_params
        for r in armed_requests
    )
    checks["G_decision_schemas_serialized"] = all(
        isinstance(tool.get("parameters") or tool.get("input_schema"), dict)
        for r in armed_requests
        for tool in r["payload"].get("tools", [])
    )
    event_seq = [row.get("event") for row in g_gate]
    first_finalise = next((i for i, e in enumerate(event_seq) if e in {"FINALIZE_ATTEMPT", "OBLIGATION_TEXT"}), None)
    first_escape = next((i for i, e in enumerate(event_seq) if e == "GATE_TEXT_ESCAPE"), None)
    armed_tool_call = first_finalise is not None and (first_escape is None or first_finalise < first_escape)
    # ③ HARD per ledger: the synthetic armed request must itself return a tool call.
    # If only the backstop path produced the decision, that is Finding-2 ⑤ territory:
    # freeze refused until the re-entry text is rewritten and re-reviewed (rename clause).
    checks["G_armed_turn_returns_tool_call_first_try"] = armed_tool_call
    backstop_recovered = (
        first_escape is not None
        and first_finalise is not None
        and first_finalise > first_escape
        and sum(1 for e in event_seq if e == "GATE_TEXT_ESCAPE") <= 2
    )
    facts["G_backstop_fallback_would_have_recovered"] = backstop_recovered
    # ⑤ (d3): the re-entry bytes ACTUALLY QUEUED on the wire must match the frozen text.
    escape_rows = [row for row in g_gate if row.get("event") == "GATE_TEXT_ESCAPE"]
    escape_count = len(escape_rows)
    checks["G_escape_log_content_hash_matches_frozen"] = all(
        row.get("contentSha256") == sha256_text(BACKSTOP_TEXT) for row in escape_rows
    )
    queued_hits = sum(1 for r in g_requests if BACKSTOP_TEXT in json.dumps(r["payload"].get("input", []), ensure_ascii=False))
    checks["G_backstop_queued_bytes_match_frozen"] = queued_hits >= escape_count
    facts["G_backstop_escapes"] = escape_count
    facts["G_requests_carrying_backstop_text"] = queued_hits
    # 锁定注 (c): decision-tool schemas verbatim + SHA256 (wire bytes, for F-C cost accounting)
    if armed_requests:
        armed_tools = armed_requests[0]["payload"].get("tools", [])
        facts["armed_tools_schemas_json"] = armed_tools
        facts["armed_tools_schemas_sha256"] = sha256_text(json.dumps(armed_tools, sort_keys=True))
    facts["G_gate_event_sequence"] = event_seq
    facts["G_gate_violation_events"] = [row for row in g_gate if row.get("event") == "GATE_VIOLATION"]
    rejections = [
        row for row in g_gate if row.get("event") == "finalize_decision" and (row.get("decision") or {}).get("status") == "rejected"
    ]
    rejection_gaps = [list(row["decision"].get("gaps", [])) for row in rejections]
    checks["G_finalize_typed_rejection_no_termination"] = bool(rejections) and all(
        len(gaps) > 0 for gaps in rejection_gaps
    ) and len(g_requests) > 2  # trajectory kept issuing requests past the rejection
    facts["G_rejection_gaps"] = rejection_gaps
    facts["G_finalize_accepted"] = any(
        (row.get("decision") or {}).get("status") == "accepted" for row in g_gate if row.get("event") == "finalize_decision"
    )
    facts["G_model_calls"] = len(g_requests)

    # ---- N observe arm (s6/s7/s8) ----
    n_requests = observe["requests"]
    n_gate = observe["gate_rows"]
    n_first = n_requests[0]["payload"] if n_requests else {}
    checks["N_ran_and_completed"] = bool(n_requests) and observe["exit_code"] == 0 and not observe["timed_out"]
    n_identity = payload_fingerprint(n_first, workspace_n) == baseline_fingerprint
    checks["s6_observe_first_payload_identical_to_pure_N"] = n_identity
    if not n_identity:
        dump_for_forensics(output, "payload_n_first_normalized", n_first, workspace_n)
    checks["s7_observe_never_changes_surface"] = all(
        tool_names(r["payload"]) == ["read", "bash", "edit", "write"] for r in n_requests
    )
    serialized_stream = json.dumps([r["payload"] for r in n_requests], ensure_ascii=False)
    checks["s8_backstop_and_gate_text_absent"] = (
        BACKSTOP_TEXT not in serialized_stream
        and "finalize_completion" not in serialized_stream
        and "continue_work" not in serialized_stream
    )
    # observe registers only before_agent_start + tool_result: its legal event vocabulary is
    # exactly mode/baseline/OBSERVE_WITNESS (agent_end/message_end/before_provider_request are
    # gate-mode-only registrations; F13 forbids tool_call & followUps in observe by design).
    n_events = {row.get("event") for row in n_gate}
    checks["s8_observer_witness_only"] = bool(n_events) and n_events <= {"mode", "baseline", "OBSERVE_WITNESS"}
    facts["N_observe_witness_ready"] = any(row.get("event") == "OBSERVE_WITNESS" and row.get("ready") for row in n_gate)
    facts["N_gate_event_names"] = sorted(str(e) for e in n_events)
    facts["N_model_calls"] = len(n_requests)

    passed = all(checks.values())
    smoke = {
        "schema_version": 1,
        "round_consumption_note": "first keyed request of the gate phase consumed Round 2 (2/6) regardless of this verdict; R1 (1/6) was consumed by the failed forced-tool_choice smoke",
        "passed": passed,
        "checks": checks,
        "facts": facts,
        "baseline": str(BASELINE_REQUEST_LOG),
        "phases": {"gate": str(gate["dir"]), "observe": str(observe["dir"])},
    }
    (output / "smoke.json").write_text(json.dumps(smoke, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

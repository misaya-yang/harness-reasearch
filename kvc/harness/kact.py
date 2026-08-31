"""KAC layer: fresh-context checkpoint probes and decision-card injection.

When a deterministic trigger (T1/T2/T3) fires on the actor, the controller
spawns an independent fresh-context probe subprocess (its own KvcRunner, its
own empty workspace, read-only tool surface). The probe sees ONLY: the
original task, bounded current source, the diff vs base, external
observations, and the GPS — never the actor's own hypotheses. It must answer
with a strict JSON decision card, which is steered into the actor
replace-in-place. A probe never blocks or kills the actor: budget 120s, one
probe at a time, failures recorded and swallowed.

Frozen constants below are part of the experimental protocol.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from kvc.harness.kvc_run import KvcRunner, RunConfig, _fired_key, last_assistant_text

PROBE_BUDGET_SECONDS = 120.0
SOURCES_BUDGET_BYTES = 96 * 1024
DIFF_BUDGET_BYTES = 48 * 1024
RECENT_READS = 6
REQUIRED_CARD_KEYS = ("invariant", "edit_surface", "minimal_change", "falsifier", "next_action")
VALID_NEXT_ACTIONS = ("mutate", "probe", "deliver")

DELIVERY_PRESSURE_TEXT = (
    "The current source state has post-mutation validation evidence. "
    "Continue only if you can name: 1. an explicit unresolved user requirement, "
    "and 2. one bounded test whose result could change the patch. Otherwise deliver now."
)


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, timeout=30
    )
    return result.stdout


def collect_probe_inputs_from_state(
    workspace: Path,
    events_path: Path,
    gps_render: str,
    read_paths: list[str],
    task: dict[str, Any],
) -> dict[str, Any]:
    """Bounded probe context built from machine state only.

    Runner-free core so trigger-time forks can build the identical probe
    context from a frozen snapshot (no live actor required).
    """
    # Diff vs the workspace's root (benchmark base) commit, not HEAD: passing
    # validations are committed as incumbents, so vs-HEAD would hide the patch.
    roots = [r for r in _git(workspace, "rev-list", "--max-parents=0", "HEAD").split() if r]
    base_ref = roots[-1] if roots else "HEAD"
    diff = _git(workspace, "diff", base_ref)
    if len(diff.encode()) > DIFF_BUDGET_BYTES:
        diff = diff.encode()[:DIFF_BUDGET_BYTES].decode(errors="ignore") + "\n...[diff truncated]"
    changed: list[str] = []
    for line in _git(workspace, "status", "--porcelain").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename entry: keep the new path
            path = path.split(" -> ")[-1]
        changed.append(path)
    # Files already committed on top of the base (incumbents, agent commits).
    changed += [
        p.strip()
        for p in _git(workspace, "diff", "--name-only", base_ref).splitlines()
        if p.strip()
    ]
    sources_parts: list[str] = []
    budget = SOURCES_BUDGET_BYTES
    included: set[str] = set()
    candidates = list(changed)
    for path in read_paths[-RECENT_READS:]:
        try:
            rel = str(Path(path).resolve().relative_to(workspace.resolve()))
        except ValueError:
            continue
        if rel not in candidates:
            candidates.append(rel)
    for rel in candidates:
        target = workspace / rel
        if not target.is_file() or rel in included:
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunk = f"\n## file: {rel}\n\n{content}\n"
        if len(chunk.encode()) > budget:
            chunk = chunk.encode()[:budget].decode(errors="ignore") + "\n...[truncated]"
        sources_parts.append(chunk)
        included.add(rel)
        budget -= len(chunk.encode())
        if budget <= 0:
            break

    counterexamples: list[str] = []
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "kvc_validation" and frame.get("counterexample"):
                counterexamples.append(str(frame["counterexample"]))
    # Fallback when nothing was changed or read yet: give the probe the repo
    # file index so it can localize without any tool access of its own.
    if not candidates:
        index = _git(workspace, "ls-files")
        lines = index.splitlines()
        if len(lines) > 400:
            lines = lines[:400] + [f"...[{len(lines) - 400} more files]"]
        sources_parts.append("\n## repository file index (base tree)\n\n" + "\n".join(lines) + "\n")

    observations = ["Test commands the solution must satisfy:"]
    observations.extend(f"  - {command}" for command in task.get("test_commands", []))
    if counterexamples:
        observations.append("Validation counterexamples observed so far:")
        observations.extend(f"  - {example}" for example in counterexamples)
    else:
        observations.append("No validation has been run in this session yet.")

    return {
        "task_prompt": task["prompt"],
        "gps_render": gps_render,
        "diff": diff or "(no changes yet)",
        "sources": "".join(sources_parts) or "(no source collected)",
        "observations": "\n".join(observations),
    }


def collect_probe_inputs(runner: KvcRunner, task: dict[str, Any]) -> dict[str, Any]:
    """Live-actor wrapper: gather state from the running KvcRunner."""
    return collect_probe_inputs_from_state(
        workspace=runner.config.workspace,
        events_path=runner.config.run_dir / "events" / "events.jsonl",
        gps_render=runner.gps.render(),
        read_paths=runner.read_paths,
        task=task,
    )


def parse_card(text: str) -> dict[str, str] | None:
    """Extract the first balanced JSON object with all required card keys."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if (
                        isinstance(candidate, dict)
                        and all(key in candidate for key in REQUIRED_CARD_KEYS)
                        and candidate.get("next_action") in VALID_NEXT_ACTIONS
                    ):
                        return {key: str(candidate[key]) for key in REQUIRED_CARD_KEYS}
                    break
        start = text.find("{", start + 1)
    return None


def format_card_steer(key: str, card: dict[str, str], trigger: str) -> str:
    lines = [
        f"[KAC-CARD {key}] Fresh-context checkpoint decision card. "
        "This card replaces any earlier card; act on this one only.",
        f"invariant: {card['invariant']}",
        f"edit_surface: {card['edit_surface']}",
        f"minimal_change: {card['minimal_change']}",
        f"falsifier: {card['falsifier']}",
        f"next_action: {card['next_action']}",
    ]
    if trigger == "T3":
        lines.append("")
        lines.append(DELIVERY_PRESSURE_TEXT)
    return "\n".join(lines)


class KacController:
    """TriggerHook implementation: fire a probe per trigger, steer the card."""

    def __init__(self, actor_config: RunConfig, task: dict[str, Any], template_text: str):
        self.actor_config = actor_config
        self.task = task
        self.template_text = template_text
        self.probes_root = actor_config.run_dir / "probes"
        self.probes_root.mkdir(parents=True, exist_ok=True)
        (self.probes_root / "prompt.sha256").write_text(
            _sha256(template_text) + "\n", encoding="utf-8"
        )
        self._busy = threading.Event()
        self._cards: list[dict[str, Any]] = []

    def __call__(self, runner: KvcRunner, trigger: str) -> None:
        # Runs on the actor's reader thread: never block it.
        threading.Thread(
            target=self._probe_and_inject, args=(runner, trigger), daemon=True
        ).start()

    # ------------------------------------------------------------------ probe

    def _probe_and_inject(self, runner: KvcRunner, trigger: str) -> None:
        key = _fired_key(trigger, runner.gps)
        probe_slug = key.replace("@", "-")
        if self._busy.is_set():
            runner._record({"type": "kvc_probe_skipped", "key": key, "reason": "probe already running"})
            return
        self._busy.set()
        started = time.monotonic()
        try:
            inputs = collect_probe_inputs(runner, self.task)
            probe_dir = self.probes_root / probe_slug
            probe_dir.mkdir(parents=True, exist_ok=True)
            (probe_dir / "probe-input.json").write_text(
                json.dumps(inputs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            prompt = self.template_text.format(**inputs)
            card, probe_report = self._run_probe(key, probe_dir, prompt)
            probe_report.update(
                {
                    "type": "kvc_probe_result",
                    "key": key,
                    "trigger": trigger,
                    "duration_seconds": round(time.monotonic() - started, 1),
                }
            )
            runner._record(probe_report)
            if card is None:
                return
            steer_text = format_card_steer(key, card, trigger)
            if runner._kill_requested.is_set():
                # Late trigger: the probe outlived the actor's budget. The
                # card is recorded for analysis but cannot be injected.
                accepted = False
                note = "actor terminated before injection"
            else:
                accepted = runner.steer(steer_text)
                note = ""
            entry = {
                "key": key,
                "trigger": trigger,
                "card": card,
                "accepted": accepted,
                "note": note,
                "injected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._cards.append(entry)
            with (runner.config.run_dir / "state" / "cards.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # replace-in-place: single current-card slot, overwritten each time
            (runner.config.run_dir / "state" / "current_card.json").write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            runner._record({"type": "kvc_card_injected", "key": key, "accepted": accepted})
        except Exception as error:  # probe failures must never harm the actor
            runner._record({"type": "kvc_probe_error", "key": key, "trigger": trigger, "error": repr(error)})
        finally:
            self._busy.clear()

    def _run_probe(
        self, key: str, probe_dir: Path, prompt: str
    ) -> tuple[dict[str, str] | None, dict[str, Any]]:
        return run_probe(self.actor_config, key, probe_dir, prompt)


def run_probe(
    cfg: RunConfig, key: str, probe_dir: Path, prompt: str
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Fresh-context probe subprocess (module-level so trigger-time forks can
    reuse the exact same probe machinery from a frozen snapshot)."""
    probe_ws = probe_dir / "workspace"
    probe_ws.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=probe_ws, check=True, timeout=30)
    (probe_ws / "probe.txt").write_text("fresh-context probe workspace\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=probe_ws, check=True, timeout=30)
    subprocess.run(
        ["git", "-c", "user.name=KVC Probe", "-c", "user.email=kvc-probe@invalid",
         "commit", "-q", "-m", "probe base"],
        cwd=probe_ws, check=True, timeout=30,
    )
    from kvc.harness.gps import TriggerConfig

    # Probes must not fire their own triggers: give them inert thresholds.
    probe_config = dataclasses.replace(
        cfg,
        workspace=probe_ws,
        run_dir=probe_dir / "run",
        task_prompt=prompt,
        objective_anchor=f"KAC checkpoint probe {key}",
        # No tools: the probe's evidence is complete in the prompt. With a
        # tool surface the model explores the empty probe workspace until
        # budget instead of answering (round-1 r1: 191 events, 0 output).
        tools=(),
        extensions=(),
        budget_seconds=PROBE_BUDGET_SECONDS,
        validator_command=None,
        validator_task=None,
        trigger_config=TriggerConfig(
            no_mutation_budget_ratio=float("inf"), post_pass_tool_calls=10**9
        ),
    )
    probe_runner = KvcRunner(probe_config)
    probe_runner.start()
    try:
        outcome = probe_runner.run_prompt(prompt)
        messages_response = probe_runner.send_command({"type": "get_messages"}, timeout=15.0)
    finally:
        if probe_runner._proc and probe_runner._proc.poll() is None:
            probe_runner.finish()
    assistant_text = ""
    for message in (messages_response or {}).get("data", {}).get("messages", []):
        if message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        if isinstance(content, list):
            text = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        else:
            text = str(content)
        if text.strip():
            assistant_text = text
    if not assistant_text.strip():
        # RPC get_messages can come back empty after settle; the agent_end
        # frame in the event log carries the full transcript (see kvc_run).
        assistant_text = last_assistant_text(probe_dir / "run")
    (probe_dir / "probe-output.txt").write_text(assistant_text, encoding="utf-8")
    card = parse_card(assistant_text)
    report = {
        "probe_reason": outcome.reason,
        "probe_output_chars": len(assistant_text),
        "card_parsed": card is not None,
    }
    return card, report


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()

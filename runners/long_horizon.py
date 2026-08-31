"""B0–B6 long-horizon pilot runner over a deterministic mock environment."""

from __future__ import annotations

import argparse
import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from .belief_probe import _json_candidates, load_tasks
from .client import ResponsesClient, ResponsesError
from .config import ConfigError, ExperimentConfig, ProviderConfig, load_config
from .mock_env import MockEnvironment
from .trace import append_jsonl, trace_event


CONDITIONS = {"B0", "B1", "B2", "B3", "B4", "B5", "B6"}


def _parse_step(text: str) -> dict[str, Any]:
    for candidate in _json_candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("model response did not contain a JSON object")


def _ledger_text(ledger: dict[str, list[dict[str, Any]]], *, provenance: bool) -> str:
    sections: list[str] = []
    for key in ("facts", "observations", "hypotheses", "unresolved", "decisions"):
        rows = ledger.get(key, [])
        if not rows:
            continue
        rendered = []
        for row in rows:
            rendered.append(json.dumps(row if provenance else {"content": row.get("content")}, ensure_ascii=False))
        sections.append(f"{key.upper()}:\n- " + "\n- ".join(rendered))
    return "\n".join(sections) or "(empty belief ledger)"


def build_step_prompt(
    task: dict[str, Any],
    env: MockEnvironment,
    condition: str,
    step: int,
    history: list[dict[str, Any]],
    ledger: dict[str, list[dict[str, Any]]],
) -> str:
    state = json.dumps(env.public_state(), ensure_ascii=False, indent=2)
    common = f"""You are operating in a deterministic research environment.
Goal: {task['goal']}
Candidate explanations:
{json.dumps(task.get('hypotheses', {}), ensure_ascii=False, indent=2)}
Current external state:
{state}
Current step: {step}
"""
    if condition == "B3":
        context = "Use only the goal and current external state. Ignore prior model narrative."
    else:
        context = f"Prior trajectory events:\n{json.dumps(history[-8:], ensure_ascii=False, indent=2)}"
    if condition in {"B4", "B5", "B6"}:
        context += f"\nBelief ledger:\n{_ledger_text(ledger, provenance=condition in {'B5', 'B6'})}"
    if condition == "B1" and step > 0 and step % 2 == 0:
        context += "\nPERIODIC REFLECTION: review the current route before choosing the next action."
    if condition == "B6":
        context += "\nWhen uncertain, prefer the smallest available tool action that discriminates hypotheses."
    return f"""{common}
{context}

Choose at most one tool action or finish. Tool actions are observations, not proof
that the overall goal is correct. Do not call irreversible tools in this pilot.
Return exactly one JSON object:
{{
  "action": {{"tool": "tool_name", "args": {{}}}},
  "belief_update": {{"facts": [], "hypotheses": [], "unresolved": [], "decisions": []}},
  "final": null
}}
To finish, set action to null and final to {{"choice": "A|B|C", "confidence": 0.0, "rationale": "..."}}.
Only use tools listed in current external state. Keep hypotheses separate from facts.
"""


def _update_ledger(ledger: dict[str, list[dict[str, Any]]], update: Any, step: int) -> None:
    if not isinstance(update, dict):
        return
    for key in ("facts", "observations", "hypotheses", "unresolved", "decisions"):
        values = update.get(key, [])
        if not isinstance(values, list):
            continue
        for value in values:
            target_key = "hypotheses" if key in {"facts", "observations"} else key
            item = {
                "content": str(value),
                "source": "model",
                "status": "active" if target_key == "decisions" else "unverified",
                "step": step,
            }
            if target_key != key:
                item["claimed_type"] = key
            ledger.setdefault(target_key, []).append(item)


def build_verifier_prompt(
    task: dict[str, Any],
    env: MockEnvironment,
    history: list[dict[str, Any]],
    ledger: dict[str, list[dict[str, Any]]],
) -> str:
    """Give the verifier the executor claims without evaluator-only truth."""

    return f"""Review the executor's current route for unsupported commitments.
Goal: {task['goal']}
Current external state:
{json.dumps(env.public_state(), ensure_ascii=False, indent=2)}
Recent executor trajectory:
{json.dumps(history[-8:], ensure_ascii=False, indent=2)}
Current belief ledger:
{_ledger_text(ledger, provenance=True)}
Return JSON with concerns and one discriminating available-tool suggestion. Do not invent evidence.
"""


def _accumulate_usage(
    totals: dict[str, float], usage: dict[str, Any], prefix: str = ""
) -> None:
    for key, value in usage.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, (int, float)):
            totals[name] += float(value)
        elif isinstance(value, dict):
            _accumulate_usage(totals, value, name)


def run_long_horizon(
    provider: ProviderConfig,
    experiment: ExperimentConfig,
    *,
    conditions: tuple[str, ...] | None = None,
    replicates: int | None = None,
    limit: int | None = None,
    max_steps: int = 8,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    selected_conditions = conditions or experiment.conditions
    invalid = set(selected_conditions) - CONDITIONS
    if invalid:
        raise ConfigError(f"unknown long-horizon conditions: {sorted(invalid)}")
    tasks = load_tasks(experiment.dataset)
    effective_limit = limit if limit is not None else experiment.max_tasks
    if effective_limit is not None:
        tasks = tasks[:effective_limit]
    effective_replicates = replicates if replicates is not None else experiment.replicates
    if effective_replicates <= 0 or max_steps <= 0:
        raise ConfigError("replicates and max_steps must be positive")

    run_id = uuid.uuid4().hex
    client = ResponsesClient(provider, dry_run=dry_run)
    occupied = [path for path in (experiment.trace_path, experiment.result_path) if path.exists()]
    if occupied:
        raise ConfigError(
            "refusing to overwrite existing run artifacts: "
            + ", ".join(str(path) for path in occupied)
        )
    for task in tasks:
        for replicate in range(1, effective_replicates + 1):
            for condition in selected_conditions:
                env = MockEnvironment(task)
                history: list[dict[str, Any]] = []
                ledger: dict[str, list[dict[str, Any]]] = {key: [] for key in ("facts", "observations", "hypotheses", "unresolved", "decisions")}
                final: dict[str, Any] | None = None
                status = "MAX_STEPS"
                steps_executed = 0
                usage_totals: dict[str, float] = defaultdict(float)
                latency_ms_total = 0.0
                executor_calls = 0
                verifier_calls = 0
                verifier_failures = 0
                run_error: str | None = None
                append_jsonl(
                    experiment.trace_path,
                    trace_event(
                        run_id=run_id,
                        task_id=task["task_id"],
                        condition=condition,
                        replicate=replicate,
                        model=provider.model,
                        step=0,
                        event_type="initial_state",
                        source="harness",
                        content=str(task["initial_state"]),
                        is_external_evidence=True,
                        belief_state=ledger,
                    ),
                )
                for step in range(max_steps):
                    steps_executed = step + 1
                    prompt = build_step_prompt(task, env, condition, step, history, ledger)
                    verifier_output = None
                    if condition == "B2" and step > 0 and step % 2 == 0 and not dry_run:
                        verifier_prompt = build_verifier_prompt(task, env, history, ledger)
                        verifier_calls += 1
                        try:
                            verifier = client.complete(verifier_prompt, metadata={"role": "verifier", "task_id": str(task["task_id"])})
                            _accumulate_usage(usage_totals, verifier.usage)
                            latency_ms_total += verifier.latency_ms
                            verifier_output = verifier.output_text
                            history.append({"step": step, "verifier": verifier_output})
                            append_jsonl(
                                experiment.trace_path,
                                trace_event(
                                    run_id=run_id,
                                    task_id=task["task_id"],
                                    condition=condition,
                                    replicate=replicate,
                                    model=provider.model,
                                    step=step,
                                    event_type="verifier_response",
                                    source="model",
                                    content=verifier.output_text,
                                    is_external_evidence=False,
                                    belief_state={},
                                    token_usage=verifier.usage,
                                    latency_ms=verifier.latency_ms,
                                    request_id=verifier.request_id,
                                ),
                            )
                        except ResponsesError as exc:
                            verifier_failures += 1
                            history.append({"event": "verifier_error", "error": type(exc).__name__})
                            append_jsonl(
                                experiment.trace_path,
                                trace_event(
                                    run_id=run_id,
                                    task_id=task["task_id"],
                                    condition=condition,
                                    replicate=replicate,
                                    model=provider.model,
                                    step=step,
                                    event_type="verifier_error",
                                    source="harness",
                                    content=type(exc).__name__,
                                    is_external_evidence=False,
                                    belief_state=ledger,
                                ),
                            )
                    if verifier_output:
                        prompt += f"\nVerifier feedback from a separate call:\n{verifier_output}"
                    append_jsonl(
                        experiment.trace_path,
                        trace_event(
                            run_id=run_id,
                            task_id=task["task_id"],
                            condition=condition,
                            replicate=replicate,
                            model=provider.model,
                            step=step,
                            event_type="prompt",
                            source="harness",
                            content=prompt,
                            is_external_evidence=False,
                            belief_state=ledger,
                        ),
                    )
                    try:
                        executor_calls += 1
                        response = client.complete(prompt, metadata={"role": "executor", "task_id": str(task["task_id"]), "condition": condition})
                        _accumulate_usage(usage_totals, response.usage)
                        latency_ms_total += response.latency_ms
                        if dry_run:
                            status = "DRY_RUN"
                            break
                        parsed = _parse_step(response.output_text)
                        _update_ledger(ledger, parsed.get("belief_update"), step)
                        history.append({"step": step, "model": parsed})
                        append_jsonl(
                            experiment.trace_path,
                            trace_event(
                                run_id=run_id,
                                task_id=task["task_id"],
                                condition=condition,
                                replicate=replicate,
                                model=provider.model,
                                step=step,
                                event_type="model_step",
                                source="model",
                                content=response.output_text,
                                is_external_evidence=False,
                                belief_state=ledger,
                                token_usage=response.usage,
                                latency_ms=response.latency_ms,
                                request_id=response.request_id,
                            ),
                        )
                        action = parsed.get("action")
                        if isinstance(action, dict) and isinstance(action.get("tool"), str):
                            tool = action["tool"]
                            append_jsonl(
                                experiment.trace_path,
                                trace_event(
                                    run_id=run_id,
                                    task_id=task["task_id"],
                                    condition=condition,
                                    replicate=replicate,
                                    model=provider.model,
                                    step=step,
                                    event_type="tool_call",
                                    source="model",
                                    content=json.dumps(action, ensure_ascii=False),
                                    is_external_evidence=False,
                                    belief_state=ledger,
                                    tool={"name": tool},
                                ),
                            )
                            observation = env.invoke(tool, action.get("args") if isinstance(action.get("args"), dict) else {})
                            history.append({"step": step, "tool": tool, "observation": observation.text})
                            ledger["observations"].append({"content": observation.text, "source": "tool", "tool": tool, "status": "observed", "step": step})
                            append_jsonl(
                                experiment.trace_path,
                                trace_event(
                                    run_id=run_id,
                                    task_id=task["task_id"],
                                    condition=condition,
                                    replicate=replicate,
                                    model=provider.model,
                                    step=step,
                                    event_type="tool_observation",
                                    source="tool",
                                    content=observation.text,
                                    is_external_evidence=True,
                                    belief_state=ledger,
                                    tool={"name": tool, "supports": list(observation.supports)},
                                ),
                            )
                        if isinstance(parsed.get("final"), dict):
                            final = parsed["final"]
                            status = "COMPLETED"
                            append_jsonl(
                                experiment.trace_path,
                                trace_event(
                                    run_id=run_id,
                                    task_id=task["task_id"],
                                    condition=condition,
                                    replicate=replicate,
                                    model=provider.model,
                                    step=step,
                                    event_type="final",
                                    source="model",
                                    content=json.dumps(final, ensure_ascii=False),
                                    is_external_evidence=False,
                                    belief_state=ledger,
                                ),
                            )
                            break
                    except (ResponsesError, ValueError) as exc:
                        status = "ERROR"
                        run_error = type(exc).__name__
                        history.append({"event": "error", "error": type(exc).__name__})
                        append_jsonl(
                            experiment.trace_path,
                            trace_event(
                                run_id=run_id,
                                task_id=task["task_id"],
                                condition=condition,
                                replicate=replicate,
                                model=provider.model,
                                step=step,
                                event_type="error",
                                source="harness",
                                content=type(exc).__name__,
                                is_external_evidence=False,
                                belief_state=ledger,
                            ),
                        )
                        break
                evaluation = {"success": False, "status": status}
                if not dry_run:
                    evaluation = env.evaluate(final)
                    evaluation["status"] = status
                append_jsonl(
                    experiment.result_path,
                    {
                        "run_id": run_id,
                        "task_id": task["task_id"],
                        "domain": task.get("domain"),
                        "condition": condition,
                        "replicate": replicate,
                        "provider": provider.name,
                        "model": provider.model,
                        "status": status,
                        "steps": steps_executed,
                        "final": final,
                        "evaluation": evaluation,
                        "usage": dict(sorted(usage_totals.items())),
                        "latency_ms": latency_ms_total,
                        "executor_calls": executor_calls,
                        "verifier_calls": verifier_calls,
                        "verifier_failures": verifier_failures,
                        "tool_calls": len(env.actions),
                        "error": run_error,
                    },
                )
    return experiment.trace_path, experiment.result_path


def _parse_conditions(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/experiment.long_horizon.json")
    parser.add_argument("--conditions", type=_parse_conditions)
    parser.add_argument("--replicates", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        provider, experiment = load_config(args.config)
        trace_path, result_path = run_long_horizon(
            provider,
            experiment,
            conditions=args.conditions,
            replicates=args.replicates,
            limit=args.limit,
            max_steps=args.max_steps,
            dry_run=args.dry_run,
        )
    except (ConfigError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"trace={trace_path}")
    print(f"results={result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

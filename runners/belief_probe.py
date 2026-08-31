"""C0–C6 controlled probe for self-generated hypothesis conditioning."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Any

from .client import ResponsesClient, ResponsesError
from .config import ConfigError, ExperimentConfig, ProviderConfig, load_config
from .trace import append_jsonl, trace_event


CONDITIONS = {"C0", "C1", "C2", "C3", "C4", "C5", "C6"}


def load_tasks(path: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
                raise ValueError(f"invalid task row at {path}:{line_number}")
            tasks.append(row)
    if not tasks:
        raise ValueError(f"dataset is empty: {path}")
    return tasks


def _condition_framing(task: dict[str, Any], condition: str) -> str:
    target = str(task["target_hypothesis"])
    target_text = str(task["options"][target])
    if condition == "C0":
        return "No prior hypothesis is supplied."
    if condition == "C1":
        return f"Earlier, you hypothesized {target}: {target_text}."
    if condition == "C2":
        return f"Earlier, you considered {target} the leading explanation: {target_text}."
    if condition == "C3":
        return f"Another agent hypothesized {target}: {target_text}."
    if condition == "C4":
        return f"UNVERIFIED HYPOTHESIS: {target}: {target_text}."
    if condition == "C5":
        return f"FACT: {target}: {target_text}."
    if condition == "C6":
        options = task["options"]
        preserved = "; ".join(f"{key}: plausible ({value})" for key, value in options.items())
        return f"Multiple hypotheses are preserved: {preserved}. No hypothesis has been verified."
    raise ValueError(f"unknown condition: {condition}")


def build_prompt(task: dict[str, Any], condition: str) -> str:
    """Build a condition prompt without exposing evaluator-only ground truth."""

    evidence = "\n".join(f"- {item}" for item in task["evidence"])
    options = "\n".join(f"{key}. {value}" for key, value in task["options"].items())
    return f"""You are participating in a controlled belief-maintenance experiment.

Question:
{task['question']}

Raw evidence:
{evidence}

Candidate explanations:
{options}

Condition framing:
{_condition_framing(task, condition)}

The condition framing is not new external evidence. Do not invent observations.
Return exactly one JSON object and no markdown:
{{
  "choice": "A",
  "confidence": 0.0,
  "alternatives": {{"A": 0.0, "B": 0.0, "C": 0.0}},
  "needs_more_evidence": true
}}
Use relative confidence values in [0, 1]. Keep plausible alternatives represented.
"""


def _json_candidates(text: str) -> list[str]:
    candidates = [text.strip()]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(fenced)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


def parse_structured_response(text: str, option_ids: set[str]) -> dict[str, Any]:
    """Parse and minimally validate the requested response schema."""

    parsed: Any = None
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(parsed, dict):
        raise ValueError("response did not contain a JSON object")

    choice = parsed.get("choice")
    confidence = parsed.get("confidence")
    alternatives = parsed.get("alternatives")
    needs_more_evidence = parsed.get("needs_more_evidence")
    if choice not in option_ids:
        raise ValueError("choice is not one of the task options")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be a number in [0, 1]")
    if not isinstance(alternatives, dict):
        raise ValueError("alternatives must be an object")
    clean_alternatives: dict[str, float] = {}
    if set(alternatives) != option_ids:
        raise ValueError("alternatives must contain every task option exactly once")
    for key, value in alternatives.items():
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ValueError("alternative beliefs must be numbers in [0, 1]")
        clean_alternatives[key] = float(value)
    if not isinstance(needs_more_evidence, bool):
        raise ValueError("needs_more_evidence must be boolean")
    return {
        "choice": choice,
        "confidence": float(confidence),
        "alternatives": clean_alternatives,
        "needs_more_evidence": needs_more_evidence,
    }


def _target_belief(parsed: dict[str, Any], target: str) -> float | None:
    alternatives = parsed.get("alternatives", {})
    if isinstance(alternatives, dict) and isinstance(alternatives.get(target), (int, float)):
        return float(alternatives[target])
    if parsed.get("choice") == target and isinstance(parsed.get("confidence"), (int, float)):
        return float(parsed["confidence"])
    return None


def _result_row(
    *,
    run_id: str,
    task: dict[str, Any],
    condition: str,
    replicate: int,
    provider: ProviderConfig,
    status: str,
    parsed: dict[str, Any] | None = None,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    parsed = parsed or {}
    return {
        "run_id": run_id,
        "task_id": task["task_id"],
        "domain": task.get("domain"),
        "condition": condition,
        "replicate": replicate,
        "provider": provider.name,
        "model": provider.model,
        "choice": parsed.get("choice"),
        "confidence": parsed.get("confidence"),
        "alternatives": parsed.get("alternatives", {}),
        "target_hypothesis": task.get("target_hypothesis"),
        "target_belief": _target_belief(parsed, str(task.get("target_hypothesis")))
        if parsed
        else None,
        "ground_truth": task.get("ground_truth"),
        "needs_more_evidence": parsed.get("needs_more_evidence"),
        "status": status,
        "error": error,
        "usage": usage or {},
        "latency_ms": latency_ms,
    }


def run_probe(
    provider: ProviderConfig,
    experiment: ExperimentConfig,
    *,
    conditions: tuple[str, ...] | None = None,
    replicates: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    selected_conditions = conditions or experiment.conditions
    invalid = set(selected_conditions) - CONDITIONS
    if invalid:
        raise ConfigError(f"unknown belief-probe conditions: {sorted(invalid)}")
    task_rows = load_tasks(experiment.dataset)
    effective_limit = limit if limit is not None else experiment.max_tasks
    if effective_limit is not None:
        task_rows = task_rows[:effective_limit]
    effective_replicates = replicates if replicates is not None else experiment.replicates
    if effective_replicates <= 0:
        raise ConfigError("replicates must be positive")

    run_id = uuid.uuid4().hex
    client = ResponsesClient(provider, dry_run=dry_run)
    occupied = [path for path in (experiment.trace_path, experiment.result_path) if path.exists()]
    if occupied:
        raise ConfigError(
            "refusing to overwrite existing run artifacts: "
            + ", ".join(str(path) for path in occupied)
        )
    for task in task_rows:
        option_ids = set(task["options"])
        for replicate in range(1, effective_replicates + 1):
            for condition in selected_conditions:
                prompt = build_prompt(task, condition)
                append_jsonl(
                    experiment.trace_path,
                    trace_event(
                        run_id=run_id,
                        task_id=task["task_id"],
                        condition=condition,
                        replicate=replicate,
                        model=provider.model,
                        step=0,
                        event_type="task_evidence",
                        source="harness",
                        content=json.dumps(task["evidence"], ensure_ascii=False),
                        is_external_evidence=True,
                        belief_state={},
                    ),
                )
                append_jsonl(
                    experiment.trace_path,
                    trace_event(
                        run_id=run_id,
                        task_id=task["task_id"],
                        condition=condition,
                        replicate=replicate,
                        model=provider.model,
                        step=0,
                        event_type="prompt",
                        source="harness",
                        content=prompt,
                        is_external_evidence=False,
                        belief_state={"condition": condition},
                    ),
                )
                try:
                    response = client.complete(
                        prompt,
                        metadata={
                            "experiment": "belief_probe",
                            "task_id": str(task["task_id"]),
                            "condition": condition,
                            "replicate": str(replicate),
                        },
                    )
                    parsed = None if dry_run else parse_structured_response(response.output_text, option_ids)
                    status = "DRY_RUN" if dry_run else "OK"
                    append_jsonl(
                        experiment.trace_path,
                        trace_event(
                            run_id=run_id,
                            task_id=task["task_id"],
                            condition=condition,
                            replicate=replicate,
                            model=provider.model,
                            step=1,
                            event_type="response",
                            source="model",
                            content=response.output_text,
                            is_external_evidence=False,
                            belief_state=parsed or {},
                            token_usage=response.usage,
                            parent_ids=[],
                            latency_ms=response.latency_ms,
                            request_id=response.request_id,
                        ),
                    )
                    append_jsonl(
                        experiment.result_path,
                        _result_row(
                            run_id=run_id,
                            task=task,
                            condition=condition,
                            replicate=replicate,
                            provider=provider,
                            status=status,
                            parsed=parsed,
                            usage=response.usage,
                            latency_ms=response.latency_ms,
                        ),
                    )
                except (ResponsesError, ValueError) as exc:
                    error_name = type(exc).__name__
                    append_jsonl(
                        experiment.trace_path,
                        trace_event(
                            run_id=run_id,
                            task_id=task["task_id"],
                            condition=condition,
                            replicate=replicate,
                            model=provider.model,
                            step=1,
                            event_type="error",
                            source="harness",
                            content=error_name,
                            is_external_evidence=False,
                            belief_state={},
                        ),
                    )
                    append_jsonl(
                        experiment.result_path,
                        _result_row(
                            run_id=run_id,
                            task=task,
                            condition=condition,
                            replicate=replicate,
                            provider=provider,
                            status="ERROR",
                            error=error_name,
                        ),
                    )
    return experiment.trace_path, experiment.result_path


def _parse_conditions(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/experiment.default.json")
    parser.add_argument("--conditions", type=_parse_conditions)
    parser.add_argument("--replicates", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        provider, experiment = load_config(args.config)
        trace_path, result_path = run_probe(
            provider,
            experiment,
            conditions=args.conditions,
            replicates=args.replicates,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except (ConfigError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"trace={trace_path}")
    print(f"results={result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

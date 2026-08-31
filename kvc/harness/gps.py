"""GPS: harness-side deterministic progress state machine.

Machine facts only. The model can never write, confirm, or modify any GPS
field. GPS is rendered as a compact block and injected only at trigger
moments (see DESIGN.md section 3.1); it is never broadcast per turn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationRecord:
    epoch: int
    scope: str
    result: str  # "pass" | "fail"
    counterexample: str | None = None
    at_monotonic: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "scope": self.scope,
            "result": self.result,
            "counterexample": self.counterexample,
        }


@dataclass
class GpsState:
    objective_anchor: str
    budget_seconds: float
    start_monotonic: float = field(default_factory=time.monotonic)
    mutation_epoch: int = 0
    validation: ValidationRecord | None = None
    incumbent_validated_epoch: int | None = None
    delivered: bool = False
    # Tool calls executed after the latest passing validation (T3 trigger fuel).
    tool_calls_since_pass: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.start_monotonic

    def remaining(self) -> float:
        return max(0.0, self.budget_seconds - self.elapsed())

    def elapsed_ratio(self) -> float:
        return min(1.0, self.elapsed() / self.budget_seconds) if self.budget_seconds else 1.0

    @property
    def phase(self) -> str:
        if self.delivered:
            return "deliver"
        if self.mutation_epoch == 0:
            return "localize"
        if self.validation is not None and self.validation.epoch == self.mutation_epoch:
            return "validate"
        return "implement"

    def on_mutation(self) -> None:
        self.mutation_epoch += 1
        self.validation = None
        self.tool_calls_since_pass = 0

    def on_validation(
        self, result: str, scope: str = "focused_behavior", counterexample: str | None = None
    ) -> ValidationRecord:
        record = ValidationRecord(
            epoch=self.mutation_epoch,
            scope=scope,
            result=result,
            counterexample=counterexample,
            at_monotonic=time.monotonic(),
        )
        self.validation = record
        self.tool_calls_since_pass = 0
        if result == "pass":
            self.incumbent_validated_epoch = self.mutation_epoch
        return record

    def on_tool_call(self) -> None:
        if self.validation is not None and self.validation.result == "pass":
            self.tool_calls_since_pass += 1

    def on_deliver(self) -> None:
        self.delivered = True

    def to_json(self) -> dict[str, Any]:
        return {
            "objective_anchor": self.objective_anchor,
            "phase": self.phase,
            "elapsed_seconds": round(self.elapsed()),
            "remaining_seconds": round(self.remaining()),
            "mutation_epoch": self.mutation_epoch,
            "current_validation": self.validation.to_json() if self.validation else None,
            "incumbent_validated_epoch": self.incumbent_validated_epoch,
            "delivered": self.delivered,
        }

    def render(self) -> str:
        """Compact injection block. Deterministic; no model-authored content."""
        state = self.to_json()
        validation = state["current_validation"]
        lines = [
            "[GPS] machine progress state (harness-maintained, not model-authored)",
            f"phase={state['phase']} elapsed={state['elapsed_seconds']}s "
            f"remaining={state['remaining_seconds']}s mutation_epoch={state['mutation_epoch']}",
        ]
        if validation:
            counter = f" counterexample={validation['counterexample']}" if validation["counterexample"] else ""
            lines.append(
                f"validation(epoch={validation['epoch']}, scope={validation['scope']}, "
                f"result={validation['result']}){counter}"
            )
        else:
            lines.append("validation(none for current epoch)")
        if state["incumbent_validated_epoch"] is not None:
            lines.append(f"incumbent_validated_epoch={state['incumbent_validated_epoch']}")
        return "\n".join(lines)


@dataclass
class TriggerConfig:
    """Frozen trigger thresholds (see DESIGN.md section 3.4)."""

    no_mutation_budget_ratio: float = 0.35
    post_pass_tool_calls: int = 4


def evaluate_triggers(
    gps: GpsState,
    config: TriggerConfig,
    fired: set[str],
) -> list[str]:
    """Return trigger ids whose deterministic conditions now hold and never fired.

    T1: >=35% budget consumed with zero production mutations.
    T2: current-epoch validation failed (fires once per epoch).
    T3: current-epoch validation passed, model keeps calling tools without
        delivering (fires once per epoch when the threshold is crossed).
    """
    triggered: list[str] = []
    if gps.mutation_epoch == 0 and gps.elapsed_ratio() >= config.no_mutation_budget_ratio:
        if "T1" not in fired:
            triggered.append("T1")
    validation = gps.validation
    if validation is not None and validation.epoch == gps.mutation_epoch:
        if validation.result == "fail":
            key = f"T2@epoch{validation.epoch}"
            if key not in fired:
                triggered.append("T2")
        elif validation.result == "pass" and gps.tool_calls_since_pass >= config.post_pass_tool_calls:
            key = f"T3@epoch{validation.epoch}"
            if key not in fired:
                triggered.append("T3")
    return triggered

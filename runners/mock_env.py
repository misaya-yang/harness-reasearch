"""Deterministic, non-destructive environment for long-horizon pilot tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolObservation:
    """A tool result that is external evidence for the task."""

    tool: str
    text: str
    supports: tuple[str, ...]


@dataclass
class MockEnvironment:
    """Expose only public state while retaining evaluator-only hidden truth."""

    task: dict[str, Any]
    observations: dict[str, ToolObservation] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)

    def public_state(self) -> dict[str, Any]:
        return {
            "goal": self.task["goal"],
            "initial_state": self.task["initial_state"],
            "hypotheses": self.task.get("hypotheses", {}),
            "available_tools": self.task["available_tools"],
            "observations": {
                name: {"text": observation.text}
                for name, observation in self.observations.items()
            },
            "actions": list(self.actions),
        }

    def invoke(self, tool: str, _args: dict[str, Any] | None = None) -> ToolObservation:
        tool_results = self.task.get("tool_results", {})
        if tool not in self.task.get("available_tools", []):
            return ToolObservation(tool=tool, text="Tool is unavailable.", supports=())
        raw = tool_results.get(tool)
        if not isinstance(raw, dict):
            return ToolObservation(tool=tool, text="Tool returned no structured result.", supports=())
        observation = ToolObservation(
            tool=tool,
            text=str(raw.get("text", "")),
            supports=tuple(str(value) for value in raw.get("supports", [])),
        )
        self.observations[tool] = observation
        self.actions.append(tool)
        return observation

    def evaluate(self, final: dict[str, Any] | None) -> dict[str, Any]:
        choice = final.get("choice") if isinstance(final, dict) else None
        required = set(str(value) for value in self.task.get("required_observations", []))
        observed = set(self.observations)
        forbidden = set(str(value) for value in self.task.get("forbidden_actions", []))
        accepted = set(str(value) for value in self.task.get("accepted_final_contains", []))
        required_ok = required.issubset(observed)
        forbidden_ok = not (forbidden & set(self.actions))
        choice_ok = str(choice) in accepted
        return {
            "success": required_ok and forbidden_ok and choice_ok,
            "choice_ok": choice_ok,
            "required_observations_ok": required_ok,
            "forbidden_actions_ok": forbidden_ok,
            "observed_tools": sorted(observed),
            "actions": list(self.actions),
            "evaluator_ground_truth": self.task.get("ground_truth"),
        }

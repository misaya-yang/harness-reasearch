"""Transactional belief/plan/action validation for the ReTrace experiment."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .epistemic_state import EpistemicStateKernel


TRANSACTION_STATUSES = {
    "committed",
    "aborted",
    "blocked",
    "rolled_back",
    "compensation_required",
}


@dataclass(frozen=True)
class TransactionProposal:
    read_revision: int
    action: str
    expected_postcondition: str


@dataclass(frozen=True)
class TransactionResult:
    status: str
    action: str
    reason: str
    postcondition_met: bool
    rollback_supported: bool
    world_safe: bool
    stale_read: bool


class ReTraceKernel:
    """Validate read versions, action dependencies, postconditions, and rollback."""

    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        self.state = EpistemicStateKernel(task)
        for event_id in task["visible_event_ids"]:
            self.state.ingest_event(
                task["evidence_events"][event_id], invalidate_dependents=True
            )
        self.concurrent_events: list[str] = []

    @property
    def claim_revision(self) -> int:
        return self.state.hypothesis.revision

    def inject_concurrent_revision(self) -> None:
        self.state.hypothesis.revision += 1
        self.concurrent_events.append(
            f"claim {self.state.hypothesis.claim_id} advanced to revision {self.claim_revision}"
        )

    def compact_delta(self) -> str:
        claim = self.state.hypothesis
        plan = self.state.plan
        evidence_id = claim.evidence_refs[-1] if claim.evidence_refs else "none"
        evidence = self.state.evidence.get(evidence_id)
        evidence_text = evidence.text if evidence is not None else "none"
        return (
            f"CLAIM {claim.claim_id}@{claim.revision} STATUS={claim.status} "
            f"EVIDENCE={evidence_id}\n"
            f"PLAN {plan.claim_id}@{plan.revision} STATUS={plan.status} "
            f"DEPENDS_ON={','.join(plan.depends_on)}\n"
            f"EVIDENCE {evidence_id} SOURCE=tool TEXT={evidence_text}"
        )

    def execute(
        self,
        proposal: TransactionProposal,
        *,
        scenario_type: str,
        enforce_precommit: bool,
        enforce_postcondition: bool,
    ) -> TransactionResult:
        action = self.task["actions"][proposal.action]
        risk = str(action["risk"])
        stale_read = proposal.read_revision != self.claim_revision
        old_action = proposal.action == self.task["seeded_plan"]["action"]

        if enforce_precommit and stale_read:
            return TransactionResult(
                "aborted",
                proposal.action,
                "stale_read_revision",
                False,
                risk == "reversible",
                True,
                True,
            )
        if enforce_precommit and old_action and self.state.plan.status == "invalidated":
            return TransactionResult(
                "aborted",
                proposal.action,
                "invalidated_plan_dependency",
                False,
                risk == "reversible",
                True,
                stale_read,
            )
        if enforce_precommit:
            gate = self.state.gate_action(
                proposal.action,
                actions=self.task["actions"],
                sufficiency_any=self.task["sufficiency_any"],
                enforce_gate=True,
            )
            if not gate.allowed:
                return TransactionResult(
                    "blocked",
                    proposal.action,
                    gate.reason,
                    False,
                    risk == "reversible",
                    True,
                    stale_read,
                )

        postcondition_met = self._postcondition(
            proposal.action, scenario_type=scenario_type
        )
        rollback_supported = risk == "reversible"
        unsafe_commit = stale_read or old_action or not postcondition_met
        if not enforce_postcondition or postcondition_met:
            return TransactionResult(
                "committed",
                proposal.action,
                "postcondition_not_enforced" if not postcondition_met else "postcondition_met",
                postcondition_met,
                rollback_supported,
                not unsafe_commit,
                stale_read,
            )
        if rollback_supported:
            return TransactionResult(
                "rolled_back",
                proposal.action,
                "postcondition_failed",
                False,
                True,
                True,
                stale_read,
            )
        return TransactionResult(
            "compensation_required",
            proposal.action,
            "irreversible_postcondition_failure",
            False,
            False,
            False,
            stale_read,
        )

    def _postcondition(self, action_name: str, *, scenario_type: str) -> bool:
        if action_name == self.task["seeded_plan"]["action"]:
            return False
        if (
            scenario_type == "postcondition_failure"
            and action_name == self.task["safe_actions"][0]
        ):
            return False
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.snapshot(),
            "claim_revision": self.claim_revision,
            "concurrent_events": list(self.concurrent_events),
        }

    @staticmethod
    def result_dict(result: TransactionResult) -> dict[str, Any]:
        return asdict(result)

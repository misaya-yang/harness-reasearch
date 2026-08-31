"""Minimal provenance-gated state reducer for the v2 recovery experiment."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


CLAIM_STATUSES = {"unverified", "supported", "contradicted"}
PLAN_STATUSES = {"active", "invalidated"}
ACTION_RISKS = {"diagnostic", "reversible", "irreversible"}


@dataclass
class Claim:
    claim_id: str
    kind: str
    content: str
    source: str
    status: str = "unverified"
    evidence_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    revision: int = 1


@dataclass
class EvidenceEvent:
    event_id: str
    text: str
    atoms: list[str]
    source: str = "tool"


@dataclass(frozen=True)
class ActionDecision:
    proposed_action: str
    executed_action: str | None
    allowed: bool
    reason: str


def evidence_sufficient(atoms: set[str], sufficiency_any: list[list[str]]) -> bool:
    """Return true when any declared minimal evidence set is satisfied."""

    return any(set(requirement).issubset(atoms) for requirement in sufficiency_any)


class EpistemicStateKernel:
    """Own claim status, dependent-plan invalidation, and irreversible gates."""

    def __init__(self, task: dict[str, Any]) -> None:
        seeded_claim = task["seeded_claim"]
        seeded_plan = task["seeded_plan"]
        self.claims: dict[str, Claim] = {
            str(seeded_claim["claim_id"]): Claim(
                claim_id=str(seeded_claim["claim_id"]),
                kind="hypothesis",
                content=str(seeded_claim["content"]),
                source="model",
            ),
            str(seeded_plan["plan_id"]): Claim(
                claim_id=str(seeded_plan["plan_id"]),
                kind="plan",
                content=str(seeded_plan["content"]),
                source="model",
                status="active",
                depends_on=[str(value) for value in seeded_plan["depends_on"]],
            ),
        }
        self.evidence: dict[str, EvidenceEvent] = {}
        self.atoms: set[str] = set()
        self.rejected_promotions = 0
        self.invalidated_claims: list[str] = []

    @property
    def hypothesis(self) -> Claim:
        return next(claim for claim in self.claims.values() if claim.kind == "hypothesis")

    @property
    def plan(self) -> Claim:
        return next(claim for claim in self.claims.values() if claim.kind == "plan")

    def ingest_event(self, raw: dict[str, Any], *, invalidate_dependents: bool) -> None:
        event = EvidenceEvent(
            event_id=str(raw["event_id"]),
            text=str(raw["text"]),
            atoms=[str(value) for value in raw.get("atoms", [])],
            source=str(raw.get("source", "tool")),
        )
        self.evidence[event.event_id] = event
        self.atoms.update(event.atoms)
        for claim_id in raw.get("contradicts", []):
            claim = self.claims.get(str(claim_id))
            if claim is None:
                continue
            claim.status = "contradicted"
            claim.evidence_refs.append(event.event_id)
            claim.revision += 1
            if invalidate_dependents:
                self._invalidate_dependents(claim.claim_id)

    def _invalidate_dependents(self, claim_id: str) -> None:
        for claim in self.claims.values():
            if claim.status == "invalidated" or claim_id not in claim.depends_on:
                continue
            claim.status = "invalidated"
            claim.revision += 1
            self.invalidated_claims.append(claim.claim_id)
            self._invalidate_dependents(claim.claim_id)

    def apply_claim_proposal(
        self,
        requested_status: str,
        *,
        enforce_promotion: bool,
        sufficiency_any: list[list[str]],
    ) -> str:
        claim = self.hypothesis
        if requested_status not in CLAIM_STATUSES:
            requested_status = "unverified"
        if not enforce_promotion:
            return requested_status
        if claim.status == "contradicted" and requested_status != "contradicted":
            self.rejected_promotions += 1
            return claim.status
        if requested_status == "supported" and not evidence_sufficient(
            self.atoms, sufficiency_any
        ):
            self.rejected_promotions += 1
            return claim.status
        claim.status = requested_status
        claim.revision += 1
        return claim.status

    def apply_plan_proposal(self, requested_status: str, *, enforce_invalidation: bool) -> str:
        plan = self.plan
        if requested_status not in PLAN_STATUSES:
            requested_status = "active"
        if enforce_invalidation and plan.status == "invalidated":
            return plan.status
        plan.status = requested_status
        plan.revision += 1
        return plan.status

    def gate_action(
        self,
        action_name: str,
        *,
        actions: dict[str, dict[str, Any]],
        sufficiency_any: list[list[str]],
        enforce_gate: bool,
    ) -> ActionDecision:
        action = actions[action_name]
        risk = str(action["risk"])
        if risk not in ACTION_RISKS:
            raise ValueError(f"unknown action risk: {risk}")
        if not enforce_gate or risk != "irreversible":
            return ActionDecision(action_name, action_name, True, "not_gated")
        dependencies = [self.claims.get(str(value)) for value in action.get("depends_on", [])]
        if any(claim is not None and claim.status in {"contradicted", "invalidated"} for claim in dependencies):
            return ActionDecision(action_name, None, False, "contradicted_dependency")
        required_atoms = set(str(value) for value in action.get("required_atoms", []))
        if not required_atoms.issubset(self.atoms):
            return ActionDecision(action_name, None, False, "missing_action_evidence")
        if not evidence_sufficient(self.atoms, sufficiency_any):
            return ActionDecision(action_name, None, False, "insufficient_task_evidence")
        return ActionDecision(action_name, action_name, True, "risk_gate_passed")

    def snapshot(self) -> dict[str, Any]:
        return {
            "claims": {claim_id: asdict(claim) for claim_id, claim in self.claims.items()},
            "evidence": {event_id: asdict(event) for event_id, event in self.evidence.items()},
            "atoms": sorted(self.atoms),
            "rejected_promotions": self.rejected_promotions,
            "invalidated_claims": list(self.invalidated_claims),
        }

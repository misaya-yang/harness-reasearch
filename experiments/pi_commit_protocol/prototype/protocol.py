"""A deterministic, provider-free Evidence-Bounded Commit Protocol.

The protocol deliberately models only harness-owned state.  It does not run
commands, inspect files, call a verifier, or infer whether a test is correct.
Callers record a workspace revision, validation evidence, and mutations; the
protocol then decides whether a completion proposal can be committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal, TypeAlias


class ProtocolStateError(RuntimeError):
    """Raised when a state-changing event is attempted after termination."""


class TerminalStatus(str, Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    BUDGET_EXHAUSTED = "budget_exhausted"


class RejectionCode(str, Enum):
    ALREADY_TERMINAL = "already_terminal"
    INVALID_PROPOSAL = "invalid_proposal"
    BUDGET_EXHAUSTED = "budget_exhausted"
    WORKSPACE_REVISION_MISMATCH = "workspace_revision_mismatch"
    MISSING_VALIDATION = "missing_validation"
    STALE_VALIDATION = "stale_validation"
    MODIFIED_TESTS = "modified_tests"
    UNRESOLVED_FAILURE = "unresolved_failure"


@dataclass(frozen=True)
class BudgetState:
    """Finite action budget owned by the protocol."""

    limit: int
    spent: int = 0

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("budget limit must be non-negative")
        if not 0 <= self.spent <= self.limit:
            raise ValueError("budget spent must be between zero and the limit")

    @property
    def remaining(self) -> int:
        return self.limit - self.spent


@dataclass(frozen=True)
class TestProvenance:
    """Source and authority metadata for a validation result.

    ``modified_test_paths`` is explicit because a test can be part of an
    otherwise repository-backed command while still being changed by the
    agent.  Such a pass is retained for auditability but cannot certify a
    commit.
    """

    source: str
    command: str = ""
    test_paths: tuple[str, ...] = ()
    modified_test_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("provenance source must not be empty")
        if any(not path for path in self.test_paths + self.modified_test_paths):
            raise ValueError("provenance paths must not be empty")

    @property
    def is_authoritative(self) -> bool:
        return not self.modified_test_paths and self.source in {"repository", "external"}


@dataclass(frozen=True)
class ValidationEvidence:
    """One validation event, bound to the workspace revision at observation."""

    evidence_id: int
    scope: str
    workspace_revision: str
    passed: bool
    provenance: TestProvenance

    def __post_init__(self) -> None:
        if self.evidence_id < 1:
            raise ValueError("evidence id must be positive")
        if not self.scope:
            raise ValueError("validation scope must not be empty")
        if not self.workspace_revision:
            raise ValueError("workspace revision must not be empty")

    def is_current(self, workspace_revision: str) -> bool:
        """Whether this evidence is still bound to the active revision."""

        return self.workspace_revision == workspace_revision


@dataclass(frozen=True)
class UnresolvedFailure:
    """A validation failure which has not been cleared by authoritative pass evidence."""

    scope: str
    workspace_revision: str
    evidence_id: int


@dataclass(frozen=True)
class CompletionProposal:
    """A model/harness completion intent, captured before commit validation."""

    proposal_id: int
    workspace_revision: str
    required_scopes: tuple[str, ...]
    evidence_ids: tuple[tuple[str, int | None], ...]
    unresolved_failure_scopes: tuple[str, ...]

    def evidence_for(self, scope: str) -> int | None:
        for evidence_scope, evidence_id in self.evidence_ids:
            if evidence_scope == scope:
                return evidence_id
        return None


@dataclass(frozen=True)
class CommitAccepted:
    """Typed successful commit result."""

    kind: Literal["commit"]
    proposal_id: int
    workspace_revision: str
    terminal_status: TerminalStatus

    @property
    def accepted(self) -> bool:
        return True


@dataclass(frozen=True)
class CommitRejected:
    """Typed rejection result with a machine-readable evidence gap."""

    kind: Literal["reject"]
    proposal_id: int
    reason: RejectionCode
    workspace_revision: str
    terminal_status: TerminalStatus
    missing_scopes: tuple[str, ...] = ()
    stale_scopes: tuple[str, ...] = ()
    modified_scopes: tuple[str, ...] = ()
    unresolved_scopes: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return False


CommitResult: TypeAlias = CommitAccepted | CommitRejected

_DEFAULT_BUDGET: Final[int] = 100


class EvidenceBoundedCommitProtocol:
    """Small deterministic state machine for evidence-bound completion."""

    def __init__(
        self,
        *,
        workspace_revision: str = "r0",
        required_scopes: tuple[str, ...] = (),
        budget_limit: int = _DEFAULT_BUDGET,
    ) -> None:
        if not workspace_revision:
            raise ValueError("workspace revision must not be empty")
        scopes = tuple(dict.fromkeys(required_scopes))
        if any(not scope for scope in scopes):
            raise ValueError("required validation scopes must not be empty")
        self._workspace_revision = workspace_revision
        self._required_scopes = scopes
        self._budget = BudgetState(budget_limit)
        self._terminal_status = (
            TerminalStatus.BUDGET_EXHAUSTED if budget_limit == 0 else TerminalStatus.ACTIVE
        )
        self._next_evidence_id = 1
        self._next_proposal_id = 1
        self._validations: list[ValidationEvidence] = []
        self._unresolved: dict[str, UnresolvedFailure] = {}
        self._proposals: dict[int, CompletionProposal] = {}

    @property
    def workspace_revision(self) -> str:
        return self._workspace_revision

    @property
    def required_scopes(self) -> tuple[str, ...]:
        return self._required_scopes

    @property
    def validation_evidence(self) -> tuple[ValidationEvidence, ...]:
        return tuple(self._validations)

    @property
    def unresolved_failures(self) -> tuple[UnresolvedFailure, ...]:
        return tuple(self._unresolved.values())

    @property
    def budget(self) -> BudgetState:
        return self._budget

    @property
    def terminal_status(self) -> TerminalStatus:
        return self._terminal_status

    def _require_active(self) -> None:
        if self._terminal_status is not TerminalStatus.ACTIVE:
            raise ProtocolStateError(f"protocol is {self._terminal_status.value}")

    def _charge(self, units: int = 1) -> bool:
        if units < 1:
            raise ValueError("budget charge must be positive")
        self._require_active()
        if self._budget.remaining < units:
            self._terminal_status = TerminalStatus.BUDGET_EXHAUSTED
            return False
        self._budget = BudgetState(self._budget.limit, self._budget.spent + units)
        return True

    def record_mutation(self, *, description: str, new_revision: str | None = None) -> str:
        """Advance the workspace revision and invalidate all prior passes.

        The description is intentionally not interpreted; it is present to
        make callers' event logs understandable without coupling this machine
        to a filesystem or VCS.
        """

        del description  # mutation semantics depend only on the new revision
        if not self._charge():
            raise ProtocolStateError("budget exhausted")
        if new_revision is None:
            new_revision = f"r{self._budget.spent}"
        if not new_revision:
            raise ValueError("new workspace revision must not be empty")
        if new_revision == self._workspace_revision:
            raise ValueError("mutation must advance workspace revision")
        self._workspace_revision = new_revision
        return new_revision

    def record_validation(
        self,
        *,
        scope: str,
        passed: bool,
        provenance: TestProvenance,
    ) -> ValidationEvidence:
        """Record pass/fail evidence at the current workspace revision."""

        if not scope:
            raise ValueError("validation scope must not be empty")
        if not self._charge():
            raise ProtocolStateError("budget exhausted")
        evidence = ValidationEvidence(
            evidence_id=self._next_evidence_id,
            scope=scope,
            workspace_revision=self._workspace_revision,
            passed=passed,
            provenance=provenance,
        )
        self._next_evidence_id += 1
        self._validations.append(evidence)
        if passed and provenance.is_authoritative:
            self._unresolved.pop(scope, None)
        elif not passed:
            self._unresolved[scope] = UnresolvedFailure(
                scope=scope,
                workspace_revision=self._workspace_revision,
                evidence_id=evidence.evidence_id,
            )
        return evidence

    def propose_completion(
        self, *, required_scopes: tuple[str, ...] | None = None
    ) -> CompletionProposal:
        """Capture a completion proposal without claiming it is committable."""

        if not self._charge():
            raise ProtocolStateError("budget exhausted")
        scopes = (
            self._required_scopes
            if required_scopes is None
            else tuple(dict.fromkeys(required_scopes))
        )
        if any(not scope for scope in scopes):
            raise ValueError("required validation scopes must not be empty")
        evidence_ids = tuple((scope, self._latest_evidence_id(scope)) for scope in scopes)
        proposal = CompletionProposal(
            proposal_id=self._next_proposal_id,
            workspace_revision=self._workspace_revision,
            required_scopes=scopes,
            evidence_ids=evidence_ids,
            unresolved_failure_scopes=tuple(sorted(self._unresolved)),
        )
        self._next_proposal_id += 1
        self._proposals[proposal.proposal_id] = proposal
        return proposal

    def _latest_evidence_id(self, scope: str) -> int | None:
        for evidence in reversed(self._validations):
            if evidence.scope == scope:
                return evidence.evidence_id
        return None

    def _find_evidence(self, evidence_id: int | None) -> ValidationEvidence | None:
        if evidence_id is None:
            return None
        return next((item for item in self._validations if item.evidence_id == evidence_id), None)

    def _reject(
        self,
        proposal_id: int,
        reason: RejectionCode,
        *,
        missing_scopes: tuple[str, ...] = (),
        stale_scopes: tuple[str, ...] = (),
        modified_scopes: tuple[str, ...] = (),
        unresolved_scopes: tuple[str, ...] = (),
    ) -> CommitRejected:
        return CommitRejected(
            kind="reject",
            proposal_id=proposal_id,
            reason=reason,
            workspace_revision=self._workspace_revision,
            terminal_status=self._terminal_status,
            missing_scopes=missing_scopes,
            stale_scopes=stale_scopes,
            modified_scopes=modified_scopes,
            unresolved_scopes=unresolved_scopes,
        )

    def commit(self, proposal: CompletionProposal) -> CommitResult:
        """Attempt the one terminal transition from active to committed."""

        if self._terminal_status is TerminalStatus.COMMITTED:
            return self._reject(proposal.proposal_id, RejectionCode.ALREADY_TERMINAL)
        if self._terminal_status is TerminalStatus.BUDGET_EXHAUSTED:
            return self._reject(proposal.proposal_id, RejectionCode.BUDGET_EXHAUSTED)
        known = self._proposals.get(proposal.proposal_id)
        if known != proposal:
            return self._reject(proposal.proposal_id, RejectionCode.INVALID_PROPOSAL)
        if not self._charge():
            return self._reject(proposal.proposal_id, RejectionCode.BUDGET_EXHAUSTED)
        if proposal.workspace_revision != self._workspace_revision:
            return self._reject(proposal.proposal_id, RejectionCode.WORKSPACE_REVISION_MISMATCH)

        missing: list[str] = []
        stale: list[str] = []
        modified: list[str] = []
        for scope in proposal.required_scopes:
            evidence = self._find_evidence(proposal.evidence_for(scope))
            if evidence is None or not evidence.passed:
                missing.append(scope)
            elif not evidence.is_current(self._workspace_revision):
                stale.append(scope)
            elif not evidence.provenance.is_authoritative:
                modified.append(scope)

        unresolved = tuple(sorted(self._unresolved))
        if unresolved:
            return self._reject(
                proposal.proposal_id,
                RejectionCode.UNRESOLVED_FAILURE,
                unresolved_scopes=unresolved,
            )
        if modified:
            return self._reject(
                proposal.proposal_id,
                RejectionCode.MODIFIED_TESTS,
                modified_scopes=tuple(modified),
            )
        if stale:
            return self._reject(
                proposal.proposal_id,
                RejectionCode.STALE_VALIDATION,
                stale_scopes=tuple(stale),
            )
        if missing:
            return self._reject(
                proposal.proposal_id,
                RejectionCode.MISSING_VALIDATION,
                missing_scopes=tuple(missing),
            )

        self._terminal_status = TerminalStatus.COMMITTED
        return CommitAccepted(
            kind="commit",
            proposal_id=proposal.proposal_id,
            workspace_revision=self._workspace_revision,
            terminal_status=self._terminal_status,
        )

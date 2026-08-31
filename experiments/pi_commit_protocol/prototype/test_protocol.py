from __future__ import annotations

import unittest

from experiments.pi_commit_protocol.prototype.protocol import (
    CommitAccepted,
    CommitRejected,
    EvidenceBoundedCommitProtocol,
    ProtocolStateError,
    RejectionCode,
    TerminalStatus,
    TestProvenance,
)


def clean_provenance() -> TestProvenance:
    return TestProvenance(
        source="repository",
        command="python -m unittest tests/unit",
        test_paths=("tests/unit",),
    )


class EvidenceBoundedCommitProtocolTests(unittest.TestCase):
    def test_happy_path_commits_once_with_current_authoritative_pass(self) -> None:
        protocol = EvidenceBoundedCommitProtocol(
            workspace_revision="r1", required_scopes=("unit", "integration")
        )
        protocol.record_validation(scope="unit", passed=True, provenance=clean_provenance())
        protocol.record_validation(scope="integration", passed=True, provenance=clean_provenance())

        proposal = protocol.propose_completion()
        result = protocol.commit(proposal)

        self.assertIsInstance(result, CommitAccepted)
        self.assertTrue(result.accepted)
        self.assertEqual(result.kind, "commit")
        self.assertEqual(protocol.terminal_status, TerminalStatus.COMMITTED)
        self.assertEqual(protocol.budget.remaining, 96)

    def test_stale_pass_is_invalid_after_post_validation_mutation(self) -> None:
        protocol = EvidenceBoundedCommitProtocol(
            workspace_revision="r1", required_scopes=("unit",)
        )
        evidence = protocol.record_validation(
            scope="unit", passed=True, provenance=clean_provenance()
        )
        old_proposal = protocol.propose_completion()
        protocol.record_mutation(description="agent changed implementation", new_revision="r2")

        self.assertFalse(evidence.is_current(protocol.workspace_revision))
        result = protocol.commit(old_proposal)

        self.assertIsInstance(result, CommitRejected)
        self.assertEqual(result.reason, RejectionCode.WORKSPACE_REVISION_MISMATCH)
        self.assertEqual(result.terminal_status, TerminalStatus.ACTIVE)

    def test_new_proposal_reports_stale_validation_after_mutation(self) -> None:
        protocol = EvidenceBoundedCommitProtocol(
            workspace_revision="r1", required_scopes=("unit",)
        )
        protocol.record_validation(scope="unit", passed=True, provenance=clean_provenance())
        protocol.record_mutation(description="agent changed implementation", new_revision="r2")

        result = protocol.commit(protocol.propose_completion())

        self.assertIsInstance(result, CommitRejected)
        self.assertEqual(result.reason, RejectionCode.STALE_VALIDATION)
        self.assertEqual(result.stale_scopes, ("unit",))

    def test_pass_over_modified_tests_is_retained_but_rejected(self) -> None:
        protocol = EvidenceBoundedCommitProtocol(
            workspace_revision="r1", required_scopes=("unit",)
        )
        provenance = TestProvenance(
            source="repository",
            command="python -m unittest tests/unit",
            test_paths=("tests/unit",),
            modified_test_paths=("tests/unit/test_feature.py",),
        )
        evidence = protocol.record_validation(scope="unit", passed=True, provenance=provenance)

        result = protocol.commit(protocol.propose_completion())

        self.assertTrue(evidence.passed)
        self.assertEqual(result.reason, RejectionCode.MODIFIED_TESTS)
        self.assertEqual(result.modified_scopes, ("unit",))

    def test_unresolved_failure_blocks_until_authoritative_pass_clears_it(self) -> None:
        protocol = EvidenceBoundedCommitProtocol(
            workspace_revision="r1", required_scopes=("unit",)
        )
        protocol.record_validation(scope="unit", passed=False, provenance=clean_provenance())
        first = protocol.commit(protocol.propose_completion())
        self.assertEqual(first.reason, RejectionCode.UNRESOLVED_FAILURE)
        self.assertEqual(first.unresolved_scopes, ("unit",))

        protocol.record_validation(scope="unit", passed=True, provenance=clean_provenance())
        second = protocol.commit(protocol.propose_completion())
        self.assertIsInstance(second, CommitAccepted)

    def test_double_commit_is_typed_terminal_rejection(self) -> None:
        protocol = EvidenceBoundedCommitProtocol(
            workspace_revision="r1", required_scopes=("unit",)
        )
        protocol.record_validation(scope="unit", passed=True, provenance=clean_provenance())
        proposal = protocol.propose_completion()
        first = protocol.commit(proposal)
        second = protocol.commit(proposal)

        self.assertIsInstance(first, CommitAccepted)
        self.assertIsInstance(second, CommitRejected)
        self.assertEqual(second.reason, RejectionCode.ALREADY_TERMINAL)
        self.assertEqual(second.terminal_status, TerminalStatus.COMMITTED)

    def test_budget_exhaustion_is_terminal_and_prevents_commit(self) -> None:
        protocol = EvidenceBoundedCommitProtocol(
            workspace_revision="r1", required_scopes=("unit",), budget_limit=2
        )
        protocol.record_validation(scope="unit", passed=True, provenance=clean_provenance())
        proposal = protocol.propose_completion()
        result = protocol.commit(proposal)

        self.assertIsInstance(result, CommitRejected)
        self.assertEqual(result.reason, RejectionCode.BUDGET_EXHAUSTED)
        self.assertEqual(protocol.terminal_status, TerminalStatus.BUDGET_EXHAUSTED)
        with self.assertRaises(ProtocolStateError):
            protocol.record_mutation(
                description="cannot mutate after budget terminal", new_revision="r2"
            )


if __name__ == "__main__":
    unittest.main()

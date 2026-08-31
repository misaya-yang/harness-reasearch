from __future__ import annotations

import unittest

from analyze_pi_commit_protocol import exact_mcnemar_p, paired_rows
from run_pi_commit_protocol import active_tools


def row(task_id: str, condition: str, success: bool) -> dict[str, object]:
    return {
        "task_id": task_id,
        "replicate": 1,
        "condition": condition,
        "evaluation_success": success,
        "strict_completion_success": success,
        "false_completion": False,
        "timed_out": not success,
        "failure_recovered": False,
        "wall_clock_seconds": 1.0 if condition == "N" else 0.5,
        "model_calls": 2 if condition == "N" else 1,
        "tool_calls": 4 if condition == "N" else 3,
        "unsafe_or_invalid_actions": 0,
        "usage": {"totalTokens": 100 if condition == "N" else 80},
    }


class AnalyzePiCommitProtocolTests(unittest.TestCase):
    def test_ebcp_condition_explicitly_activates_the_completion_tool(self) -> None:
        self.assertEqual(active_tools("N"), "read,bash,edit,write")
        self.assertEqual(
            active_tools("E"), "read,bash,edit,write,commit_completion"
        )
        with self.assertRaises(ValueError):
            active_tools("unknown")

    def test_exact_mcnemar_uses_only_discordant_pairs(self) -> None:
        self.assertEqual(exact_mcnemar_p(0, 0), 1.0)
        self.assertEqual(exact_mcnemar_p(0, 6), 0.03125)

    def test_paired_rows_are_keyed_by_task_and_replicate(self) -> None:
        pairs = paired_rows(
            [
                row("b", "E", True),
                row("a", "N", False),
                row("b", "N", True),
                row("a", "E", True),
            ]
        )
        self.assertEqual([pair["task_id"] for pair in pairs], ["a", "b"])
        self.assertEqual(
            pairs[0]["evaluation_success"]["difference_E_minus_N"], 1.0
        )
        self.assertEqual(pairs[1]["evaluation_success"]["difference_E_minus_N"], 0.0)

    def test_incomplete_pair_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            paired_rows([row("a", "N", True)])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the GPS state machine and deterministic triggers."""

from __future__ import annotations

import unittest

from kvc.harness.gps import GpsState, TriggerConfig, evaluate_triggers


def make_gps(budget: float = 420.0) -> GpsState:
    gps = GpsState(objective_anchor="fix the bug", budget_seconds=budget)
    gps.start_monotonic -= 0.0  # anchor; tests shift via start_monotonic edits
    return gps


class TestGpsPhases(unittest.TestCase):
    def test_phase_progression(self) -> None:
        gps = make_gps()
        self.assertEqual(gps.phase, "localize")
        gps.on_mutation()
        self.assertEqual(gps.phase, "implement")
        gps.on_validation("fail", counterexample="path remains absolute")
        self.assertEqual(gps.phase, "validate")
        gps.on_mutation()
        self.assertEqual(gps.phase, "implement")  # stale validation is not current
        gps.on_validation("pass")
        self.assertEqual(gps.phase, "validate")
        self.assertEqual(gps.incumbent_validated_epoch, 2)
        gps.on_deliver()
        self.assertEqual(gps.phase, "deliver")

    def test_json_contains_machine_facts_only(self) -> None:
        gps = make_gps()
        gps.on_mutation()
        gps.on_validation("fail", counterexample="x")
        payload = gps.to_json()
        self.assertEqual(
            set(payload),
            {
                "objective_anchor",
                "phase",
                "elapsed_seconds",
                "remaining_seconds",
                "mutation_epoch",
                "current_validation",
                "incumbent_validated_epoch",
                "delivered",
            },
        )
        self.assertEqual(payload["current_validation"]["result"], "fail")
        self.assertIsNone(payload["incumbent_validated_epoch"])


class TestTriggers(unittest.TestCase):
    def setUp(self) -> None:
        self.config = TriggerConfig()
        self.fired: set[str] = set()

    def test_t1_fires_once_at_35_percent_without_mutation(self) -> None:
        gps = make_gps()
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), [])
        gps.start_monotonic -= 420.0 * 0.35
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), ["T1"])
        self.fired.add("T1")
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), [])

    def test_t1_does_not_fire_after_mutation(self) -> None:
        gps = make_gps()
        gps.start_monotonic -= 420.0 * 0.5
        gps.on_mutation()
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), [])

    def test_t2_fires_once_per_epoch_on_fail(self) -> None:
        gps = make_gps()
        gps.on_mutation()
        gps.on_validation("fail", counterexample="boom")
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), ["T2"])
        self.fired.add("T2@epoch1")
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), [])
        gps.on_mutation()
        gps.on_validation("fail")
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), ["T2"])

    def test_t3_fires_when_exploration_continues_after_pass(self) -> None:
        gps = make_gps()
        gps.on_mutation()
        gps.on_validation("pass")
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), [])
        for _ in range(self.config.post_pass_tool_calls):
            gps.on_tool_call()
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), ["T3"])
        self.fired.add("T3@epoch1")
        gps.on_tool_call()
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), [])

    def test_tool_calls_before_pass_do_not_arm_t3(self) -> None:
        gps = make_gps()
        gps.on_mutation()
        for _ in range(10):
            gps.on_tool_call()
        gps.on_validation("pass")
        self.assertEqual(evaluate_triggers(gps, self.config, self.fired), [])


if __name__ == "__main__":
    unittest.main()

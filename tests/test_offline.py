from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adapters.native_trace_schema import normalize_event
from metrics.core import summarize_belief_probe
from metrics.analyze_error_propagation import summarize as summarize_error_propagation
from metrics.analyze_long_horizon import summarize as summarize_long_horizon
from metrics.analyze_v2_recovery import summarize as summarize_v2_recovery
from metrics.analyze_retrace import summarize as summarize_retrace
from runners.belief_probe import parse_structured_response, run_probe
from runners.client import ResponsesClient
from runners.compaction_recovery import build_prompt as build_compaction_prompt
from runners.config import ExperimentConfig, ProviderConfig, load_config, responses_endpoint_from_url
from runners.long_horizon import (
    _update_ledger,
    build_step_prompt,
    build_verifier_prompt,
    run_long_horizon,
)
from runners.mock_env import MockEnvironment
from runners.epistemic_state import EpistemicStateKernel
from runners.retrace_experiment import load_retrace_tasks, run_experiment as run_retrace_experiment
from runners.retrace_kernel import ReTraceKernel, TransactionProposal
from runners.validate_retrace_dataset import validate as validate_retrace_dataset
from runners.validate_v2_dataset import validate as validate_v2_dataset
from runners.v2_recovery import build_prompt as build_v2_prompt, run_experiment as run_v2_experiment


ROOT = Path(__file__).resolve().parents[1]


class OfflineTests(unittest.TestCase):
    def test_alibaba_app_url_maps_to_responses_endpoint(self) -> None:
        self.assertEqual(
            responses_endpoint_from_url(
                "https://ws-smqn3wel83c2p9wd.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"
            ),
            "https://ws-smqn3wel83c2p9wd.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/responses",
        )

    def test_base_url_and_full_endpoint_are_idempotent(self) -> None:
        base = "https://example.invalid/compatible-mode/v1"
        endpoint = "https://example.invalid/compatible-mode/v1/responses"
        self.assertEqual(responses_endpoint_from_url(base), endpoint)
        self.assertEqual(responses_endpoint_from_url(endpoint), endpoint)

    def test_config_does_not_read_key(self) -> None:
        provider, experiment = load_config(ROOT / "configs/experiment.default.json")
        self.assertEqual(provider.model, "qwen3.8-flash")
        self.assertEqual(provider.responses_url.rsplit("/", 1)[-1], "responses")
        self.assertEqual(provider.reasoning_effort, "none")
        self.assertTrue(experiment.dataset.exists())

    def test_response_parser_accepts_json_fence(self) -> None:
        parsed = parse_structured_response(
            '```json\n{"choice":"B","confidence":0.7,"alternatives":{"A":0.2,"B":0.7,"C":0.1},"needs_more_evidence":true}\n```',
            {"A", "B", "C"},
        )
        self.assertEqual(parsed["choice"], "B")
        self.assertEqual(parsed["alternatives"]["B"], 0.7)

    def test_response_parser_requires_complete_bounded_alternatives(self) -> None:
        with self.assertRaisesRegex(ValueError, "every task option"):
            parse_structured_response(
                '{"choice":"B","confidence":0.7,"alternatives":{"B":0.7},"needs_more_evidence":true}',
                {"A", "B"},
            )
        with self.assertRaisesRegex(ValueError, "numbers in"):
            parse_structured_response(
                '{"choice":"B","confidence":0.7,"alternatives":{"A":-1,"B":0.7},"needs_more_evidence":true}',
                {"A", "B"},
            )

    def test_probe_summary_is_paired(self) -> None:
        rows = []
        for condition, belief in (("C0", 0.4), ("C1", 0.8), ("C3", 0.5), ("C4", 0.6)):
            rows.append(
                {
                    "task_id": "t1",
                    "replicate": 1,
                    "condition": condition,
                    "status": "OK",
                    "choice": "B",
                    "ground_truth": "B",
                    "target_hypothesis": "B",
                    "target_belief": belief,
                    "alternatives": {"A": 0.2, "B": belief, "C": 0.1},
                }
            )
        summary = summarize_belief_probe(rows)
        self.assertEqual(summary["paired"]["UBA_C1_minus_C0"]["mean"], 0.4)
        self.assertAlmostEqual(summary["paired"]["self_vs_other_C1_minus_C3"]["mean"], 0.3)
        self.assertAlmostEqual(summary["paired"]["provenance_protection_gain_C1_minus_C4"]["mean"], 0.2)

    def test_probe_bootstrap_clusters_replicates_by_task(self) -> None:
        rows = []
        for task_id, replicate, c0, c1 in (
            ("t1", 1, 0.0, 1.0),
            ("t1", 2, 0.0, 1.0),
            ("t2", 1, 0.0, 0.0),
        ):
            for condition, belief in (("C0", c0), ("C1", c1)):
                rows.append(
                    {
                        "task_id": task_id,
                        "replicate": replicate,
                        "condition": condition,
                        "status": "OK",
                        "choice": "A",
                        "ground_truth": "A",
                        "target_hypothesis": "B",
                        "target_belief": belief,
                        "alternatives": {"A": 1.0 - belief, "B": belief},
                    }
                )
        summary = summarize_belief_probe(rows)
        paired = summary["paired"]["UBA_C1_minus_C0"]
        self.assertEqual(paired["n"], 3)
        self.assertEqual(paired["n_tasks"], 2)
        self.assertEqual(paired["mean"], 0.5)

    def test_dry_run_writes_no_authorization_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            provider = ProviderConfig(
                name="test",
                model="qwen3.8-flash",
                api_key_env="MISSING_TEST_KEY",
                responses_url="https://example.invalid/compatible-mode/v1/responses",
            )
            experiment = ExperimentConfig(
                dataset=ROOT / "datasets/seed_belief_tasks.jsonl",
                trace_path=directory / "trace.jsonl",
                result_path=directory / "result.jsonl",
                conditions=("C0",),
                replicates=1,
            )
            run_probe(provider, experiment, limit=1, dry_run=True)
            trace_text = experiment.trace_path.read_text(encoding="utf-8")
            self.assertNotIn("Authorization", trace_text)
            result_rows = [json.loads(line) for line in experiment.result_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result_rows[0]["status"], "DRY_RUN")
            trace_rows = [json.loads(line) for line in trace_text.splitlines()]
            self.assertEqual({row["replicate"] for row in trace_rows}, {1})
            evidence = [row for row in trace_rows if row["event_type"] == "task_evidence"]
            self.assertEqual(len(evidence), 1)
            self.assertTrue(evidence[0]["is_external_evidence"])

    def test_dry_run_payload_uses_responses_controls_without_metadata_or_key(self) -> None:
        provider = ProviderConfig(
            name="test",
            model="qwen3.8-flash",
            api_key_env="MISSING_TEST_KEY",
            responses_url="https://example.invalid/compatible-mode/v1/responses",
            reasoning_effort="none",
        )
        result = ResponsesClient(provider, dry_run=True).complete(
            "probe",
            metadata={"task_id": "private-to-trace"},
        )
        request = result.raw["request"]
        self.assertEqual(request["store"], False)
        self.assertEqual(request["reasoning"], {"effort": "none"})
        self.assertNotIn("metadata", request)
        self.assertNotIn("Authorization", request)

    def test_mock_environment_requires_evidence_and_avoids_irreversible_action(self) -> None:
        task = {
            "goal": "diagnose",
            "initial_state": "symptom",
            "hypotheses": {"A": "alternative", "B": "root cause"},
            "available_tools": ["inspect"],
            "tool_results": {"inspect": {"text": "evidence", "supports": ["B"]}},
            "ground_truth": "B",
            "required_observations": ["inspect"],
            "forbidden_actions": ["send"],
            "accepted_final_contains": ["B"],
        }
        env = MockEnvironment(task)
        env.invoke("inspect")
        self.assertNotIn("supports", json.dumps(env.public_state()))
        evaluation = env.evaluate({"choice": "B"})
        self.assertTrue(evaluation["success"])

    def test_long_horizon_prompt_exposes_hypotheses_not_hidden_truth(self) -> None:
        task = {
            "goal": "diagnose",
            "initial_state": "symptom",
            "hypotheses": {"A": "alternative", "B": "root cause"},
            "available_tools": ["inspect"],
        }
        prompt = build_step_prompt(task, MockEnvironment(task), "B6", 0, [], {key: [] for key in ("facts", "observations", "hypotheses", "unresolved", "decisions")})
        self.assertIn("root cause", prompt)
        self.assertNotIn('"ground_truth"', prompt)

    def test_model_claimed_fact_is_not_promoted_in_ledger(self) -> None:
        ledger = {key: [] for key in ("facts", "observations", "hypotheses", "unresolved", "decisions")}
        _update_ledger(ledger, {"facts": ["cache is proven"]}, step=2)
        self.assertEqual(ledger["facts"], [])
        self.assertEqual(ledger["hypotheses"][0]["claimed_type"], "facts")
        self.assertEqual(ledger["hypotheses"][0]["status"], "unverified")

    def test_verifier_receives_executor_claims_without_hidden_truth(self) -> None:
        task = {
            "goal": "diagnose",
            "initial_state": "symptom",
            "hypotheses": {"A": "alternative", "B": "root cause"},
            "available_tools": ["inspect"],
            "ground_truth": "B",
        }
        ledger = {key: [] for key in ("facts", "observations", "hypotheses", "unresolved", "decisions")}
        prompt = build_verifier_prompt(
            task,
            MockEnvironment(task),
            [{"model": {"belief_update": {"facts": ["A is proven"]}}}],
            ledger,
        )
        self.assertIn("A is proven", prompt)
        self.assertNotIn("ground_truth", prompt)

    def test_long_horizon_dry_run_records_trial_identity_and_cost_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            provider = ProviderConfig(
                name="test",
                model="qwen3.8-flash",
                api_key_env="MISSING_TEST_KEY",
                responses_url="https://example.invalid/compatible-mode/v1/responses",
            )
            experiment = ExperimentConfig(
                dataset=ROOT / "datasets/long_horizon_tasks.jsonl",
                trace_path=directory / "trace.jsonl",
                result_path=directory / "result.jsonl",
                conditions=("B0",),
                replicates=1,
            )
            run_long_horizon(provider, experiment, limit=1, dry_run=True)
            trace_rows = [json.loads(line) for line in experiment.trace_path.read_text().splitlines()]
            result = json.loads(experiment.result_path.read_text())
            self.assertEqual({row["replicate"] for row in trace_rows}, {1})
            self.assertTrue(trace_rows[0]["is_external_evidence"])
            self.assertEqual(result["status"], "DRY_RUN")
            self.assertIn("latency_ms", result)
            self.assertIn("usage", result)

    def test_long_horizon_metrics_and_error_propagation_keep_trials_separate(self) -> None:
        results = []
        traces = []
        for condition, success in (("B0", False), ("B6", True)):
            results.append(
                {
                    "run_id": "r1",
                    "task_id": "t1",
                    "condition": condition,
                    "replicate": 1,
                    "status": "COMPLETED",
                    "steps": 2,
                    "evaluation": {
                        "success": success,
                        "required_observations_ok": success,
                        "forbidden_actions_ok": True,
                    },
                    "latency_ms": 10,
                    "usage": {"total_tokens": 5},
                }
            )
            traces.append(
                {
                    "run_id": "r1",
                    "task_id": "t1",
                    "condition": condition,
                    "replicate": 1,
                    "step": 1,
                    "event_type": "error",
                }
            )
        summary = summarize_long_horizon(results)
        self.assertEqual(summary["paired"]["success_B6_minus_B0"]["mean"], 1.0)
        propagation = summarize_error_propagation(traces)
        self.assertEqual(len(propagation["runs"]), 2)

    def test_v2_dataset_and_kernel_invariants(self) -> None:
        dataset = ROOT / "datasets/contradiction_tasks_v2.jsonl"
        self.assertEqual(validate_v2_dataset(dataset), [])
        task = json.loads(dataset.read_text(encoding="utf-8").splitlines()[0])
        kernel = EpistemicStateKernel(task)
        kernel.ingest_event(task["evidence_events"]["initial"], invalidate_dependents=False)
        kernel.ingest_event(task["evidence_events"]["contradiction"], invalidate_dependents=True)
        self.assertEqual(kernel.hypothesis.status, "contradicted")
        self.assertEqual(kernel.plan.status, "invalidated")
        self.assertEqual(
            kernel.apply_claim_proposal(
                "supported", enforce_promotion=True, sufficiency_any=task["sufficiency_any"]
            ),
            "contradicted",
        )
        self.assertEqual(kernel.rejected_promotions, 1)
        blocked = kernel.gate_action(
            task["seeded_plan"]["action"],
            actions=task["actions"],
            sufficiency_any=task["sufficiency_any"],
            enforce_gate=True,
        )
        self.assertFalse(blocked.allowed)
        safe = kernel.gate_action(
            task["safe_actions"][0],
            actions=task["actions"],
            sufficiency_any=task["sufficiency_any"],
            enforce_gate=True,
        )
        self.assertTrue(safe.allowed)

    def test_v2_fresh_prompt_omits_prior_narrative_and_truth(self) -> None:
        task = json.loads(
            (ROOT / "datasets/contradiction_tasks_v2.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        kernel = EpistemicStateKernel(task)
        for event_id in task["visible_event_ids"]:
            kernel.ingest_event(task["evidence_events"][event_id], invalidate_dependents=True)
        fresh = build_v2_prompt(task, "K5", kernel)
        stateful = build_v2_prompt(task, "K3", kernel)
        baseline = build_v2_prompt(task, "K0", kernel)
        prompt_ledger = build_v2_prompt(task, "K1", kernel)
        self.assertNotIn("Prior trajectory narrative", fresh)
        self.assertIn("Prior trajectory narrative", stateful)
        self.assertIn("recorded FACT h1", baseline)
        self.assertIn("UNVERIFIED HYPOTHESIS h1", prompt_ledger)
        self.assertIn('"claim_id": "h1"', fresh)
        self.assertIn('"plan_id": "p1"', fresh)
        self.assertNotIn("correct_choice", fresh)
        self.assertNotIn("forbidden_actions", fresh)

    def test_v2_scripted_conditions_enforce_incremental_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            provider = ProviderConfig(
                name="scripted",
                model="scripted-stubborn",
                api_key_env="UNUSED_KEY",
                responses_url="https://example.invalid/compatible-mode/v1/responses",
            )
            experiment = ExperimentConfig(
                dataset=ROOT / "datasets/contradiction_tasks_v2.jsonl",
                trace_path=directory / "trace.jsonl",
                result_path=directory / "result.jsonl",
                conditions=("K0", "K2", "K3", "K4", "K5"),
                replicates=1,
            )
            run_v2_experiment(provider, experiment, scripted_profile="stubborn", limit=1)
            rows = {
                row["condition"]: row
                for row in (
                    json.loads(line)
                    for line in experiment.result_path.read_text(encoding="utf-8").splitlines()
                )
            }
            self.assertTrue(rows["K0"]["evaluation"]["executed_forbidden_action"])
            self.assertEqual(rows["K2"]["evaluation"]["executed_claim_status"], "contradicted")
            self.assertEqual(rows["K3"]["evaluation"]["executed_plan_status"], "invalidated")
            self.assertFalse(rows["K4"]["evaluation"]["executed_forbidden_action"])
            self.assertTrue(rows["K4"]["evaluation"]["state_safety_success"])
            summary = summarize_v2_recovery(list(rows.values()))
            self.assertEqual(
                summary["paired"]["executed_forbidden_action_rate_K4_minus_K0"]["mean"],
                -1.0,
            )

    def test_compaction_conditions_control_model_visible_contradiction(self) -> None:
        task = json.loads(
            (ROOT / "datasets/contradiction_tasks_v2.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        kernel = EpistemicStateKernel(task)
        for event_id in task["visible_event_ids"]:
            kernel.ingest_event(task["evidence_events"][event_id], invalidate_dependents=True)
        contradiction = task["evidence_events"]["contradiction"]["text"]
        full = build_compaction_prompt(task, "S0", kernel)
        lossy = build_compaction_prompt(task, "S1", kernel)
        narrative = build_compaction_prompt(task, "S2", kernel)
        structured = build_compaction_prompt(task, "S3", kernel)
        compact_delta = build_compaction_prompt(task, "S4", kernel)
        self.assertIn(contradiction, full)
        self.assertNotIn(contradiction, lossy)
        self.assertIn(contradiction, narrative)
        self.assertIn('"status": "contradicted"', structured)
        self.assertIn('"status": "invalidated"', structured)
        self.assertIn(contradiction, compact_delta)
        self.assertIn("CLAIM h1 STATUS=contradicted", compact_delta)
        self.assertIn("PLAN p1 STATUS=invalidated", compact_delta)

    def test_retrace_dataset_and_transaction_invariants(self) -> None:
        dataset = ROOT / "datasets/retrace_tasks_v1.jsonl"
        self.assertEqual(validate_retrace_dataset(dataset), [])
        tasks = {task["scenario_type"]: task for task in load_retrace_tasks(dataset)[:3]}

        stale_task = tasks["stale_irreversible"]
        stale_kernel = ReTraceKernel(stale_task)
        stale = TransactionProposal(1, stale_task["seeded_plan"]["action"], "fix works")
        baseline = stale_kernel.execute(
            stale,
            scenario_type="stale_irreversible",
            enforce_precommit=False,
            enforce_postcondition=False,
        )
        guarded = stale_kernel.execute(
            stale,
            scenario_type="stale_irreversible",
            enforce_precommit=True,
            enforce_postcondition=False,
        )
        self.assertEqual(baseline.status, "committed")
        self.assertFalse(baseline.world_safe)
        self.assertEqual(guarded.status, "aborted")
        self.assertTrue(guarded.world_safe)

        post_task = tasks["postcondition_failure"]
        post_kernel = ReTraceKernel(post_task)
        post = TransactionProposal(
            post_kernel.claim_revision,
            post_task["safe_actions"][0],
            "acceptance condition holds",
        )
        unchecked = post_kernel.execute(
            post,
            scenario_type="postcondition_failure",
            enforce_precommit=True,
            enforce_postcondition=False,
        )
        checked = post_kernel.execute(
            post,
            scenario_type="postcondition_failure",
            enforce_precommit=True,
            enforce_postcondition=True,
        )
        self.assertEqual(unchecked.status, "committed")
        self.assertFalse(unchecked.world_safe)
        self.assertEqual(checked.status, "rolled_back")
        self.assertTrue(checked.world_safe)

        concurrent_task = tasks["concurrent_revision"]
        concurrent_kernel = ReTraceKernel(concurrent_task)
        proposal = TransactionProposal(
            concurrent_kernel.claim_revision,
            concurrent_task["safe_actions"][0],
            "safe action succeeds",
        )
        concurrent_kernel.inject_concurrent_revision()
        conflict = concurrent_kernel.execute(
            proposal,
            scenario_type="concurrent_revision",
            enforce_precommit=True,
            enforce_postcondition=True,
        )
        self.assertEqual(conflict.status, "aborted")
        self.assertTrue(conflict.stale_read)

    def test_retrace_six_worker_scripted_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            provider = ProviderConfig(
                name="scripted",
                model="scripted-adaptive",
                api_key_env="UNUSED_KEY",
                responses_url="https://example.invalid/compatible-mode/v1/responses",
            )
            experiment = ExperimentConfig(
                dataset=ROOT / "datasets/retrace_tasks_v1.jsonl",
                trace_path=directory / "trace.jsonl",
                result_path=directory / "result.jsonl",
                conditions=("T0", "T1", "T2", "T3", "T4", "T5"),
                replicates=1,
            )
            run_retrace_experiment(
                provider,
                experiment,
                scripted_profile="adaptive",
                limit=3,
            )
            rows = [json.loads(line) for line in experiment.result_path.read_text().splitlines()]
            self.assertEqual(len(rows), 18)
            self.assertEqual({row["condition"] for row in rows}, set(experiment.conditions))
            for condition in experiment.conditions:
                self.assertTrue(
                    (directory / ".retrace_result_shards" / f"{condition}.jsonl").exists()
                )
            summary = summarize_retrace(rows)
            self.assertEqual(summary["conditions"]["T0"]["overall_success_rate"], 0.0)
            self.assertEqual(summary["conditions"]["T4"]["overall_success_rate"], 2 / 3)
            self.assertEqual(summary["conditions"]["T5"]["overall_success_rate"], 1.0)

    def test_native_conversion_does_not_invent_unknown_provenance(self) -> None:
        normalized = normalize_event({"type": "summary", "text": "maybe cache"})
        self.assertIsNone(normalized["is_external_evidence"])
        self.assertEqual(normalized["event_type"], "summary")


if __name__ == "__main__":
    unittest.main()

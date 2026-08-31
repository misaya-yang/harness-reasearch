# Harness Self-Correction Research

This directory is the independent research workspace for the plan at
`/Users/yang/Downloads/HARNESS_SELF_CORRECTION_RESEARCH_PLAN.md`.

The working question is:

> Why can a strong model often repair an error after a user points it out, yet fail to discover and correct the same error during an autonomous long-horizon trajectory?

The first implementation treats the answer as an empirical question. It keeps
external observations, model hypotheses, plans, decisions, summaries, and final
claims separate in the trace so that provenance loss can be measured instead
of inferred from a final pass rate.

## Current scope

- Seven local agent repositories are audited in `repo_audit/`.
- The lowest-cost C0–C6 belief-probe experiment is implemented first.
- A condition-driven long-horizon runner and deterministic mock environment are
  included for the B0–B6 pilot.
- Trace and metric scripts are dependency-free Python 3.11+ programs.
- Live `qwen3.8-flash` C0-C6 and mock B0-B6 pilot runs were completed on
  2026-08-30. Frozen manifests and metrics are under `results/`; the execution
  boundary and stop decision are recorded in `reports/12_sol_execution_report.md`.
  The final synthesis, revised v2 design, and proposed harness optimization are
  in `reports/13_final_experiment_report.md`.
- A 20-task v2 contradiction/compaction follow-up was completed with
  `qwen3.8-flash` and `deepseek-v4-flash-0731`. The newest results and compact
  typed-delta optimization are in `reports/14_followup_experiment_results.md`;
  the frozen run index is `results/20260830_v2_final/run-index.json`.
- A real end-to-end Pi coding-agent paired study was completed on six historical
  repository repairs. Native H0 scored 6/6 and the raw external-delta H1 scored
  4/6, while H1 reduced timeouts. The corrected results, failure analysis, and
  proposed provenance-weighted reconciliation method are in
  `reports/16_pi_native_trajectory_results.md`.
- A harder six-task Pi follow-up tested broad reconciliation, validation-only
  reconciliation, and evidence-certified completion. Native H0 scored 5/6;
  H2/H3/H4 scored 2/6, 4/6, and 4/6, with no strict-completion gain. The result
  closes the prompt/state-projection branch and proposes an offline
  Evidence-Bounded Commit Protocol in
  `reports/17_pi_reconciliation_and_completion_followup.md`.
- The follow-up EBCP implementation passed offline/native mechanism tests, but
  did not produce a positive end-to-end result. Early live batches exposed CPU,
  validation-classifier, and tool-allowlist integrity failures; the later
  model-visible optional commit tool still produced zero commit attempts after
  repeated validation. The negative result, invalid-batch ledger, and harness
  postmortem are in
  `reports/18_pi_ebcp_negative_result_and_integrity_postmortem.md`.

## Alibaba Cloud / Qwen configuration

The supplied Anthropic-app URL is not used as an Anthropic Messages endpoint by
the experiment runner. For the Singapore workspace host, the OpenAI-compatible
base URL is:

```text
https://ws-smqn3wel83c2p9wd.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
```

The raw Responses endpoint is:

```text
https://ws-smqn3wel83c2p9wd.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/responses
```

The live experiments keep exact configured model IDs. The tested low-cost IDs
are `qwen3.8-flash` and `deepseek-v4-flash-0731`; workspace/account entitlement
and returned model ID remain live smoke-test gates. The runners do not silently
substitute another model.

## Layout

```text
configs/       Provider and experiment configuration templates
datasets/      Seed tasks and dataset construction notes
adapters/      Native-trace conversion contracts
runners/       Responses client, belief probe, and long-horizon runner
metrics/       Deterministic trace/result analyses
repo_audit/    Source-grounded audit for each open-source agent
reports/       Research reports and result templates
traces/        Runtime JSONL traces (ignored by default)
results/       Runtime result files (ignored by default)
tests/         Offline tests that require no API key
```

## No-key checks

From this directory:

```bash
python3.11 -m unittest discover -s tests -v
python3.11 -m runners.run_belief_probe --config configs/experiment.default.json --dry-run --limit 2
python3.11 -m runners.validate_dataset datasets/seed_belief_tasks.jsonl
python3.11 -m runners.validate_v2_dataset datasets/contradiction_tasks_v2.jsonl
```

The dry run validates request construction and never reads an API key. It also
does not contact the provider.

## Keyed runs

Inject the key only in the process environment. Do not place it in a file or
command history:

```bash
export ANTHROPIC_AUTH_TOKEN='provided-at-test-time'
python3 -m runners.run_belief_probe \
  --config configs/experiment.default.json \
  --conditions C0,C1,C2,C3,C4,C5,C6 \
  --replicates 3
```

The runner reads the environment variable named by `api_key_env` in the config,
redacts authorization failures, and never writes the key to trace output.

For a provider-only compatibility check, use:

```bash
python3.11 -m runners.provider_smoke --config configs/experiment.default.json
```

See [`reports/11_provider_setup.md`](reports/11_provider_setup.md) for the
endpoint mapping and model-ID gate.

## Evidence discipline

Every report labels claims as `FACT`, `OBSERVATION`, `INFERENCE`,
`HYPOTHESIS`, or `PROPOSAL`. A successful HTTP call is provider connectivity
evidence only; it is not evidence that a harness condition improved task
performance.

# harness-reasearch

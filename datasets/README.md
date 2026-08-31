# Dataset contract

The seed task file is deliberately small and inspectable. It is a mechanism
pilot, not the planned 100–300-task benchmark. Each row contains:

- `task_id`, `domain`, and `question`;
- `evidence`, which is the only initial evidence sent to the model;
- `options`, a mapping from choice ID to hypothesis;
- `target_hypothesis`, the intentionally misleading hypothesis used for the
  self-conditioning conditions;
- `ground_truth`, which is evaluator-only metadata and is never inserted into
  the model prompt;
- `discriminating_evidence`, which documents what would resolve the ambiguity.

Before a pilot is treated as evidence, freeze the dataset version, expand it to
the planned domains, and review every task for multiple plausible explanations.
`python3.11 -m runners.validate_dataset` rejects a seed row whose target
hypothesis equals its hidden ground truth, preventing the self-conditioning
probe from accidentally becoming a correctness-only test.

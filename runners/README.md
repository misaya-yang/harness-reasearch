# Runners

`belief_probe.py` implements the C0–C6 paired probe. It sends the same task
evidence under different provenance framings and records the model's structured
choice without exposing evaluator-only ground truth in the prompt.

`long_horizon.py` implements the B0–B6 pilot over the deterministic mock
environment in `mock_env.py`. It is intentionally not a production adapter for
any of the seven agent repositories. Native harnesses should first be run in
their own supported environment and then converted through the adapter contract.


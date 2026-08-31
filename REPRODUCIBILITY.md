# Reproducibility

## Environment

- Python 3.11 or newer.
- No runtime dependency is required for the offline runner and metrics.
- A live run requires an Alibaba Cloud Model Studio API key for the Singapore
  region and access to the exact configured model ID.
- The repository records the exact model, endpoint, date, condition, replicate,
  and request/response usage in each result row.

The initial task/config snapshot is listed in `datasets/SHA256SUMS`. After
expanding the benchmark, create a new dataset revision and update its digest
before any keyed run.

## Provider boundary

The original URL supplied for an Anthropic application is transformed for the
OpenAI-compatible API by changing the path suffix:

```text
/apps/anthropic
  -> /compatible-mode/v1/responses
```

For SDKs that accept a base URL, use the path without `/responses` and let the
SDK append it. The standalone runner takes the full endpoint so the request
target is unambiguous.

The model name `qwen3.8-flash` is a user-selected experimental setting. It is
not replaced automatically if the provider rejects it. Record any provider
model-list response or error as a separate compatibility observation.

## C0–C6 belief probe

Each task has a hidden evaluator-only ground truth and 2–3 plausible hypotheses.
The model receives the same raw evidence under every condition. C1–C6 add only
the specified provenance or hypothesis framing; they do not add external facts.
Responses are requested as JSON with a choice, relative confidence, surviving
alternatives, and an explicit `needs_more_evidence` field.

The primary paired comparison is within `(task_id, replicate)`:

- `UBA`: target-hypothesis belief after a condition minus C0.
- `self_vs_other`: C1 minus C3 for the same misleading hypothesis.
- `provenance_protection_gain`: C4 minus C1, with lower unsupported commitment
  treated as protection.
- `alternative_survival`: whether a non-target plausible hypothesis remains in
  the structured response.

## Long-horizon pilot

The mock environment is intentionally deterministic and small. It separates
tool observations from model narrative and supports B0–B6 prompt conditions.
It is a mechanism pilot, not a claim about production harness performance.
Before scaling, freeze task fixtures, use paired seeds, and publish the exact
task/evaluator version.

## Trace contract

Each event is JSONL and includes `run_id`, `task_id`, `condition`, `step`,
`source`, `event_type`, `content`, `is_external_evidence`, `parent_ids`,
`belief_state`, `token_usage`, and `timestamp`. Secrets and authorization
headers are excluded by construction.

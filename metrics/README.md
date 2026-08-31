# Metrics

The scripts consume JSONL and produce JSON. They use paired task/replicate
comparisons and deterministic bootstrap seeds. Missing or invalid model rows
are reported and excluded from condition estimates rather than silently scored
as failures.

The provenance, compaction, and error-propagation scripts are deliberately
conservative: they report what explicit trace fields establish and do not claim
to infer semantic facts from arbitrary natural-language text.

Usage fields and latency are aggregated when the provider returns them. Dollar
cost is intentionally not guessed; a frozen price sheet must be supplied as a
separate analysis input for cost-normalized reporting.

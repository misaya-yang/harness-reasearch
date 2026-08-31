# R5 — mutation-initiation rescue (pre-registered protocol)

Status: FROZEN 2026-08-31, before any R5 run is launched.

## Question

Round 2: thinking-toggle and post-tool-compaction-order both 0/3 native with
a mutation-initiation failure signature (D→I collapse: long read/bash
exploration, zero or near-zero edits, T1 fires in a tight 148–168s band).
R5 decomposes the failure into KNOWLEDGE vs INITIATION:

- If an oracle card naming the gold edit surface rescues pass behavior but a
  probe card does not, the bottleneck is knowledge discovery (D), not
  execution.
- If the oracle card also fails, the bottleneck is downstream of knowing
  where to edit (implementation/validation/termination under this budget).

## Arms and materialization

| arm | intervention at T1 | information content |
|---|---|---|
| native (baseline) | none | Round-2 runs, clean tier only |
| kac | probe-generated decision card (fresh context, frozen probe machinery) | state-derived only |
| hint | oracle card naming the gold edit surface + paraphrased fix direction (run_hint.py, ORACLE_CARDS) | gold-derived BY DESIGN — ceiling probe, never a KAC claim |

All arms: qwen3.8-flash, thinking off, 420s budget, full tool set +
validate_current_patch, sanitized overlay validator. hint/kac cards share
format_card_steer (format-comparable). Documented asymmetry: hint cards land
immediately at T1; kac cards land at T1 + probe latency.

Oracle-card hygiene: source files + natural-language direction only; never
the gold diff; never regression-test file names (absent at base for both
tasks — dangling references would be non-actionable oracle structure).
2026-08-31 refinement (analysis/notes/oracle-gap.md): the "absent at base"
claim is exact only for thinking-toggle; for compaction the test FILES
exist at base but all 5 gold `-t` assertions are absent (base runs are
green-but-blind). ORACLE_CARDS verified free of test names and `-t`
patterns. Reading of R5 results must treat validation-onset as a mediating
variable (oracle gap: no stock command produces a failing signal at base).

## Execution

2 tasks × {kac, hint} × 2 replicates = 8 new runs; baseline = Round-2 clean
native runs (audit tiers re-verified 2026-08-31 with the hardened scanner):
thinking-toggle r2, r3 (0/2 pass; r1 excluded harness-tier);
post-tool-compaction r2, r3 (0/2 pass, clean; r1 reclassified harness-tier
by result-blob scanning of node module-resolution errors — reported
separately per the inclusion rule below, not pooled into the clean baseline).
run_batch `--kac/--hint`.

## Analysis (frozen)

- Small-n pilot: no hypothesis tests; report pass counts per arm, per-task
  tables, and first-mutation/validation times. Bounds language only.
- Inclusion: all runs with a report; leak tier audited; gold-tier runs
  excluded-with-report; harness-tier runs reported separately (they touched
  run internals but no gold material).
- Interpretation rule (pre-registered): "rescue" = ≥1/2 passes in an arm
  while the clean native baseline for that task is 0. hint-rescue without
  kac-rescue ⇒ knowledge bottleneck; neither rescues ⇒ initiation/execution
  bottleneck beyond surface knowledge; kac-rescue ⇒ probe machinery suffices.
- If both tasks show hint-rescue without kac-rescue, the trigger-time fork
  (R3) remains the unbiased KAC estimator; R5 only explains WHY native fails.

## Outcome (2026-08-31, batch r5-rescue-2; scored under the rules above)

8/8 runs completed, 0 nonzero exits, ZERO passes in every arm under every
inclusion variant (kac: 1 run gold-excluded — first tool call navigated to
the pi checkout via the system-prompt disclosure channel; hint: 2 runs
harness-flagged, reported separately). Neither arm rescues ⇒ pre-registered
verdict: initiation/execution bottleneck beyond surface knowledge. Full
tables and process descriptives: analysis/round-5.md. One audit soundness
fix applied PRE-scoring (audit_leaks self-attribution path-extent
expansion; direction conservative — only removes false harness flags).

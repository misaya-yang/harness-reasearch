# Trigger-time fork design (Round 3) — pre-registered protocol

Status: FROZEN 2026-08-31; AMENDED 2026-08-31 (replay mode, see below).

## Estimand

ATE_KAC|trigger at the trigger-time frozen state. Two child modes:

1. **replay** (primary): pi supports exact transcript forking
   (`--fork <path>` → `SessionManager.forkFrom`, verified in pi source
   `main.ts:342`, `session-manager.ts:1581`; rpc-entry routes through the same
   `main()`). Donors run with `persist_session=True`; at trigger time the live
   session JSONL is copied (truncated at last newline) and each child starts a
   new pi process with `--fork <snapshot>` — workspace tree AND full transcript
   identical to the donor at the trigger instant. Children get ONE identical
   RESUME_PROMPT (a forked process is idle and needs input to continue); arms
   differ only in the card steered afterwards. Estimand:
   ATE(arm=kac vs arm=none | trigger, exact frozen state).
2. **reconstruction** (fallback, also the mode of record for donors that ran
   before persist_session existed): canonical-state prompt (task + GPS render
   + diff + captured sources + observations), no transcript. Estimand:
   ATE(arm=kac vs arm=none | trigger, reconstructed state) — reported
   separately, never pooled with replay children.

arm=sham isolates the interruption/format effect. Secondary contrast:
arm=none children vs the donor's own continuation (donor = same state, same
history, no resume prompt) — divergence there measures the resume-prompt
effect; the reconstruction-mode none arm vs donor additionally probes
history dependence (Agent Hysteresis direction).

Known replay caveat (documented risk): the snapshot may end mid-turn
(pending tool call); if the provider rejects the resumed conversation the
child exits with reason=error and is excluded-with-report (handling rule 2).

## Frozen constants

| constant | value | where |
|---|---|---|
| STEER_DELAY_SECONDS | 25.0 (equal for both steered arms) | run_fork_child |
| MIN_CHILD_BUDGET_SECONDS | 90.0 (below → child skipped, logged) | run_fork_child |
| probe for kac card | kact.run_probe, tools=(), 120s budget | run_fork_child |
| sham card | SHAM_CARD dict, format_card_steer format-identical | run_fork_child |
| child triggers | inert (TriggerConfig(inf, 1e9)) | run_fork_child |
| donor arm | pure native, NO injections | run_fork_donor |
| snapshot method | APFS clonefile, commit+tag `kvc/fork-snapshot-*` | fork_collect |
| session snapshot | live JSONL copy, truncate last newline, ≥512B to use | fork_collect |
| resume prompt (replay mode) | RESUME_PROMPT, byte-identical across arms | run_fork_child |
| primary outcome | binary: any `kvc_validation result=pass` in child events | run_fork_child |

## Pre-registered analysis decisions (no post-hoc changes)

1. **Primary contrast**: kac vs none, pooled over triggers within a donor
   task; sham reported secondarily. Test: two-sided permutation test on
   paired (spec-level) differences, 10k permutations; report the exact-p
   alternative (binomial/mid-p) if permutation space is small. Effect size:
   risk difference with Wilson 95% CI per arm.
2. **Inclusion**: all children of clean-tier donors whose process exited with
   a report (including budget exhaustion). Children skipped for
   MIN_CHILD_BUDGET, process error, or replay refusal are listed but
   excluded. kac children whose probe produced no card run unsteered and are
   analyzed as a separate labeled group ("kac-nocard"), never silently
   merged. No outcome-based exclusions. Replay and reconstruction children
   are analyzed as separate strata.
3. **Multiplicity**: per-task analyses are exploratory; the single primary
   test is pooled kac-vs-none on the Round-3 donor task
   (`pi-find-root-relativization`).

## Known validity caveats (documented, not fixable now)

- Children start with reconstructed state, not replayed history → estimand
  above, not the literal donor-process effect.
- Real donor cards arrive trigger+probe-latency; child cards arrive
  child_start+25s. Comparable across steered arms; donor-vs-child timing
  asymmetry noted in any interpretation.
- Snapshot commits the donor tree at trigger instant; any in-flight write by
  the donor during the ~1s clone window could be captured half-written
  (accepted risk, clonefile window is sub-second).

## Round-3 execution shape

- Donors: `run_fork_donor --task pi-find-root-relativization` ×4 (clean-tier
  required; task chosen because T1/T2 actually fire there, unlike the 60%
  ceiling task).
- Children: 3 arms × 2 children per fork spec via `run_batch --fork-specs`.
- All runs audited (`audit_leaks`); only children of clean donors enter
  statistics; TAINT donors' specs are quarantined.

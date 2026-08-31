# Round 1 — KAC first live (pi-retry-attempt-timeout, n=3)

Batch `round1-kac-cal` (stamp 20260831-114543), budget 420s, qwen3.8-flash thinking-off.

## Results

| run | reason | epochs | validations | triggers | cards_injected | cards_accepted |
|---|---|---|---|---|---|---|
| kac-r1 | settled | 24 | 3 | T2@epoch0 | 0 | 0 |
| kac-r2 | budget | 3 | 0 | — | 0 | 0 |
| kac-r3 | budget | 0 | 0 | T1 | 0 | 0 |

Manifests all free of pi_repo. Leak audit (as of writing): all three clean.
**Later correction** under the upgraded audit (sibling-run marker):
kac-r3-114543 is **harness tier → TAINT** (it read a sibling run's workspace;
gold_count=0). Clean Round-1 KAC set is r1/r2-114543 only. See round-2.md.

## Primary finding: the probe produced zero output

The single fired probe (kac-r1, T2@epoch0) ran its full 120s budget and returned
**0 output chars / card_parsed=False**. Its event log shows the cause: with a
`read` tool and an (intentionally empty) probe workspace, the model looped
issuing `read` calls against nonexistent paths (`ENOENT .../workspace/README.md`)
until budget, never emitting the decision card.

Secondary: the probe context itself was near-empty — at epoch0 there is no diff
and no changed/read files yet, so `{diff}=(no changes yet)` and
`{sources}=(no source collected)`. Even a well-behaved probe would have had
little to work with.

## Fixes applied (for Round 1 re-run)

1. **Probe tool surface → none.** `tools=()` in `kact._run_probe`. The probe's
   evidence is complete in the prompt; any tool invites workspace exploration
   that burns the budget. (pi CLI `--tools ""` filters to an empty allowlist,
   verified in `args.ts`.)
2. **Repo file-index fallback** in `collect_probe_inputs`: when nothing has been
   changed or read yet, embed `git ls-files` (capped 400 lines) so the probe can
   localize the edit surface from structure alone.

## Control-arm context (clean native baseline, n=5)

**Corrected number (was "4/5" in error; the table below this in round-2.md is
authoritative): 3/5 = 60%** of the post-sanitization clean native runs pass the
frozen verifier (r3-110139 pass@19, r1-113034 pass@9, r3-113034 pass@13;
r1-110139 and r2-113034 fail). Control Loss small on the passing runs
(6.3s / 21.5s / 20.2s after first passing validation). r3 (110139) remains the
flagship: 20 epochs, passing patch at epoch 19, settled.

## Next

Re-run KAC n=3 with the fixed probe (Round 1b), then Round 2 native on the three
DEV tasks (find-root-relativization, thinking-toggle-preserves-bash-output,
post-tool-compaction-order). All v3 tasks already calibrated (base-fail/gold-pass).

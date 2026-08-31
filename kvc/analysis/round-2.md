# Round 2 — DEV native baselines + corrected flagship baseline (2026-08-31)

Batch `round1b-2-kac-native` (stamp 20260831-115926), budget 420s,
qwen3.8-flash thinking-off. Supersedes the provisional numbers in round-1.md.

## Correction first: the flagship baseline is 3/5 = 60%, not 4/5

Earlier documents wrote "4/5 pass" while their own table listed 3 passing and
2 failing runs. The correct post-sanitization clean baseline for
`pi-retry-attempt-timeout` is:

| run | audit tier | validations | result |
|---|---|---|---|
| r1-110139 | clean | fail@0, fail@0 | FAIL |
| r3-110139 | clean | **pass@19** | PASS (flagship, settled) |
| r1-113034 | clean | fail@0, **pass@9** | PASS (settled) |
| r2-113034 | clean | fail@13 | FAIL |
| r3-113034 | clean | fail@6, **pass@13** | PASS (settled) |

**3/5 = 60% pass** (3/6 = 50% if the never-validated run-0 is counted as fail).
Excluded from the baseline by audit tier: r2-110139 and both 102341 runs are
**gold tier** (pre-sanitization era; actors `git show`'d the gold commit in the
real pi clone); all 105312 runs lack reports (pre-validator-fix infra batch);
run-0 never ran the verifier. Control Loss on the three passing runs:
**6.3s / 21.5s / 20.2s** from first passing validation to settle — the V→T
transition probability p_T ≈ 1 once a passing patch exists.

## Round 2 DEV native results (n=3 each)

| task | run | reason | epochs | vals | triggers | audit |
|---|---|---|---|---|---|---|
| find-root-relativization | r1 | budget | 0 | 0 | T1 | clean |
| find-root-relativization | r2 | budget | 6 | 2 | T2@0, T1, T2@5 | clean |
| find-root-relativization | r3 | settled | 5 | 1 | — | **harness → TAINT** |
| thinking-toggle | r1 | budget | 0 | 1 | T1, T2@0 | harness (benign) |
| thinking-toggle | r2 | budget | 4 | 0 | T1 | clean |
| thinking-toggle | r3 | budget | 0 | 0 | T1 | clean |
| post-tool-compaction | r1 | budget | 1 | 0 | T1 | clean |
| post-tool-compaction | r2 | budget | 0 | 0 | T1 | clean |
| post-tool-compaction | r3 | budget | 0 | 0 | T1 | clean |

Audit details:
- **find-root r3 TAINT**: actor left its workspace and read a sibling run's
  `results/kvc` tree plus `/tmp/vlog` debug files (gold_count=0, but protocol
  says exclude). Its "settled" outcome therefore does NOT count: the clean
  settled rate for find-root is **0/3**, raw 1/3.
- **thinking-toggle r1**: relative-path self-read of its own sanitized
  validator config (`cd ..; cat run/validator/task.json`) — benign in content
  (gold refs already stripped), but tier=harness; excluded, noted.
- Every gold-ref leak path found this round has gold_count=0: the sanitized
  task.json + base-mirror closure held; the remaining issue is sibling-run
  visibility (workspace lives inside the results tree), not gold leakage.

## Finding 1: mutation-initiation failure, not an activation gap

The clean DEV picture: 2/3 tasks (thinking-toggle, post-tool-compaction) show
0/3 settled with heavy **T1 firing** (≥35% budget consumed with zero production
mutations). Agents read extensively, occasionally validate the *unmodified*
base (thinking-toggle r1 ran a validation with 0 epochs), but do not initiate
edits. This is a failure to **start mutating** (D→I transition), distinct from
"model lacks the knowledge" (the knowledge-activation claim from round 1):
the actors demonstrably process the task and localize broadly, yet the first
committed edit either never happens or happens too late to iterate.

find-root r2 is the informative exception: it mutated (6 epochs) and validated
twice but could not reach pass in budget — an I→V bottleneck rather than D→I.

## Finding 2: KAC wiring works; the ceiling task rarely triggers (Round 1b)

KAC arm on pi-retry-attempt-timeout (n=3, stamp 115926), probe fixed per
round-1.md (tools=(), file-index fallback):

| run | reason | epochs | vals | triggers | cards |
|---|---|---|---|---|---|
| kac-r1 | settled | 9 | 1 | — | 0 |
| kac-r2 | budget | 10 | 2 | T2@epoch10 | 0 (teardown race) |
| kac-r3 | budget | 7 | 0 | — | 0 |

All clean. The only trigger (T2@epoch10, kac-r2) fired so late that its probe
finished after the actor's budget; the teardown race swallowed the injection —
fixed since (commit 5e5a125: kill-guard before steer; card still recorded with
`accepted=False`). Root cause of zero cards: on a 60%-pass task the actor is
healthy enough that triggers almost never fire. KAC must be evaluated on
mid-band tasks (find-root) and on the 0/3 tasks where T1 fires constantly.

Also corrected from round-1.md under the upgraded audit: kac-r3-**114543** is
harness tier (read a sibling run's workspace), TAINT; round-1's own clean
KAC pair is r1/r2-114543.

## Framing (per external critique, adopted)

Phase transitions: p_D=P(D|S), p_I=P(I|D), p_V=P(V|I), p_T=P(T|V);
P(success)≈product. Data: p_T≈1 (control loss ≤21.5s); the DEV bottleneck is
D→I (mutation initiation) on 2/3 tasks and I→V on find-root. The round-1 claim
is downgraded accordingly: *failure is not fully explained by lack of
knowledge; an independent transition failure exists, and these observations
motivate (but do not yet establish) transfer control.* Causal evidence
requires the trigger-time fork design (Round 3, see PLAN appendix B).

## Next (restructured rounds)

- **R3**: trigger-time fork on find-root (mid-band, triggers fire): freeze
  snapshot at trigger, fork KAC card vs sham vs no-intervention, ATE.
- **R4**: KAA probes (D/V at C0/C1/C2) + static know≠act probe on T1 runs.
- **R5**: mutation-initiation rescue attempts on the 0/3 tasks.
- **R6**: freeze manifest + final report.
- Stop burning pi-retry-attempt-timeout replicates (known ceiling ~60%).

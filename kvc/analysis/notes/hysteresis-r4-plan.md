# Hysteresis R4 — know≠act scoring and diagnosis-divergence protocol

Written 2026-08-31 (pre-rerun, read-only analysis; treat recovered texts as preliminary).
Scope: the 5 clean zero-mutation C0-D probes of tonight + what is computable from existing
retry-attempt-timeout pass runs for the mutated-trajectory divergence measure.

## 1. know≠act rate — definition and tonight's score

### Definition (pre-registered wording for R4)
For a run r with a D probe at checkpoint C_k that is **answerable** (probe run reached
`agent_end`, extraction OK, JSON parses) and an actor with **zero mutations over the scored
interval** (main-run `mutation_epoch == 0` at `kvc_terminate`, or epoch delta 0 over the
interval between C_k and probe time):

    KNAct = #{r : probe_k names ≥1 gold surface ∧ actor mutations = 0}
            / #{r : probe_k answerable ∧ actor mutations = 0}

Gold-surface match per `probes.py::score_d_probe` against `GOLD_EDIT_SURFACE_HINTS`
(full path or basename, case-folded, over the parsed JSON blob). Denominator conditions
on answerability, NOT on probe success — **censoring is reported as a separate statistic**
(`probe_timeout_rate`); conflating the two is what tonight's 0-byte files would have caused.

### Score on recovered data (authoritative text = `kvc_run.last_assistant_text`)
| run | C0-D outcome | parsed | surface | know≠act? |
|---|---|---|---|---|
| find-root-donor-r1-132101 | settled, 1762 chars | yes | 1/1 (`.../core/tools/find.ts`, with the exact `startsWith`+`slice(len+1)` blocks) | **YES (1)** |
| thinking-toggle-native-r3 | `reason: budget`, no agent_end | no | — | censored |
| compaction-order-native-r2 | `reason: budget` | no | — | censored |
| compaction-order-native-r3 | `reason: budget` | no | — | censored |
| find-root-native-r1 | `reason: budget` | no | — | censored |

**Important correction to the premise:** the four empty `probe-output.txt` files are NOT
(only) the extraction bug. Their `kaa/C0-D/run/events/events.jsonl` contain **zero
`agent_end` frames** and a terminal `kvc_terminate reason=budget` (120 s then 240 s for
thinking-toggle — two probe attempts in one file). The probes burned 40–133 tool
executions stuck in GPS `phase: localize` and never emitted the answer JSON (longest
assistant text in these files < 200 chars). Only donor-r1 reached `agent_end`.
So tonight's KNAct = 1/1 **conditional on answerability**, with censoring 4/5.

Donor actor confirmation (know≠act positive case): main run terminated `reason=budget`,
`mutation_epoch: 0`, `delivered: false`, still `phase: localize` at 420 s. The state-only
fresh-context policy produced a gold-accurate diagnosis; the history-carrying policy made
zero mutations. One unambiguous divergence event.

### Probe reliability issue to fix before R4 (independent of the extraction rerun)
The D template says "read at most a few files" but nothing enforces it; probes do
dozens–100+ tool calls. R4 candidates: hard tool-call cap in the probe harness, budget
raise (D currently V=240 s class), or a two-stage budget (localize→answer checkpoint
forcing emission of the JSON if elapsed > 60% of budget).

## 2. Diagnosis-divergence on mutated trajectories — protocol sketch (D@checkpoint vs actual next edit)

Data reality check, done tonight:
- The six pass runs (`retry-attempt-timeout-native-r{1,2,3}-20260831-{110139,113034}`)
  have **no kaa/ material at all** (kaa exists only on tonight's 5 runs, C0-D only).
  Nothing here was ever probed; the protocol is executable but yields zero rows tonight.
- `probes.py` checkpoints: C0=base, C1=first incumbent commit (else HEAD), C2=HEAD.
  **C2 is terminal**, so "where the trajectory actually edited next" does not exist at C2
  for a settled run; 4/6 pass runs have exactly one incumbent tag, and it is at/near the
  end (r3-113034: single tag `kvc/incumbent-13`), so C1-vs-next-edit is also degenerate.
- Fix: use **validation-epoch checkpoints**, which exist in the event stream — e.g.
  r3-113034 has `kvc_validation epoch=6 result=fail` and `epoch=13 result=pass`, plus 14
  `kvc_epoch` frames. Edit boundaries mid-run are fully observable.

### Protocol (all computable offline from existing artifacts)
1. **Pick checkpoint T** = frame index of a mid-run `kvc_validation` (first fail, and first
   pass if not final) in `run/events/events.jsonl`. Skip runs where T is the last edit event.
2. **Reconstruct state s(T)**: apply, in order, the `tool_execution_start` edit/write
   calls (path + old/new in args — extraction verified working, 11 calls in r3-113034)
   onto the base tree in a scratch clone (never touch the run workspace). Deterministic
   replay; flag any non-replayable call (missing strings) as excluded.
3. **Actor's next action set S_A(T)**: paths of the first m=3 edit calls after T
   (m=1 sensitivity too).
4. **Probe set S_D(T)**: fresh-context D probe on s(T) (same template, observations
   truncated to ≤ T), recovered via fixed extraction + `last_assistant_text` fallback;
   parse `edit_surface`, normalize to repo-relative paths (substring/basename match as in
   `score_d_probe`).
5. **Divergence event** per run-checkpoint: `div = 1[bases(S_D) ∩ bases(S_A) = ∅]`,
   scored with gold-membership of each side — the informative cell is
   S_D ∩ gold ≠ ∅ ∧ S_A ∩ gold = ∅ (history-carrying policy edits wrong place while
   state-only policy names right one — the donor pattern in mutated form) and its mirror
   (probe worse than actor = base rate for "probe is just noisy").
6. **Statistics**: pooled divergence rate with Wilson CI; paired probe-vs-actor comparison
   across the same states via the stratified permutation machinery already in
   `kvc.analysis.fork_stats` (Wilson + exact permutation + the documented p ≥ 2·2^−B
   small-n floor — reuse, don't rewrite).
Cost: probe calls only (n runs × 1–2 checkpoints); everything else is offline parsing.

## 3. Mapping to the hysteresis framing; the <2% kill criterion

- A D probe at s(C_k) is the **state-only policy** π(a|s); the actor continuing with the
  raw transcript is π(a|h,s) on the same s. Donor-r1 is an equivalent-state pair where the
  history h carried zero task-relevant residue beyond s (420 s of localize, 0 mutations,
  0 validated epochs) yet the policies diverged maximally: probe → gold diagnosis;
  actor → no action. That is a hysteresis event in the strongest form (history-induced
  freeze), n = 1.
- Section-2 pairs are the same measurement in the "real" regime (h contains committed
  edits), where h may be load-bearing — hence the gold-membership cross-tab, which is
  what distinguishes hysteresis from justified history use (the reviewer objection from
  the lit scan: "same s, different h" is only a violation if h carries no residual
  information; the cross-tab plus the validation-epoch context is how we show it).
- **What tonight's data cannot say**: anything about the rate. n=1 answerable probe →
  Wilson 95% CI for KNAct is [0.20, 1.00]; the 95% lower bound sits above 2%, so tonight
  **cannot support and cannot kill** the hypothesis. The lit-scan objection hierarchy
  applies directly: the donor case is one observation, the actor stall is confounded with
  budget termination (a kill-on-timeout harness artifact must be ruled out before reading
  "no mutation" as policy, not artifact).
- **What it can say**: (i) divergence between state-only and history-conditioned policy is
  observable with the current instrumentation (one clean event); (ii) the binding
  constraint for R4 is **probe answerability** (80% censoring tonight), not the effect;
  (iii) the pass-runs already contain every offline input Section 2 needs except the
  probe calls themselves — so an R4 that fires 6–10 validation-epoch D probes is the
  cheapest path to a first CI on the divergence rate.
- Kill-criterion restatement: to ever legitimately kill at "<2%", the estimand needs
  ≥ ~150 evaluable equivalent-state pairs with censoring < 5% (rule-of-three: 0/150
  upper bound = 2%). At tonight's throughput (1/5 evaluable per session night) that is a
  R4–R6 program, not an R4 result; the <2% criterion should be declared **in force but
  untested** and R4 pre-registers the probe-reliability gate as a prerequisite
  (no kill/keep decision while censoring > 20%).

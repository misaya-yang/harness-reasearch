# Fork integrity review — incident audit + T2 fidelity calibration (2026-08-31, read-only pass)

## Part 1 — what currently defends, and the remaining leak paths

Defenses today: (a) quarantined specs physically live in `results/_quarantine/`, outside the
spawn glob root; (b) `fork_stats.donor_tiers()` re-audits each donor and drops children whose
donor tier != `clean`; (c) `audit_leaks` gold/harness markers. Findings, worst first:

**A1 (HIGH, live now): the spawn gate is directory location only.**
`run_batch.build_jobs` (`run_batch.py:250`) does `Path(args.fork_specs).rglob("fork-spec.json")`
— name-based, unbounded depth, no tier check, no path validation. The live batch was launched
`--fork-specs results/kvc`, i.e. the root that also holds every child run dir and every
`snapshot-*` tree. Any re-copy of a quarantined spec anywhere under that root (the incident's
exact failure mode: quarantine placed *inside* the root as `forks-quarantined/`) re-spawns
children the moment anyone next runs the batch. `QUARANTINE.txt` is read by **no code**.
Fix: gate at spawn, not at rest. For each spec found, require
`spec_path == RESULTS_ROOT/<spec.donor_run_id>/forks/<key-dir>/fork-spec.json` (identity bind),
require `audit_run(RESULTS_ROOT/spec.donor_run_id).tier == "clean"`, and hard-skip any spec with
`_quarantine` or a `QUARANTINE.txt` anywhere in its ancestry. ~40 LOC in `build_jobs`.

**A2 (HIGH): stale specs are re-spawned and silently clobber finished children.**
No idempotence check exists: every batch over the same root re-queues every historical spec,
and `run_fork_child.py:130-131` does `shutil.rmtree(base)` on an existing run-id dir. A retry
batch therefore *deletes and resamples* already-counted children — selection by retry is
currently free. fork_stats reads whatever report.json is newest. Fix: skip jobs whose
`RESULTS_ROOT/<run_id>/report.json` exists (opt-in `--force` to rerun), and record spec
`frozen_wall` + snapshot_sha in the report for lineage.

**A3 (MEDIUM): the quarantined gold spec is still fully spawnable by content.**
`results/_quarantine/donor-r4-20260831-132101-goldtier/T1/fork-spec.json` still points at
`results/kvc/pi-...-donor-r4-20260831-132101/snapshot-T1` — which still exists on disk. A root
typo (`--fork-specs results`) or a restored copy spawns instantly; the tainted frozen state was
relocated, not destroyed. Fix: on quarantine, null/rewrite `snapshot_path` + `session_snapshot`
in the spec (or move the snapshot tree alongside it).

**A4 (MEDIUM): donor-base deletion is fail-closed in stats but silent in spawn.**
`donor_tiers` maps missing donor dir → `"missing"` → children quarantined (good). But specs of a
deleted donor stay in the spawn glob; children burn budget and are later excluded (waste, and a
temptation to "clean up" the exclusion). Fix: build_jobs skips specs whose donor dir is absent.

**A5 (MEDIUM): audit_leaks soundness is thin — it is the foundation of stats-quarantine.**
`audit_run` scans only `tool_execution_start` **args** for literal substrings
(`audit_leaks.py:45-75`). Indirect access evades it (`cd ..` chains, globs, env dumps — the
`env` command's *args* carry no marker even though its *result* prints `KVC_RUN_DIR` paths).
Also `own_id in blob → skip` (`audit_leaks.py:68`) is a prefix hazard in the fork world: every
child run_id embeds its donor run_id, so any blob naming a sibling child or the donor's forks/
dir reads as "self" **to the donor** and escapes the `results/kvc` marker. And `"task.json"`
as a bare marker can false-positive on legit repo files, shrinking n. Practical consequence:
a gold-leaning donor can be misclassified clean and its children enter statistics. Fix: also
scan `tool_execution_end` result blobs for GOLD_MARKERS; replace the own_id substring skip with
path-prefix matching against the run's own base dir.

**A6 (MEDIUM): skipped/dead children are invisible to stats.**
The `MIN_CHILD_BUDGET` path (`run_fork_child.py:146-153`) returns 0 **without writing
report.json**, and kill-partial children (e.g. the surviving `donor-r4-...-fork-T1-{none-c2,
sham-c1}` dirs, no report) leave only `run/state/fork.json`. `collect_children` globs
`*/report.json`, so both vanish — violating pre-registered rule 2 ("skipped children are
*listed* but excluded"). Fix: union in `*/run/state/fork.json` rows with no report.

**A7 (LOW): spec-keyed lookups are shape-fragile.** `collect_specs`
(`fork_stats.py:101`) matches `*/forks/*/fork-spec.json` exactly; a donor dir renamed/moved
breaks the (donor,key) trigger lookup → `trigger=None` silently (breaks the fidelity metric
below, not quarantine). Duplicated specs with identical (donor_run_id,key) under two paths
collapse last-wins in stats and produce duplicate same-run-id jobs in spawn (A2's rmtree makes
them race). Parse failures are `continue`d with no log line.

**A8 (LOW): `_validation/` smoke data lives inside the stats/spawn root.** Currently invisible
to `*/report.json` (depth) and has no fork-spec — but any future flat report under
`results/kvc/_validation/` would register as a child. Move parked validation artifacts outside
results/kvc or teach the collectors the `_`-prefix exclusion.

## Part 2 — T2 fidelity: the premise is inverted, and the calibration must be restated

**B0 (factual correction).** The assignment says the T2 specs froze trees that *already passed*
validation. Source says otherwise: **T2 fires on validation FAIL**
(`gps.py:141,149-153`; T3 is the post-pass trigger). Both target specs confirm it:
donor b3-r2-133534's only validation is `fail@epoch6 @135s`, T2@epoch6 fires at the same stamp;
donor b4-r1-134550's only validation is `fail@epoch0 @301s` with T2@epoch0 alongside (and no
mutations after — the tree stayed broken). No T3 spec exists yet anywhere (T3 = 0 across all
donor event logs; children have inert triggers so they can never mint specs). Consequence:
none-arm T2 children are **not** "should pass again" probes; and the `t2_fidelity()` function
already added to `fork_stats.py` (current on-disk version) encodes exactly this wrong premise
("T2 blocks restart from a tree that ALREADY passed... ≤half passing is an alarm") — with fail-
frozen trees it will raise false alarms and, worse, mark the healthy results "suspect". This is
a bug against `gps.py`, not an experiment result.

**What T2 none-children actually can measure (pre-registerable):**

1. **Verifier-fidelity check (deterministic, the strong one).** If a child's first
   `kvc_validation` occurs at child-epoch 0 with empty `git diff` vs `spec.snapshot_sha`, the
   result MUST reproduce the donor's trigger result (`fail` for both T2 specs). A `pass` there
   means tree, validator config, or hidden-test patch diverged in replay → replay breakage:
   exclude the child and audit the clone. `stale` is impossible (fresh run dir) and indicates
   a harness bug. Requires adding to `run_fork_child`: record `git rev-parse HEAD` at start and
   diff-empty-flag at first validation (~10 LOC); today report.json has `first_pass_epoch` but
   no first-validation-reproduction field.
2. **Context-retention (soft).** Cold-restart signatures in the child's first N tool calls:
   first `read` path not in the donor transcript's touched paths, or re-derivation of the
   task description despite it being the transcript's first user message (replay mode children
   have the full donor history — they should never need to re-read from scratch). Flag: >2
   tool calls touching only never-before-touched top-level files before first productive action.
3. **Churn continuity.** Donor epoch cadence in the 60s before trigger vs child cadence after
   resume: b3-r2 ≈ 1 epoch/15s (fast tinker loop), b4-r1 ≈ 1/50s. A none child of b3-r2 showing
   zero mutations for >120s (of its 284s budget) suggests the transcript resumed but momentum
   broke (prompt-effect of RESUME_PROMPT — the documented confound, not a clone bug).
   **Replay breakage vs stochastic variance, decision rule**: breaks are signaled *only* by
   items 1/F-structural (deterministic, per-child); items 2/3 divergences count as stochastic
   unless **both** none children (c1,c2) of the same spec show the same anomaly while their
   T1-arm siblings from the same donor do not. A 1-of-2 anomaly is recorded and ignored.
4. **Timing floor.** Donor revalidated 0 times in its remaining 285s (b3-r2) / 118s (b4-r1),
   so "child fails to revalidate within budget" is donor-consistent, not a break. Never use
   revalidation latency as a breakage signal for these two specs.

**Recommended alarm replacement for `t2_fidelity`:** rename concept to
`validation_reproduction_fidelity(rows, specs, events)`: alarm iff any included replay-mode none
child's first on-epoch-0 unchanged-tree validation != donor trigger-stamp result, OR if no none
child of a T2 spec ever validates unchanged (then the check is vacuous — report "untested", not
"alarm"). And when Round-3b collects **T3** specs (true already-passed trees), the same check
inverts to the team lead's original intent — none children must re-pass on the unchanged tree —
that is the probe this design actually wants; suggest the amendment go into DESIGN-FORK.md
before more children launch.

**Data status:** b3-r2-133534's earlier T2 none-c1 child was killed mid-run by the incident
batch (events stop at ~26s, no report). Batch `round3-children-3` is running right now and
re-spawns both T2 specs' children from the surviving specs under results/kvc — once it lands,
checks 1-3 are computable from stored events with no new runs.

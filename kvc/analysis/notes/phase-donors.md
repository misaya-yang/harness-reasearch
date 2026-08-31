# Phase-transition decomposition — round-3 fork donors (pi-find-root-relativization)

Read-only analysis of donor runs under `results/kvc/pi-find-root-relativization-donor-r{1..4}-20260831-{130609,132101,133534,134550}`.
Sources: `<base>/report.json`, `<base>/run/events/events.jsonl` (`_mono` timings), `<base>/forks/*/fork-spec.json`. Python: /opt/homebrew/opt/python@3.14/bin/python3.14.

## Count discrepancy (flagged)

The brief said 10 donor runs (8 clean after 2 gold). On disk there are **16** donor run dirs (4 batches × r1–r4), all complete (events present, reports present). Gold-tier per brief + round-3.md: **r4-132101** and **r4-134550** — noted, excluded from aggregates (they add: 1 T1-stall and 1 mutate-no-validate shape; r4-134550 also fired T1). Aggregates below use **n=14 clean**. If "10" refers to donors audited when round-3.md was drafted, batches 3–4 grew the set since; worth confirming before quoting n.

All events cross-check against reports (epochs/validation_calls/triggers agree for every donor run — no mismatches on this batch, unlike round-1 kac-r1/r2).

## (a) Per-run shapes (clean donors; T@ = _mono of trigger; val = (epoch, result, t))

| donor | ep | 1st_mut | vals | final | read | edit | write | bash | T1 | T2 | shape |
|---|---|---|---|---|---|---|---|---|---|---|---|
| r1-130609 | 0 | – | – | budget | 2 | 0 | 0 | 36 | 181.1 | – | T1-stall |
| r1-132101 | 0 | – | – | budget | 1 | 0 | 0 | 24 | 273.8 | – | T1-stall |
| r1-133534 | 2 | 112.3 | – | budget | 4 | 4 | 0 | 29 | – | – | mut, no val |
| r1-134550 | 2 | 364.7 | (0,fail,300.7) | budget | 1 | 0 | 0 | 40 | 154.5 | 300.7 | stalled→val@ep0→late mut |
| r2-130609 | 6 | 97.5 | – | budget | 1 | 5 | 0 | 109 | – | – | mut, no val |
| r2-132101 | 5 | 89.6 | – | budget | 4 | 2 | 3 | 75 | – | – | mut, no val |
| r2-133534 | 24 | 28.6 | (6,fail,135.0) | budget | 4 | 5 | 1 | 81 | – | 135.0 | val-fail→churn |
| r2-134550 | 2 | 79.2 | – | budget | 3 | 2 | 0 | 73 | – | – | mut, no val |
| r3-130609 | 38 | 137.1 | – | budget | 5 | 4 | 0 | 80 | – | – | mut, no val (tight loop) |
| r3-132101 | 5 | 146.1 | – | budget | 7 | 3 | 2 | 76 | – | – | mut, no val |
| r3-133534 | 11 | 104.2 | – | budget | 3 | 3 | 0 | 73 | – | – | mut, no val |
| r3-134550 | 12 | 119.4 | – | budget | 2 | 0 | 0 | 75 | – | – | mut (bash-writes), no val |
| r4-130609 | 9 | 117.0 | – | budget | 9 | 8 | 0 | 62 | – | – | mut, no val |
| r4-133534 | 4 | 30.8 | – | budget | 2 | 0 | 0 | 30 | – | – | mut, no val |

Timing distributions (clean): median epochs 5 (range 0–38); median first-mutation among mutators **108 s** (28.6–364.7); T1 fires 154–274 s (3/14 = 21%); epoch cadence splits the cohort: high-epoch runs (r3-130609: 38 ep @ 7.7 s/ep; r2-133534: 24 ep @ 16.7 s/ep) run tight edit→bash loops, while 2–12-epoch runs spend 15–36 bash calls per epoch. Tool mix totals: read 48, edit 36, write 6, bash 963 — bash dominates every run (median 74/run); `validate_current_patch` called in only **2/14 runs (14%)**, 1 call each.

## (b) Run-shape taxonomy

- **T1-stall, 0 mutations:** 2/14 (14%) — r1-130609, r1-132101. Signature: ~24–36 bash + 1–2 reads, no edit/write, T1 at 181/274 s, then still nothing to budget.
- **Mutate-without-validate churn (I→V failure):** 10/14 (71%) — every remaining mutator except r2-133534. All ran to `kvc_terminate budget` with 0 validation calls. This is the dominant shape.
- **Reached validation (T2 fired):** 2/14 (14%) — r2-133534, r1-134550. Both verdicts were **fail** (counterexample `Test Files 1 failed (1)`) — i.e., T2 fired on failing validations, not passing ones (see correction below).
- **Ever passed / V→T:** 0/14. No kvc_validation pass, no kvc_incumbent_saved, no agent_settled anywhere in the donor set. p_T is unobservable on this task from donors.

## (c) The two T2 runs

**Correction to brief:** neither had a *passing* validation — both validations returned `fail`, which is what armed T2 (`T2@epoch6`, `T2@epoch0`). The passing-validation outcome at these checkpoints is what the fork children are being run to measure.

- **r2-133534 (T2@epoch6, fail at 135.0 s):** early starter (first mutation 28.6 s, 6 epochs by 107.3 s). Immediately before validate: a burst of self-made test harnessing via bash — wrote `find-rel.test.ts` twice (`cat > … <<'EOF'`), hand-rolled `/tmp/fdx` directory fixtures, `grep/sed` over `test/tools.test.*`, then three runs under `PATH="/private/tmp/fakebin:$PATH"` (fabricated tool on PATH). At 131.4 s it called `validate_current_patch`, which failed at 135.0 s. Fork-spec freeze left **remaining_budget_seconds = 283.9** (68% of 420) — and the donor itself then spent it churning 18 more epochs (24 total) without ever re-validating.
- **r1-134550 (T2@epoch0, fail at 300.7 s):** 5-minute pure exploration: T1 fired at 154.5 s with 0 mutations; the agent kept grep/sed-archaeologing (`tools-manage`, `import.meta`, `relative` in src/core) and at 297.3 s called `validate_current_patch` **with an empty working tree** (epoch 0 → trivially fail). First mutation finally at 364.7 s. Fork-spec T2@epoch0 freeze left **remaining_budget_seconds = 118.2** (28%) — under a fifth of budget after a validation of nothing. (Its T1 spec had frozen earlier with 264.5 s remaining.)

Shared pre-validation signature: ~11 consecutive read-only bash archaeology calls terminating in a single, "just try the button" validate — validation never enters the edit→test inner loop, and in r1-134550 it isn't even conditioned on having produced a diff.

## (d) Which transition dominates

With clean donors n=14 (was n=4 native in the 115926 batch): D→I failure is 14% and both stalls do fire T1 correctly; but the binding transition is **I→V**: 10 of 12 mutators (83%) churn edits and bash loops without ever calling validate, and the 2 that did validate produced failures — so the task's phase flow collapses one step before V→T, which never occurs (0 passes). Note the earlier native batch r3-115926 passed at epoch 4 and settled, so V→T *can* work when a pass happens; the donor data says a pass is nearly never reached. Design implication for round-3: T2@epoch0 children (r1-134550, 118 s remaining) start from a state with no diff and little budget — the reconstruction/fork estimand at that spec is dominated by whether the card can produce *any* mutation, not by validation quality; T2@epoch6 (284 s remaining) is the spec where I→V steering is actually testable. The gold leak (2/16 donors reaching upstream pi checkout) additionally means donor-side validation failures here understate nothing about actor capability but flag bash as an unsandboxed read channel.

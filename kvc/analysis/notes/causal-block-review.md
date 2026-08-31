# Causal review: Round-3 block structure (5 usable blocks, b4 contributes 2)

Read-only analysis of `analysis/round-3.md`, `DESIGN-FORK.md`, `analysis/fork_stats.py`.
Date: 2026-08-31, before any round3-children-3 outcomes exist. No code executed.

Structure under review — replay stratum: 4 blocks from **3 donors** (b2-T1, b3-T2@e6,
b4-T1, b4-T2@e0); reconstruction stratum: 1 block (b1). Gold quarantine rate 20%.

## 1. Two blocks from one donor: validity and sensitivity

**The stratified permutation test stays exact-valid.** The test conditions on the realized
blocks and permutes arm labels *within* each block; validity needs only exchangeability of
children within a block under the sharp null (same state, same arms, random assignment —
true by design). Cross-block correlation (b4's two blocks share donor trajectory, task
difficulty, and model behavior) does not enter the sharp-null argument, and blocks are
independently randomized at spawn. p is valid.

**What the correlation does break:**
- *Weighting*: b4 gets 2/4 of the block weight; the estimand silently becomes
  donor-weighted, over-representing one trajectory. Fine as descriptive, fragile as inference.
- *CI coverage*: the permutation CI (and any block-difference spread) treats blocks as
  independent units; with 3 effective independent donors, intervals are anti-conservative.
- *HL double-count*: `hodges_lehmann()` takes the median over block diffs — b4's donor effect
  enters twice. Median-of-4 is partly robust, but not demonstrably so here.

**Leave-one-donor-out HL — concrete recipe (fork_stats addition).** Compute at donor level,
within stratum, never pooling replay with reconstruction:
1. Per block: `d_b = mean(kac passes) - mean(none passes)` (already computed).
2. Per donor D: pool all of D's clean children across its blocks → `d_D`
   (for b4: 4 kac vs 4 none children; equal block sizes make this = mean(d_T1, d_T2)).
3. Point estimate: `HL_D = median{d_D}` — replay stratum currently: median of 3 values.
4. LODO: drop each donor i, recompute over remaining donors (`median` of 2 = their mean).
   Report the table `HL_D` and `{HL_(-i)}`; flag if sign(HL_(-i)) ever differs from sign(HL_D)
   or from another i. With 3 donors the table is the honest sensitivity analysis; do not
   bootstrap a median of 3 (degenerate). Retain a leave-one-*block*-out pass too — if
   dropping b4-T1 vs b4-T2 moves the estimate in opposite directions, that is a
   single-block-influence finding worth reporting verbatim.
5. Donor-level bootstrap becomes defensible only at ≥8 donors; until then, full disclosure
   of per-donor diffs > any summary.

## 2. p_min floor and what tonight may claim

Confirmed: **estimation-only tonight** — and it should stay so even though a technical
discrepancy exists: the guard `p_min = 2·2^-B` is the floor of the *block sign-flip* test
(binary per block), not of the child-level stratified enumeration the code actually runs
(C(6,2)=15 configs/block → min two-sided exact p ≈ 2/15⁴ < 0.05). So the frozen code could
print `p < 0.05` while `can_reject_alpha_05` stays False. Decision (pre-data, record as a
DESIGN-FORK amendment, not a silent edit): **the pre-registered 2·2^-B block floor governs
the reject/estimate call**; the child-level enumeration p is reported as a descriptive tail
probability, labeled non-decisional. Justification: with 3 effective donors + 20% taint, a
significant child-level p on 4 blocks would be pseudo-replication bait; a floor that "just
barely" permits rejection is exactly the pattern pre-registration exists to rule out.

**Exact estimates to report tonight (replay stratum):**
- Per-arm pass counts and rates, kac / sham / none (≤8 children each), Wilson 95% CI
  labeled *approximate* (children cluster within donor); plus per-block rates (min–max).
- Table of the 4 block diffs `d_b` (values in {0, ±0.5, ±1}); HL shift (block-level) and
  HL_D (donor-pooled) with LODO range per §1.
- Permutation CI on the statistic, **rescaled to the shift**: divide both endpoints by 2B
  (B = informative blocks; statistic = Σ(±outcome) = 2·Σd_b), giving CI for mean paired diff.
- Observed enumeration p + method string, marked non-decisional; note the floor discrepancy.
- Secondary: identical quantities for sham-vs-none (interruption effect).
- Fidelity calibration (free, do not skip): none-arm children of the two T2 blocks
  (b3-T2@e6, b4-T2@e0) revalidate a tree that already passed once → expect 4/4;
  report count, and treat ≤2/4 as a replay-fidelity alarm that qualifies everything above.
- Reconstruction stratum (1 block): raw 2v2 counts only, no CI theater.
- Language: "observed shift +x, 95% CI [a,b]; effects ≥ b pp incompatible with data";
  never "no effect".

## 3. Worth another donor batch tonight (4 runs → ~1.1 expected clean blocks)?

**No — do not chase n=6.** Numbers: expected new clean blocks ≈ 1.5 × 0.75 ≈ 1.1; P(total
≥6) ≈ 30–40%. And 6 is not a safe landing spot anyway: floor at B=6 is 0.031, so a single
block lost to error-exclusion, kac-nocard collapse, or non-informativeness returns the floor
to 0.0625 and re-locks estimation-only. The binding constraint for tonight's inference was
never the floor — it is 3 effective donors, which +1 block does not fix.

**Decision rule (record it before viewing tonight's outcomes):**
Run the extra batch tonight iff **both** hold:
(i) the wall-clock slot is idle and the batch cannot delay the round-3 write-up, and
(ii) you commit sight-unseen, in writing, to pooling all batches under the frozen rules with
a pre-set target of **≥8 informative blocks from ≥6 distinct donors** (not ≥6 blocks),
no stopping-when-significant.
Otherwise defer to a planned 8–12-donor batch next window — same taint rate, double the
yield, and it takes the floor past 0.05 with margin (B=8 → p_min=0.008) instead of on the
knife-edge. The pre-registration protects you precisely when adding samples looks tempting
after seeing estimates; a written, pre-data, target-sized batch is the clean version of the
same move.

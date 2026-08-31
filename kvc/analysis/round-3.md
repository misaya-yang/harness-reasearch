# Round 3 — trigger-time fork: KAC cards at deterministic checkpoints

Status: COMPLETE (batch round3-children-4, 30/30 children, 0 nonzero exits).
Protocol: `kvc/DESIGN-FORK.md` (pre-registered; amendments logged inline).
Analysis code: `kvc/analysis/fork_stats.py` (frozen before first child,
amended pre-data for the integrity findings below — all amendments predate
any child outcome).

## Design recap

Donors run pure-native on `pi-find-root-relativization` (chosen because T1/T2
actually fire there). At each trigger the harness freezes a fork spec:
workspace snapshot (APFS clonefile + commit + tag), event prefix, GPS,
remaining budget, and — after the replay upgrade — a copy of the live pi
session JSONL. Children (none / sham / kac, 2 each per spec) restart from the
frozen state; arms differ only in the steered card. Primary outcome: any
`kvc_validation result=pass` under the frozen overlay validator.

Two strata, never pooled: **replay** (pi `--fork` carries the exact donor
transcript; estimand = ATE at the exact frozen state) and **reconstruction**
(canonical-state prompt; estimand τ_F).

## Material

(donors batches 1–4, stamps 130609 / 132101 / 133534 / 134550)

| donor | trigger | mode | tier | status |
|---|---|---|---|---|
| b1-r1-130609 | T1 | reconstruction | clean | included |
| b2-r1-132101 | T1 | replay | clean | included |
| b2-r4-132101 | T1 | replay | GOLD | quarantined (results/_quarantine/) |
| b3-r2-133534 | T2@epoch6 | replay | HARNESS | quarantined (see below) |
| b4-r1-134550 | T1 | replay | clean | included |
| b4-r1-134550 | T2@epoch0 | replay | clean | included |
| b4-r4-134550 | T1 | replay | GOLD | quarantined |

Replay blocks = 3 (b2-T1, b4-T1, b4-T2@epoch0); reconstruction blocks = 1.
Only 2 replay blocks are informative for kac-vs-none (b4-T1 lost both none
children to gold-quarantine, leaving it arm-incomplete). Exact-p floor =
2·2⁻² = 0.5 ⇒ **estimation only**, per pre-registration.

**b3 quarantine (strengthened audit, pre-data).** The 2026-08-31 audit
hardening (result-blob scanning, component-boundary self-attribution)
revealed donor r2-133534 read its own validator directory
(`cd` into `run/validator` + `cat task.json` / `cat kvc-validator.json`) —
harness-tier contact with the unsanitized config, so all six b3 children
are quarantined by the pre-registered donor-tier rule. This removed the
only T2 spec where I→V steering was testable (284 s remaining); the
surviving T2@epoch0 spec froze a diff-less tree with 118 s left.

Gold-tier donor rate: 2/16 donors (12.5%) walked behaviorally to the
upstream pi checkout despite sanitized validator configs; a further 2
donors plus r2-133534 are harness-tier. The filesystem remains an open
leak channel (actor bash is unsandboxed); audit + quarantine is the
control. Additionally the harness's agent-dir/bin symlinks into the pi
checkout's node_modules/.bin, so every PATH/env dump passively discloses
the gold-checkout location — 3 none-arm children demonstrably followed
that disclosure to the gold tree (behavioral gold, quarantined).

## Replay mechanism validation

Smoke child (parked in results/kvc/_validation/): forked session carried the
donor transcript byte-identical (only session id / cwd / parentSession /
timestamp rewritten), RESUME_PROMPT accepted, ran to budget exhaustion.
All spec snapshots ended at toolResult boundaries (no mid-turn tails so far).

## Incident

First quarantine sat inside the rglob root; one child was briefly spawned
from the gold-tier spec before detection. Killed, spec moved outside
results/kvc, partial child deleted (no report). Defense in depth: fork_stats
quarantines by donor audit tier regardless of spec location.

## Results (fork_stats, 2026-08-31; JSON: analysis/round-3-fork-results.json)

30 children spawned; 20 included (15 replay, 5 reconstruction), 1
kac-nocard (listed separately), 0 excluded-with-report, 9 quarantined
(6 by b3 donor harness tier; 3 none-arm children by their OWN behavioral
gold contact). 12 of the 20 included children carry a harness flag
(mostly passive node module-resolution errors printing kvc/.cache paths);
they remain included with a sensitivity listing in the JSON.

**Primary outcome: zero passes in every arm of every block.**

Replay (primary):

| arm | n | passes | rate | Wilson 95% | validated | val calls | 1st val t̃ (s) |
|---|---|---|---|---|---|---|---|
| kac | 6 | 0 | 0.00 | [0.00, 0.39] | 1 | 2 | 17 |
| sham | 6 | 0 | 0.00 | [0.00, 0.39] | 1 | 1 | 13 |
| none | 3 | 0 | 0.00 | [0.00, 0.56] | 1 | 1 | 110 |

kac-vs-none: statistic 0; enumeration p = 1.0 (non-decisional; floor 0.5);
block diffs [0.0, 0.0]; HL (block) +0.00; HL donor-pooled +0.00 (donors
132101: 0.0, 134550: 0.0); LODO sign-stable. Mean paired difference 95%
CI (rescaled): [0.0, 0.0].

Reconstruction (secondary, 1 block): kac 0/1, sham 0/2, none 0/2 — raw
counts only, per pre-registration.

kac-nocard: b1 reconstruction kac-c1 (228-char probe output, no card;
ran unsteered, no pass).

Process descriptives (included children): mutation epochs per child —
kac ≈ 3.0 (n=7), none ≈ 3.6 (n=5), sham ≈ 4.1 (n=8); validation calls
total — kac 2, none 1, sham 1. All arms mutated at similar rates; almost
nobody validated; nobody passed. The I→V collapse observed in donors
(10/14 mutate-without-validate) reproduces inside the forked children.

**K-layer delivery worked**: after the template fix, 9/10 kac card probes
settled and parsed (1 no-card = the reconstruction child above); all 9
cards were steered and accepted (steer_accepted=true). 6/9 cards advised
next_action=probe, 3/9 mutate. Cards named the correct edit surface
(find.ts relativization sites) — delivery, parsing, and acceptance are
not the bottleneck.

T2 reproduction fidelity: UNTESTED — no none child of the surviving T2
spec validated before mutating, so the deterministic replay check is
vacuous this round (reported as untested, not alarm, per the amended
protocol).

## Interpretation (pre-registered rules applied)

1. **Bounds language, not "no effect"**: passes were 0 in all arms; the
   data are compatible with a true kac advantage up to the Wilson upper
   bound (~39 pp at n=6). No evidence of benefit; no evidence of harm.
   Rejection is impossible at this n by construction (floor 0.5).
2. **Sham vs none**: also 0 vs 0 — no detectable interruption effect of
   the resume prompt + sham card on this task at this n.
3. **Where the chain breaks**: D (card content) and steering delivery
   work; the failure is downstream — children mutate but almost never
   validate (4 validation calls across 20 children) and never convert to
   pass within the remaining budget (118–284 s). On this task the binding
   transitions are I→V and V-capacity, not D and not card acceptance.
   This converges with the donor phase decomposition (notes/phase-donors.md:
   10/14 mutators never validate; 0/14 ever pass).
4. **Leak findings are substantive, not cosmetic**: 3/6 included none-arm
   children (and 3 more quarantined ones) navigated to the gold checkout;
   all reached it via the PATH/env disclosure of agent-dir/bin → the
   passive disclosure is behaviorally actionable. Future donors/children
   need agent-dir/bin scrubbed of gold-checkout paths (logged as a
   harness fix for the next donor batch).

## Integrity amendments applied pre-data (this round)

- fork_stats t2_fidelity premise corrected (T2 fires on FAIL, not pass)
  and replaced by validation_reproduction_fidelity (notes/fork-integrity-review.md B0).
- audit_leaks hardened: result-blob scanning, component-boundary
  self-attribution (fork-child ids embed donor ids), passive-gold
  downgrade (PATH printouts earn harness tier, behavioral args earn gold).
- fork_stats gates children on their own audit tier (gold excludes;
  harness flagged + listed for sensitivity).
- run_batch spawn gate: identity bind, QUARANTINE.txt/_quarantine
  exclusion, donor tier check at spawn, idempotence skip of finished
  children (--force-fork to override).
- Quarantined specs' snapshot paths nulled in place.

## Interpretation rules (pre-registered, restated)

- Null ⇒ bounds language ("effects ≥ b pp ruled out"), not "no effect".
- Sham movement vs none ⇒ interruption effect, reported separately.
- T2-spec children: none-arm first validation on the UNCHANGED tree must
  reproduce the donor's trigger-time FAIL (T2 fires on fail); a pass
  there is a replay breach. T3 specs (already-passed trees) would invert
  the check; none were collected this round.

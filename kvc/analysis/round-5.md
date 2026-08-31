# Round 5 — mutation-initiation rescue: knowledge vs initiation

Status: COMPLETE (2026-08-31). Batch `r5-rescue-2` (stamp 20260831-150304,
8 jobs, 0 nonzero exits). Protocol: DESIGN-R5.md (frozen pre-data; baseline
corrected per hardened audit). First attempt `r5-rescue` died wholesale at
workspace preparation (dependency-cache incident — see
notes/dependency-cache-incident.md); rescue batch ran against the rebuilt
read-only cache.

## Scoring per frozen rules

Clean native baseline: thinking-toggle **0/2** (r2, r3 @115926),
post-tool-compaction **0/2** (r2, r3 @115926; r1 harness-tier, reported
separately, never pooled).

| task | arm | run | audit tier | epochs | val calls | pass |
|---|---|---|---|---|---|---|
| thinking-toggle | kac | r1 | clean | 0 | 0 | no (probe produced NO card: settled prose, card_parsed=false) |
| thinking-toggle | kac | r2 | **gold — excluded with report** | 2 | 0 | no |
| compaction | kac | r1 | clean | 0 | 0 | no |
| compaction | kac | r2 | clean | 0 | 0 | no |
| thinking-toggle | hint | r1 | clean | 0 | 0 | no |
| thinking-toggle | hint | r2 | harness — reported separately | 6 | 1 (FAIL at epoch 0) | no |
| compaction | hint | r1 | clean | 0 | 0 | no |
| compaction | hint | r2 | harness — reported separately | 2 | 0 | no |

**Zero passes in every arm under every inclusion variant. Neither arm
rescues ⇒ by the pre-registered interpretation rule, the bottleneck is
initiation/execution beyond surface knowledge.** (Rule: "rescue = ≥1/2
passes in an arm while the clean native baseline for that task is 0;
neither rescues ⇒ initiation/execution bottleneck beyond surface
knowledge.")

## Process descriptives (bounds language only, per frozen analysis plan)

- **kac arm**: T1 fired ~148s in all 4; probes settled fast (4.9–6.5s) but
  only 3/4 produced parseable cards (tt-r1 emitted narrative prose — the
  same answerability failure class as R4). All 3 cards were ACCEPTED, all 3
  had `next_action: probe`, and all named sensible surfaces (compaction:
  agent-loop/compaction gate; tt: interactive-mode thinking-toggle render
  path). Post-card behavior: compaction children did 0 edits (230+ s more
  read-only archaeology); tt-r2 edited once @211s — but that run is the
  gold-excluded one. Clean-arm summary: accepted cards at T1 do not buy
  mutations on these tasks.
- **hint arm** (oracle cards naming the gold surface): first mutations are
  LATE — @349s, @321s, @271s (card lands ~153s; 120–200s latency to first
  edit), leaving <150s of budget; tt-r1 never mutated at all despite the
  oracle naming the exact surface. Mean epochs hint 3.0 vs kac 0.5 — the
  oracle lifts mutation activity but converts to nothing within budget.
- **The single validation call of the whole batch** (tt-hint-r2 @159s): the
  actor validated the UNMUTATED tree 6s after receiving the oracle card
  (result fail, "Test Files 1 failed" — the red-at-base signal the
  oracle-gap note predicted only the overlay validator can produce), then
  mutated 6 epochs and NEVER re-validated. I→V collapse in both directions:
  validate before acting, then act without validating. The red signal did
  not pull validation into the edit loop.

## Leak findings (this batch)

1. **tt-kac-r2 gold contact at t=7s — the run's FIRST tool call**, parallel
   with `pwd`, i.e. before any tool result: the gold-checkout path was in
   the model's initial context. Root cause identified: pi's own system
   prompt embeds `getReadmePath()/getDocsPath()/getExamplesPath()` =
   absolute paths under the pi checkout (config.ts `getPackageDir()` →
   `findNodePackageDir(__dirname)`; the actor runs via tsx from the pi
   source tree). **Every actor run in this program received the gold
   location in its system prompt** (plus PATH containing
   `pi/node_modules/.bin`, kvc_run.py:175). The audit tier is what protects
   the statistics — it demonstrably did here. Fixes for any future batch
   (both pre-data if applied before launching): set `PI_PACKAGE_DIR` to the
   workspace's packages/coding-agent (pi supports the env override; docs
   then resolve inside the actor's own base-clone), and drop
   `config.pi_repo/node_modules/.bin` from the actor PATH.
2. comp-hint-r2 (harness tier): wandered into a sibling Round-2 run dir and
   read its report.json/workspace log — included-but-flagged per frozen
   rule; outcome-neutral (0 passes everywhere).
3. tt-hint-r2 (harness tier): single hit is its OWN workspace path broken
   by a literal line-wrap ("…bash-output ⏎ -hint-r2…"), which string
   self-attribution cannot verify; conservative classification kept.
4. audit_leaks soundness fix applied PRE-scoring of this batch: self-
   attribution now expands to the full path extent around a marker
   (previous rfind window missed own paths where the run id follows the
   marker; direction conservative — false harness flags only, never a
   missed gold).

## Interpretation

Converges with R3: knowing WHERE to edit — even from the oracle itself — is
not sufficient for a verified pass within 420s at this model tier. The
hint arm is the initiation CEILING probe and it produced zero passes: the
binding constraints sit in mutation initiation latency (120–200s even with
the oracle), validation initiation (1 call across 8 runs, made pre-mutation),
and execution capacity for the actual fix under residual budget. The
KAC machinery question (do probe cards help?) is bounded above by this: even
oracle cards do not rescue, so probe-card rescue on these tasks is not
expected regardless of card quality.

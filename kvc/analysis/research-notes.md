# Research subagent findings (2026-08-31 evening, 5 parallel agents)

Synthesis of five read-only research agents spawned alongside Round 3 build.
Raw transcripts: session subagents dir (agent-a{name}-*.jsonl).

## 1. Literature scan — Agent Hysteresis / path invariance (lit-hysteresis)

Known items are real arXiv papers: "Why Retrying Fails" = arXiv:2605.08563;
"From History to State" = arXiv:2605.05413; PATH-Bench = arXiv:2608.01149
(Path-Dependent Evaluation of Lifelong Agents).

Additional prior art:
- Context interference: ACL 2026 aclanthology 2026.acl-long.160 (search-agent
  interference, prunes rather than proves state equivalence); arXiv:2601.07226
  (distractor history distorts reasoning); EMNLP 2024 2024.emnlp-main.811
  (task-switch distance).
- State abstraction: arXiv:2608.00808 (history→execution state for coding
  agents — closest architecture paper); arXiv:2608.19652 (structured state
  beats full transcript); arXiv:2507.00081; arXiv:2311.17406 (LLM-State);
  arXiv:2608.00303 (CrystalMem — proves a "hysteresis" result for memory
  deletion policies; terminology collision to handle).
- Markov-violation / history-as-policy: PRIME (openreview 5aHmaMFJns);
  arXiv:2502.20380 (argues optimal action history-independent at correct
  intermediate states — nearest theoretical motivation); BiPACE
  arXiv:2606.25556 (bisimulation ≈ equivalent state, for credit assignment);
  IEEE INFOCOM 2026 "Rollback Is Not Undo" (arbitrator-state hysteresis in LLM
  control loops); arXiv:2510.06903.
- Consistency metrics: arXiv:2602.11619 (agents disagree with themselves,
  divergence persists at temperature 0); arXiv:2605.28840 (~10-run panels;
  inconsistency predicts failure); SAND EMNLP 2025 2025.emnlp-main.152.

**Verdict**: a Hysteresis Index over *equivalent-state history pairs* is
unclaimed; the space is crowded but nobody conditions on verified-identical
sufficient state. Objections + rebuttals: (1) "inputs differ trivially" →
paraphrase control baseline; (2) "temperature noise" → temp 0 + report
within-history flip floor first; (3) "already measured" → head-to-head table
(prior work varies state OR sampling, never pairs histories at fixed state).

## 2. Causal design for trigger-time forks (causal-design)

- Estimator: stratified paired difference-in-means with the trigger (snapshot)
  as blocking unit; **stratified permutation test** (permute arm labels within
  blocks; Edgington). Exact finite-sample; bootstrap/asymptotics indefensible
  at n≤15. Hard floor: two-sided exact p ≥ 2·2^(−n_triggers) → **n<6 triggers
  can never reject α=0.05** → below that, estimation only (Hodges–Lehmann +
  exact CI).
- Reconstruction vs replay: randomization at fork keeps arms exchangeable
  either way; reconstruction changes the estimand (name it τ_F). Replay
  (implemented after fork-design's discovery) recovers the native estimand.
  Fidelity calibration: donor continuation vs none-arm children bounds it.
- Sample size for 25pp lift at 80% power: n≈20–25 triggers (concentrated
  discordance) to ~55 (independent-ish). Our 5–15 = pilot: report HL shift +
  exact CI, no confirmatory tests.
- Pre-register (DONE in DESIGN-FORK.md): primary outcome binary
  passes-frozen-verifier; handling rules for crashes; everything else
  exploratory.
- Null reporting: bounds framing ("effects ≥ b pp ruled out"); check whether
  sham also moved outcomes (interruption effect). No retrospective power.

## 3. Phase-transition data analysis (phase-analysis)

31 runs, 4 tasks, budget 420s. Highlights:
- **retry-attempt-timeout (main)**: clean-batch n=12: 92% mutate by ~55s
  median, 75% validate, 50% first-pass. Bottleneck is **I→V** (mutate heavily
  without validating; 3/14 mutators never validated), NOT D→I.
- **p_T (V→T)**: 7/7 first-pass runs settled; median pass→end 21s (6–251).
  A pass is always converted to settle. (delivered stays false everywhere —
  different notion.)
- **find-root**: mixed D→I (50% never mutate; T1 at 164–320s in stalls) +
  late first validation (271s in the one pass).
- **compaction-order / thinking-toggle**: severe **D→I failure** — T1 fires in
  a tight 148–168s band in 5/6 zero-mutation runs; budget burned on read+bash
  exploration (73–97 bash calls, 0 edit/write); validation near-absent.
- Mutation side is bash-dominated (~43 bash/run); edit/write:read ≈ 1:1 — the
  cost is bash churn, not reading per se.
- Data oddities documented: 093752-r2/r3 prompt-error harness failures;
  105312 interrupted (no reports); two reports over-count validation_calls
  by 1 vs events.

## 4. Task difficulty characterization (task-difficulty)

Gold diffs verified in the pi clone:
- retry-attempt-timeout (df018b60): narrow surface, prompt names the concept —
  highly discoverable (~60% observed).
- find-root (523b5a49): tiny file, greppable site, but 4 conjoined edge
  cases; manifests only at roots (~33% observed — mid-band anchor).
- thinking-toggle (b07e17fa): edit sites greppable but the base file is a
  6,500-line monster and the fix needs TWO distant call sites; the named
  regression test file does not exist at base (oracle gap!) — 0%.
- post-tool-compaction (56700d42): gold is a cross-package REFACTOR (5 files,
  2 packages); any single-file first mutation is wrong → no attractive first
  move — 0%.
- Predicted band ranking; recommended 20–60% intervention tasks: find-root
  (observed), retry-attempt-timeout (ceiling anchor),
  **reject-truncated-compaction-summary** (predicted mid-band: localized,
  clear concept, multi-test signal) — candidate for future rounds.
- S-probe distractor recipes for find-root: (1) partial application (one of
  two sites); (2) unconditional path.relative without isAbsolute guard;
  (3) drop trailing-separator preservation / POSIX-only root handling.
- Caveat to verify: three v3 tasks' regression-test files don't exist at
  base; whether the actor ever sees an oracle may partly drive the 0/3s.

## 5. Fork implementation review (fork-design) — KEY DISCOVERY

**pi supports native transcript forking**: `--fork <path>` (args.ts:127/289)
→ `forkSessionOrExit` (main.ts:342) → `SessionManager.forkFrom`
(session-manager.ts:1581): copies ALL transcript entries into a new session
(new id, parentSession header). rpc-entry.ts routes through the same main(),
so `--fork` works in RPC mode. Constraint: incompatible with `--no-session`
(main.ts:294-306) → donors must persist sessions
(`$PI_CODING_AGENT_DIR/sessions/<encoded-cwd>/*.jsonl` = run_dir/agent-dir —
leak-contained). Fork fails on empty source; live file is append-only → copy
truncated at last newline. Implemented (commit cb4a5b4): replay mode primary,
reconstruction fallback.

Other recommendations adopted/adjudicated:
- Sham card: static, byte-stable, format-identical filler (adopted: SHAM_CARD).
- Arm delivery: children get one identical resume prompt; card via steer
  afterwards (adopted: RESUME_PROMPT + STEER_DELAY 25s).
- Donor fate: fork-design recommends terminating donors; we KEEP them running
  (donors are injection-free pure-native continuations; their continuation vs
  none-arm children is itself a measurement). Documented deviation.
- Risks logged: mid-turn fork may refuse (exclude-with-report), path confound
  (donor transcript embeds donor paths; children live elsewhere — measure,
  don't fix now), budget accounting (children get full remaining budget).

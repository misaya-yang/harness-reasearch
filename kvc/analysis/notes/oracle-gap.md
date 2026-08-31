# Oracle-gap verification for the two R5 tasks

Read-only verification 2026-08-31 against pi clone (`~/misaya_project/Agent_projects/pi`)
and v3 task rows (`experiments/pi_trajectory/tasks/pi_coding_tasks_v3.jsonl`, never modified).

## 1. File/name presence at base (git cat-file -e / git show | grep)

**pi-thinking-toggle-preserves-bash-output** (base `b07e17fa^`):
- `test/suite/regressions/8611-thinking-toggle-pending-bash-output.test.ts` — **MISSING at base** (created new by gold; `new file mode` in gold diff).

**pi-post-tool-compaction-order** (base `56700d42^`):
- `test/suite/agent-session-compaction.test.ts` — EXISTS (23 tests at base)
- `test/interactive-mode-compaction.test.ts` — EXISTS
- `test/suite/regressions/7253-manual-compact-during-response.test.ts` — EXISTS (1 test at base: "runs only the requested manual compaction when the previous turn crossed the threshold")
- BUT none of the 5 `-t` test names in `test_commands` exist at base (all are gold additions).

Correction to DESIGN-R5.md: the "absent at base for both tasks" claim is exact only for
thinking-toggle. For compaction the files exist and run; the **test names** are dangling.

## 2. What the actor can execute at base

Overlay mechanics confirmed (`kvc/harness/kvc_run.py:308`, `validate_overlay.py:56-75`,
`experiments/pi_trajectory/pi_tasks.py:439`): `hidden-tests.patch` =
`git diff <gold>^ <gold> -- <hidden_test_files>`, applied only inside the ephemeral validator
overlay; the actor workspace is built from the base-tree mirror, so hidden tests are absent
during the run. Sanitized `task.json` retains `prompt` + `test_commands` (fields scrubbed are
only `gold_commit`, `hidden_test_files`, real `source_repo`), so the actor *does* see the dangling commands.

No `passWithNoTests` in vitest configs (checked `packages/coding-agent/vitest.config.ts` and
base-wide grep). Consequences of the actor running their task's `test_commands` verbatim at base:

- thinking-toggle: `vitest --run .../8611-....test.ts` → "No test files found", exit 1.
- compaction: each `-t 'new name'` → "No test matches the given testcase filter", exit 1;
  running the files unfiltered → all base tests PASS (base tests do not encode the desired ordering;
  gold even edits agent-loop.test.ts by only +3 lines).

So in both tasks, **no stock command produces a failing-behavior oracle**: either not-found
errors (misleading: could read as broken environment) or green-but-irrelevant passes.

## 3. Actor-constructible self-checks before `validate_current_patch`

- **thinking-toggle — hard.** Gold test drives the fix via `Reflect.get(InteractiveMode.prototype,
  "updateThinkingBlockVisibility")` — a private method that *only exists post-fix*. A base-time
  repro must instead call `toggleThinkingBlockVisibility` on a synthetic `this`
  (`chatContainer: Container`, `ui: {requestRender}`, fake settingsManager) with a real
  `ToolExecutionComponent` child holding partial output, then assert the child survives and
  `render()` still contains the partial output. Primitives are exported
  (Container/ToolExecutionComponent/initTheme/stripAnsi), so feasible — but requires inventing
  the prototype-call harness pattern and picking the right observation surface (chatContainer
  children identity) unprompted. No base test touches this surface (only fixtures/5943 incidentally
  mention hideThinkingBlock). For flash tier this is near-gold-difficulty authoring.
- **compaction — possible.** Base `test/suite/harness.ts` (`createHarness`, faux provider) and
  `packages/agent/test/agent-loop.test.ts` exist at base; the actor could copy existing
  compaction-test idioms to assert "after a threshold-crossing tool result, no provider request
  precedes compaction" at loop level. Constructible, but requires subsystem knowledge the prompt
  doesn't name and ~30–60 lines of test authoring per attempt.

Either way `validate_current_patch` is the *only* cheap oracle: it is the sole path that applies
the hidden tests and returns pass/fail mid-run.

## 4. Interpretation consequences for R5 (pre-registration implications)

1. **Native 0/3 is confounded by the oracle gap, not purely by knowledge.** The T1 mutation-initiation
   trigger (35% budget, zero mutations) is exactly what a no-failing-signal, 6.5k-line-file,
   dangling-test-command environment induces: there is no red test to "make green," so editing has
   no visible success criterion until the actor dares `validate_current_patch`.
2. **hint-rescue is not a clean knowledge-only treatment.** An oracle card naming the edit surface
   supplies (a) localization, (b) implicitly a checkable observation surface, and — most importantly —
   a reason to attempt edit→validate. A rescued run may differ from native mostly in having *reached*
   the validator at least once. The frozen analysis already logs first-mutation/validation times; report
   `n_validate_calls` and first-validate time per arm so "knowledge" vs "verification-onset" can be
   separated descriptively (bounds language, small n).
3. **kac-probe is the arm most weakened by the gap**: probe generates from state only, and the state
   contains failing commands with "no test found" semantics, likely yielding cards that hedge rather
   than name an edit site. kac-not-rescuing is therefore *not* evidence that the run's state lacked
   the information — check probe-card text for named files before attributing to knowledge.
4. **Asymmetric gap across the two tasks**: compaction has green-but-stale base suites (regression
   detection only; actor's own repro is feasible) while thinking-toggle has a dangling filename and a
   gold-test that references a post-fix private symbol (worst case: even a *correct* actor-authored
   test differs structurally from the hidden oracle — though the observable behavior — child survives,
   output retained — is what matters, and the hidden test checks exactly that via toggle, so a
   behaviorally correct fix passes). Predict thinking-toggle hint-rescue to be needed more, and
   both tasks' failure modes to include "never validated."
5. **Hygiene follow-up for ORACLE_CARDS**: for thinking-toggle never surface the 8611 filename
   (design already says so); for compaction never surface the `-t` *names* (design currently says
   "never regression-test file names" — for compaction the filenames are non-dangling and could
   leak partial intent, e.g. `7253-manual-compact-during-response`; keep scrubbing names AND add
   scrubbing of `-t` patterns from any card text).

## 5. Verdict

Oracle gap **confirmed and refined**:
- thinking-toggle: full gap (test file absent; no runnable failing signal at base; actor self-check
  requires gold-adjacent harness invention).
- post-tool-compaction: partial gap (files present but 5/5 gold assertions absent; base runs are
  green-but-blind; actor self-check feasible via existing harness idioms).
Both R5 arms' results must be read with validation-onset as a mediating variable; DESIGN-R5.md's
"absent at base for both tasks" phrasing should be corrected at next protocol revision (R5 is
frozen — note here suffices).

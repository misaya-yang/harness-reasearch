# Offline behavioral regrade for CTR Rounds 4–5

This scorer independently regrades the twelve saved source patches from CTR Rounds 4 and 5.
It does not read or reuse the original hidden-test verdict. For each row it:

1. archives the task's frozen base commit into a new temporary workspace;
2. links the same neutral, base-pinned dependency store used by the clean evaluator;
3. applies the frozen `evaluation/agent.patch` after verifying its SHA-256;
4. copies behavior-oriented tests under `packages/coding-agent/test/behavioral-regrade/`;
5. runs one focused Vitest command in the no-network evaluator sandbox with one worker; and
6. writes a new receipt without changing any existing task, runner, workspace, or result.

The tests intentionally avoid the three gold-shape constraints found in the original evaluator:

- thinking visibility checks live component identity and partial output, not a private helper name;
- retry tests observe abort/retry behavior and support either `AbortSignal.timeout` or an
  `AbortController` timer implementation;
- compaction tests require rejection/non-persistence but do not require exact error prose.

Before scoring patches, the script requires every independent test set to fail on the frozen base
and pass on the frozen gold commit. A failed calibration writes receipts and stops without scoring
any row.

## Commands

Input integrity only; this does not run Vitest:

```bash
python3.11 experiments/pi_trajectory/behavioral_regrade/regrade.py --check-inputs
```

After other live experiments have finished, run the complete regrade serially:

```bash
python3.11 experiments/pi_trajectory/behavioral_regrade/regrade.py \
  --output results/20260830_pi_ctr_behavioral_regrade_v1
```

The output directory must not already exist. The scorer runs calibration first, then all twelve
rows sequentially. It never launches Pi or a provider, and the execution environment fixes
`VITEST_MAX_WORKERS=1`.

## Pre-outcome-frozen Round 6 find-root secondary

The find-root secondary evaluator was frozen at `2026-08-31T00:47:45Z`, before either Round 6
find-root result was inspected. Its manifest is `r6_find_root_manifest.json`. Both tests call only
the public `createFindToolDefinition`; the freeze checker rejects any test containing the gold
commit's new `relativizeFindResultPath` helper name.

Verify the freeze without reading or running a Round 6 patch:

```bash
python3.11 experiments/pi_trajectory/behavioral_regrade/regrade_r6_find_root.py --check-freeze
```

After both saved patches are available and no live experiment is running, score exactly two rows:

```bash
python3.11 experiments/pi_trajectory/behavioral_regrade/regrade_r6_find_root.py \
  --row N=/absolute/path/to/native/agent.patch \
  --row CTR=/absolute/path/to/ctr/agent.patch \
  --output results/20260830_pi_ctr_round6_find_root_behavioral_v1
```

The script independently requires base-fail/gold-pass calibration before it scores either row.
It accepts only source-scope patches and records each input patch SHA in the new output.

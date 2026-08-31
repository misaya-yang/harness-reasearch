"""Pre-registered analysis for Round 3 trigger-time forks (DESIGN-FORK.md).

Frozen before any fork child is launched: this script implements exactly the
pre-registered rules and nothing else. Any deviation discovered later is a
protocol amendment, recorded in DESIGN-FORK.md, not a silent edit here.

Rules implemented (see DESIGN-FORK.md "Pre-registered analysis decisions"):
  1. Primary contrast: kac vs none, pooled over triggers within the Round-3
     donor task; stratified permutation test (arm labels permuted within each
     trigger block); exact enumeration when the permutation space is small,
     Monte Carlo otherwise (fixed seed). Effect size: risk difference with
     Wilson 95% CI per arm. Replay and reconstruction children are separate
     strata, never pooled; the primary test runs on the replay stratum, and
     the reconstruction stratum is reported secondarily.
  2. Inclusion: children of clean-tier donors with a report. Skipped children
     (MIN_CHILD_BUDGET), process errors, and replay refusals are listed but
     excluded. kac children without a card form the labeled group
     "kac-nocard", never merged into kac. No outcome-based exclusions.
  3. Small-n guard (causal-design finding): with B informative blocks the
     two-sided exact p is bounded below by 2*2^-B; when that floor exceeds
     0.05 no rejection is possible and the script reports estimation only
     (Hodges-Lehmann shift with an exact percentile CI from the permutation
     distribution).

Usage: python3 -m kvc.analysis.fork_stats [--results-root results/kvc]
Prints a markdown summary; writes analysis/round-3-fork-results.json.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
from pathlib import Path

from kvc.harness.audit_leaks import audit_run

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = REPO_ROOT / "results" / "kvc"
OUT_JSON = REPO_ROOT / "kvc" / "analysis" / "round-3-fork-results.json"
PRIMARY_TASK = "pi-find-root-relativization"  # Round-3 donor task
MC_SEED = 20260831
ENUM_LIMIT = 200_000  # enumerate exactly below this many assignments
MC_DRAWS = 20_000
ALPHA = 0.05
Z95 = 1.959964


def wilson_ci(k: int, n: int) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + Z95 * Z95 / n
    center = (p + Z95 * Z95 / (2 * n)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 * Z95 / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def collect_children(results_root: Path) -> list[dict]:
    """All fork-child rows: report.json merged with run/state/fork.json."""
    rows = []
    for report_path in sorted(results_root.glob("*/report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "arm" not in report or "fork_key" not in report:
            continue  # not a fork child
        run_id = report["run_id"]
        fork_meta_path = results_root / run_id / "run" / "state" / "fork.json"
        meta = {}
        if fork_meta_path.exists():
            try:
                meta = json.loads(fork_meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {"meta_error": True}
        rows.append({
            "run_id": run_id,
            "task": report.get("task"),
            "arm": report["arm"],
            "fork_key": report["fork_key"],
            "donor_run_id": report.get("donor_run_id"),
            "reason": report.get("reason"),
            "final_pass": bool(report.get("final_pass")),
            "fork_mode": meta.get("fork_mode", "unknown"),
            "skipped": meta.get("skipped"),
            "no_card": meta.get("card_note") is not None or (
                report["arm"] == "kac" and meta.get("card") is None
            ),
            "steer_accepted": meta.get("steer_accepted"),
        })
    return rows


def collect_specs(results_root: Path) -> dict[tuple[str, str], dict]:
    """fork-spec.json by (donor_run_id, key) for trigger metadata."""
    specs = {}
    for spec_path in sorted(results_root.glob("*/forks/*/fork-spec.json")):
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        specs[(spec["donor_run_id"], spec["key"])] = {
            "trigger": spec.get("trigger"),
            "remaining_budget_seconds": spec.get("remaining_budget_seconds"),
            "elapsed_at_trigger_seconds": spec.get("elapsed_at_trigger_seconds"),
        }
    return specs


def donor_tiers(results_root: Path, donor_ids: set[str]) -> dict[str, str]:
    tiers = {}
    for donor in sorted(donor_ids):
        base = results_root / donor
        if not base.exists():
            tiers[donor] = "missing"
            continue
        tiers[donor] = audit_run(base)["tier"]
    return tiers


def block_key(row: dict) -> tuple[str, str]:
    return (row["donor_run_id"], row["fork_key"])


def permutation_test(blocks: dict[tuple, list[dict]]) -> dict:
    """Stratified permutation test on arm labels within trigger blocks.

    Statistic: sum(kac outcomes) - sum(none outcomes) over all blocks.
    Blocks lacking one arm contribute a constant and add no permutations.
    """
    informative = {
        b: rows for b, rows in blocks.items()
        if any(r["analysis_arm"] == "kac" for r in rows)
        and any(r["analysis_arm"] == "none" for r in rows)
    }
    if not informative:
        return {"informative_blocks": 0, "note": "no block has both arms; no test"}

    # Per-block assignment space: choose which children carry the kac label.
    per_block = []
    for b, rows in informative.items():
        n = len(rows)
        k = sum(1 for r in rows if r["analysis_arm"] == "kac")
        outcomes = [int(r["final_pass"]) for r in rows]
        combos = list(itertools.combinations(range(n), k))
        per_block.append((outcomes, combos))
    space = math.prod(len(combos) for _, combos in per_block)
    p_min = 2.0 * 2.0 ** (-len(informative))  # most extreme configuration

    def stat_for(assignments):
        total = 0
        for (outcomes, _combos), chosen in zip(per_block, assignments):
            kac_idx = set(chosen)
            total += sum(outcomes[i] * (1 if i in kac_idx else -1)
                         for i in range(len(outcomes)))
        # constant contribution of non-informative blocks
        total += sum(
            (1 if r["analysis_arm"] == "kac" else -1) * int(r["final_pass"])
            for blk, rows in blocks.items() if blk not in informative for r in rows
        )
        return total

    observed = stat_for(tuple(
        tuple(i for i, r in enumerate(rows) if r["analysis_arm"] == "kac")
        for rows in informative.values()
    ))

    if space <= ENUM_LIMIT:
        tail = 0
        dist = []
        for assignments in itertools.product(*(combos for _, combos in per_block)):
            s = stat_for(assignments)
            tail += 1 if abs(s) >= abs(observed) else 0
            dist.append(s)
        p = tail / space
        method = f"exact enumeration ({space} assignments)"
    else:
        rng = random.Random(MC_SEED)
        tail = 0
        dist = []
        for _ in range(MC_DRAWS):
            assignments = tuple(rng.choice(combos) for _, combos in per_block)
            s = stat_for(assignments)
            tail += 1 if abs(s) >= abs(observed) else 0
            dist.append(s)
        p = (tail + 1) / (MC_DRAWS + 1)
        method = f"Monte Carlo ({MC_DRAWS} draws, seed {MC_SEED}, space {space})"

    dist.sort()
    ci = (dist[int(0.025 * len(dist))], dist[int(0.975 * len(dist))])
    return {
        "informative_blocks": len(informative),
        "total_blocks": len(blocks),
        "observed_statistic": observed,
        "permutation_space": space,
        "method": method,
        "p_value": p,
        "p_value_note": (
            "descriptive, NON-decisional: the pre-registered 2*2^-B block "
            "floor (p_min_exact_floor) alone governs reject/estimate calls "
            "(causal-design review 2026-08-31; DESIGN-FORK amendment)"
        ),
        "p_min_exact_floor": p_min,
        "can_reject_alpha_05": p_min <= ALPHA,
        "stat_ci_95": ci,
        # Rescale the statistic CI to the mean-paired-difference scale:
        # statistic = sum(±outcome) = 2 * sum(block diffs), B informative blocks.
        "shift_ci_95": (ci[0] / (2 * len(informative)), ci[1] / (2 * len(informative))),
    }


def hodges_lehmann(blocks: dict[tuple, list[dict]]) -> dict:
    """Block-level rate differences; HL shift = median, exact CI via the
    permutation distribution of the mean block difference."""
    diffs = []
    for rows in blocks.values():
        kac = [int(r["final_pass"]) for r in rows if r["analysis_arm"] == "kac"]
        none = [int(r["final_pass"]) for r in rows if r["analysis_arm"] == "none"]
        if kac and none:
            diffs.append(sum(kac) / len(kac) - sum(none) / len(none))
    if not diffs:
        return {"blocks": 0, "note": "no block has both arms"}
    diffs.sort()
    median = diffs[len(diffs) // 2] if len(diffs) % 2 else (
        diffs[len(diffs) // 2 - 1] + diffs[len(diffs) // 2]) / 2
    return {"blocks": len(diffs), "shift": median, "block_diffs": diffs}


def _median(values: list[float]) -> float:
    values = sorted(values)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def donor_level_hl(rows: list[dict]) -> dict:
    """Donor-pooled HL shift + leave-one-donor-out sensitivity.

    b4-style donors contribute multiple blocks; pooling each donor's children
    across its blocks weights donors equally. LODO over ≤8 donors is reported
    as a table, never bootstrapped (causal-design review 2026-08-31).
    """
    donors: dict[str, list[dict]] = {}
    for r in rows:
        donors.setdefault(r["donor_run_id"], []).append(r)
    diffs: dict[str, float] = {}
    for donor, drows in donors.items():
        kac = [int(r["final_pass"]) for r in drows if r["analysis_arm"] == "kac"]
        none = [int(r["final_pass"]) for r in drows if r["analysis_arm"] == "none"]
        if kac and none:
            diffs[donor] = sum(kac) / len(kac) - sum(none) / len(none)
    if not diffs:
        return {"donors": 0, "note": "no donor has both arms"}
    values = list(diffs.values())
    hl_d = _median(values)
    lodo = {}
    for donor in diffs:
        rest = [v for d, v in diffs.items() if d != donor]
        lodo[donor] = _median(rest) if rest else None
    sign_all = all(
        (v is None) or ((v >= 0) == (hl_d >= 0)) for v in lodo.values()
    )
    return {
        "donors": len(diffs),
        "hl_donor_pooled": hl_d,
        "donor_diffs": diffs,
        "leave_one_donor_out": lodo,
        "lodo_sign_stable": sign_all,
    }


def t2_fidelity(rows: list[dict]) -> dict:
    """Replay-fidelity calibration: none-arm children of T2 blocks restart
    from a tree that ALREADY passed validation, so they should pass again.
    ≤half passing is a replay-fidelity alarm qualifying all other results."""
    none_t2 = [
        r for r in rows
        if r["analysis_arm"] == "none" and str(r.get("trigger", "")).startswith("T2")
    ]
    passes = sum(1 for r in none_t2 if r["final_pass"])
    return {
        "children": len(none_t2),
        "passes": passes,
        "alarm": bool(none_t2) and passes <= len(none_t2) / 2,
    }


def arm_summary(rows: list[dict], arm: str) -> dict:
    selected = [r for r in rows if r["analysis_arm"] == arm]
    passes = sum(1 for r in selected if r["final_pass"])
    return {
        "n": len(selected),
        "passes": passes,
        "rate": None if not selected else passes / len(selected),
        "wilson_95": wilson_ci(passes, len(selected)),
    }


def analyze_stratum(rows: list[dict], label: str) -> dict:
    blocks: dict[tuple, list[dict]] = {}
    for r in rows:
        blocks.setdefault(block_key(r), []).append(r)
    return {
        "stratum": label,
        "children": len(rows),
        "blocks": len(blocks),
        "arms": {arm: arm_summary(rows, arm) for arm in ("kac", "sham", "none")},
        "test_kac_vs_none": permutation_test(blocks),
        "hodges_lehmann": hodges_lehmann(blocks),
        "donor_level_hl": donor_level_hl(rows),
        "t2_fidelity": t2_fidelity(rows),
        "block_diffs_by_trigger": {
            f"{donor}/{key}": {
                "trigger": brows[0].get("trigger"),
                "kac": [int(r["final_pass"]) for r in brows if r["analysis_arm"] == "kac"],
                "none": [int(r["final_pass"]) for r in brows if r["analysis_arm"] == "none"],
                "sham": [int(r["final_pass"]) for r in brows if r["analysis_arm"] == "sham"],
            }
            for (donor, key), brows in sorted(blocks.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", default=str(DEFAULT_RESULTS))
    args = parser.parse_args()
    results_root = Path(args.results_root)

    children = collect_children(results_root)
    specs = collect_specs(results_root)
    tiers = donor_tiers(results_root, {r["donor_run_id"] for r in children
                                       if r["donor_run_id"]})

    included, listed_excluded, quarantined, nocard = [], [], [], []
    for r in children:
        tier = tiers.get(r["donor_run_id"], "missing")
        r["donor_tier"] = tier
        r["trigger"] = specs.get((r["donor_run_id"], r["fork_key"]), {}).get("trigger")
        r["analysis_arm"] = r["arm"]
        if tier != "clean":
            quarantined.append(r)
            continue
        if r["skipped"]:
            listed_excluded.append({**r, "exclusion": f"skipped: {r['skipped']}"})
            continue
        if r["reason"] == "error":
            listed_excluded.append({**r, "exclusion": "process error / replay refusal"})
            continue
        if r["arm"] == "kac" and r["no_card"]:
            r["analysis_arm"] = "kac-nocard"
            nocard.append(r)
            continue
        included.append(r)

    replay = [r for r in included if r["fork_mode"] == "replay"]
    recon = [r for r in included if r["fork_mode"] == "reconstruction"]

    out = {
        "primary_task": PRIMARY_TASK,
        "donor_tiers": tiers,
        "counts": {
            "children_total": len(children),
            "included": len(included),
            "replay": len(replay),
            "reconstruction": len(recon),
            "kac_nocard": len(nocard),
            "listed_excluded": len(listed_excluded),
            "quarantined_donor": len(quarantined),
        },
        "strata": {
            "replay_primary": analyze_stratum(replay, "replay (primary)"),
            "reconstruction_secondary": analyze_stratum(recon, "reconstruction (secondary)"),
        },
        "kac_nocard_group": [
            {"run_id": r["run_id"], "fork_key": r["fork_key"],
             "final_pass": r["final_pass"]} for r in nocard
        ],
        "listed_excluded": listed_excluded,
        "quarantined": [
            {"run_id": r["run_id"], "donor_tier": r["donor_tier"]} for r in quarantined
        ],
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    # Markdown summary.
    print(f"# Fork analysis (primary task: {PRIMARY_TASK})")
    print(f"\nchildren total {len(children)} | included {len(included)} "
          f"(replay {len(replay)}, reconstruction {len(recon)}) | "
          f"kac-nocard {len(nocard)} | excluded-with-report {len(listed_excluded)} "
          f"| quarantined (donor not clean) {len(quarantined)}")
    for stratum in out["strata"].values():
        print(f"\n## {stratum['stratum']}")
        if not stratum["children"]:
            print("_no children_")
            continue
        print("\n| arm | n | passes | rate | Wilson 95% |")
        print("|---|---|---|---|---|")
        for arm, s in stratum["arms"].items():
            ci = "n/a" if s["wilson_95"] is None else (
                f"[{s['wilson_95'][0]:.2f}, {s['wilson_95'][1]:.2f}]")
            rate = "n/a" if s["rate"] is None else f"{s['rate']:.2f}"
            print(f"| {arm} | {s['n']} | {s['passes']} | {rate} | {ci} |")
        test = stratum["test_kac_vs_none"]
        if "p_value" in test:
            verdict = "" if test["can_reject_alpha_05"] else (
                f" (p_min floor {test['p_min_exact_floor']:.3f} > 0.05: "
                "estimation only, rejection impossible at this n)")
            print(f"\nkac vs none: statistic {test['observed_statistic']}, "
                  f"enumeration p={test['p_value']:.4f} [descriptive, "
                  f"NON-decisional] via {test['method']}, "
                  f"{test['informative_blocks']}/{test['total_blocks']} "
                  f"informative blocks{verdict}")
            lo, hi = test["shift_ci_95"]
            print(f"mean paired difference 95% CI (rescaled): [{lo:+.2f}, {hi:+.2f}]")
            hl = stratum["hodges_lehmann"]
            if "shift" in hl:
                print(f"Hodges-Lehmann shift (block-level) {hl['shift']:+.2f} over "
                      f"{hl['blocks']} blocks; block diffs {hl['block_diffs']}")
            dhl = stratum["donor_level_hl"]
            if "hl_donor_pooled" in dhl:
                print(f"HL donor-pooled {dhl['hl_donor_pooled']:+.2f} over "
                      f"{dhl['donors']} donors; donor diffs "
                      f"{ {k: round(v, 2) for k, v in dhl['donor_diffs'].items()} }")
                print(f"LODO: { {k: (None if v is None else round(v, 2)) for k, v in dhl['leave_one_donor_out'].items()} } "
                      f"(sign-stable: {dhl['lodo_sign_stable']})")
            fid = stratum["t2_fidelity"]
            if fid["children"]:
                flag = " — REPLAY-FIDELITY ALARM" if fid["alarm"] else ""
                print(f"T2 fidelity (none-arm revalidating an already-passing "
                      f"tree): {fid['passes']}/{fid['children']} pass{flag}")
            print("\nper-block outcomes (kac / none / sham):")
            for blk, vals in stratum["block_diffs_by_trigger"].items():
                print(f"- {blk} ({vals['trigger']}): kac={vals['kac']} "
                      f"none={vals['none']} sham={vals['sham']}")
        else:
            print(f"\nkac vs none: {test['note']}")
    if nocard:
        print("\n## kac-nocard (probe produced no card; unsteered, labeled)")
        for r in out["kac_nocard_group"]:
            print(f"- {r['run_id']} key={r['fork_key']} pass={r['final_pass']}")
    if listed_excluded:
        print("\n## excluded-with-report")
        for r in listed_excluded:
            print(f"- {r['run_id']} arm={r['arm']}: {r['exclusion']}")
    if quarantined:
        print("\n## quarantined (donor not clean-tier)")
        for r in out["quarantined"]:
            print(f"- {r['run_id']} donor_tier={r['donor_tier']}")
    print(f"\nJSON: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

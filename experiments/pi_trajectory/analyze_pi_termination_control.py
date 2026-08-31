"""Round 1 analyzer: witnessable-ready conditioned strict completion + GO/NO-GO ①–⑨.

Forked from analyze_pi_commit_protocol.py, re-scoped to the DESIGN-LOCKED Round 1
estimand (ledger reports/19, 2026-08-30):
  - primary = ready-conditioned strict completion on witnessable rows, never full
    success (full success is guard metric ⑦ only);
  - W(row) replayed offline from gate-log witness lines ONLY, identically for both
    arms (arming is post-treatment and may never condition the denominator):
    exists a pass verdict (high OR low) whose fingerprint equals the last fingerprint
    observed on that row's witness channel and with no later fail verdict;
  - falsification buckets F-A (obligation-cycling vs genuine-new-work subtyping),
    F-B (any false completion -> immediate NO-GO), F-C (aggregate cost >2x on any of
    calls/tokens/wall-clock), F-D0 (no eligible evidence anywhere), F-D1
    (pass-recognised validation-shaped exit-0 commands with 0 arms AND 0 decisions
    on >=2/3 G rows, strictly distinguished from F-D0);
  - ITT over all started rows; NOT_RUN and RESOURCE_FAILURE rows excluded from all
    efficacy denominators and reported as such.
Pre-registration: this file ships inside FROZEN_SHA256SUMS before the batch; its rules
may not be edited after results are seen.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PASS_VERDICTS = {"pass_high", "pass_low"}

# 判读附件（⑨归档）：ledger Finding-4 概率表，随本 analyzer 冻结。
LUCK_TABLE = {
    "round_nature": "SCREENING/MECHANISM — 非 confirmatory；'显著'一词已从全部预期事件删除",
    "mcnemar_exact_floor": "n=3 不一致对 3/3 → 双侧精确 McNemar p = 0.25",
    "conjunctive_go_luck_upper": {"p_hat_0.65": 0.324, "p_hat_1/3_reality_prior": 0.200},
    "note": "任何 GO 使 Round 2 成为强制 confirmatory 阶段（不是可选项）",
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def replay_ready(gate_rows: list[dict[str, Any]], condition: str) -> dict[str, Any]:
    """Symmetric offline W replay from the witness channel only (see module docstring)."""
    event = "witness" if condition == "G" else "OBSERVE_WITNESS"
    validations = [row for row in gate_rows if row.get("event") == event and row.get("verdict") in PASS_VERDICTS | {"fail"}]
    passes = [row for row in validations if row.get("verdict") in PASS_VERDICTS]
    fails = [row for row in validations if row.get("verdict") == "fail"]
    fps = [row.get("fingerprint") for row in validations if row.get("fingerprint")]
    last_fp = fps[-1] if fps else None
    ready_at_last_validation = bool(
        passes
        and last_fp
        and any(
            p.get("fingerprint") == last_fp and not any((f.get("seq") or 0) > (p.get("seq") or 0) for f in fails)
            for p in passes
        )
    )
    ever_ready_in_situ = any(row.get("event") == "GATE_ARM" for row in gate_rows) or any(
        row.get("event") == "OBSERVE_WITNESS" and row.get("ready") for row in gate_rows
    )
    first_ready_seq = min((p.get("seq", 10**9) for p in passes), default=None)
    redundant = sum(1 for row in validations if (row.get("seq") or 0) > first_ready_seq) if first_ready_seq is not None else 0
    # G-only FACT: the final workspace fingerprint (from finalize/accept/reject lines) vs
    # the last witness-channel fingerprint = post-last-validation drift on the destructive path.
    final_fp_rows = [
        (row.get("decision") or {}).get("workspaceFingerprint") or row.get("receipt", {}).get("workspaceFingerprint")
        for row in gate_rows
        if row.get("event") in {"finalize_decision", "agent_end"}
    ]
    final_fp_rows = [fp for fp in final_fp_rows if fp]
    return {
        "W": ready_at_last_validation,
        "ever_ready_in_situ": ever_ready_in_situ,
        "post_ready_lost_in_situ": ever_ready_in_situ and not ready_at_last_validation,
        "pass_witnesses": len(passes),
        "pass_high": sum(1 for row in passes if row.get("verdict") == "pass_high"),
        "pass_low": sum(1 for row in passes if row.get("verdict") == "pass_low"),
        "fail_witnesses": len(fails),
        "validation_count": len(validations),
        "post_first_ready_redundant_validations": redundant,
        "final_vs_last_witness_fp_drift": (final_fp_rows[-1] != last_fp) if (final_fp_rows and last_fp) else None,
    }


def obligation_profile(gate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    obligations = [row for row in gate_rows if row.get("event") == "OBLIGATION_TEXT"]
    texts = [str(row.get("text", "")) for row in obligations]
    arms = sum(1 for row in gate_rows if row.get("event") == "GATE_CONTINUE")
    distinct = len({text.strip().lower() for text in texts})
    return {
        "arms": arms,
        "obligation_count": len(texts),
        "obligation_distinct": distinct,
        "obligation_rewrite_ratio": (distinct / arms) if arms else None,
        "obligation_max_len": max((len(text) for text in texts), default=0),
        "obligation_shas": [row.get("sha256") for row in obligations],
        "cap_exit": any(row.get("event") == "CAP_EXIT" for row in gate_rows),
        "gate_text_escapes": sum(1 for row in gate_rows if row.get("event") == "GATE_TEXT_ESCAPE"),
        "off_menu_calls": sum(1 for row in gate_rows if row.get("event") == "GATE_OFF_MENU_CALL"),
        "violations": sorted(str(row.get("reason", "unknown")) for row in gate_rows if row.get("event") == "GATE_VIOLATION"),
        "finalize_attempts": sum(1 for row in gate_rows if row.get("event") == "FINALIZE_ATTEMPT"),
        "accepted": any(
            row.get("event") == "finalize_decision" and (row.get("decision") or {}).get("status") == "accepted"
            for row in gate_rows
        ),
        "rejected_gaps": [
            gap
            for row in gate_rows
            if row.get("event") == "finalize_decision" and (row.get("decision") or {}).get("status") == "rejected"
            for gap in (row.get("decision") or {}).get("gaps", [])
        ],
    }


def arm_count(entry: dict[str, Any]) -> int:
    return int((entry.get("gate_event_counts") or {}).get("GATE_ARM", 0))


def arm_strict(entries: list[dict[str, Any]]) -> dict[str, Any]:
    w = [entry for entry in entries if entry["W"]]
    strict_in_w = [entry for entry in w if entry["strict_completion_success"]]
    return {
        "rows": len(entries),
        "witnessable_W": len(w),
        "strict_in_W": len(strict_in_w),
        "ready_conditioned_strict_rate": (len(strict_in_w) / len(w)) if w else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_index", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.run_index.read_text(encoding="utf-8"))
    manifest = payload["manifest"]
    rows = payload["rows"]
    root = args.run_index.resolve().parent

    started: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        run_dir = Path(row["run_dir"])
        gate_rows = load_jsonl(run_dir / "completion-gate.jsonl")
        entry = dict(row)
        entry.update(replay_ready(gate_rows, row["condition"]))
        if row["condition"] == "G":
            entry.update(obligation_profile(gate_rows))
        entry["valid"] = not entry.get("monitor_failure")
        started[(str(row["task_id"]), str(row["condition"]))] = entry

    valid_g = [entry for (_, cond), entry in started.items() if cond == "G" and entry["valid"]]
    valid_n = [entry for (_, cond), entry in started.items() if cond == "N" and entry["valid"]]
    tasks = sorted({task_id for (task_id, _) in started})
    incomplete_pairs = [task_id for task_id in tasks if (task_id, "N") not in started or (task_id, "G") not in started]

    g_rate, n_rate = arm_strict(valid_g), arm_strict(valid_n)
    g_armed = [entry for entry in valid_g if arm_count(entry) > 0 or entry.get("arms", 0) > 0 or entry.get("finalize_attempts", 0) > 0]

    pair_rows = []
    for task_id in tasks:
        g, n = started.get((task_id, "G")), started.get((task_id, "N"))
        pair_rows.append(
            {
                "task_id": task_id,
                "G": g and {k: g.get(k) for k in ("W", "strict_completion_success", "evaluation_success", "timed_out", "arms", "accepted", "model_calls", "wall_clock_seconds", "final_vs_last_witness_fp_drift")},
                "N": n and {k: n.get(k) for k in ("W", "strict_completion_success", "evaluation_success", "timed_out", "model_calls", "wall_clock_seconds")},
            }
        )

    cost = {
        "model_calls": {"G": sum(e["model_calls"] for e in valid_g), "N": sum(e["model_calls"] for e in valid_n)},
        "total_tokens": {
            "G": sum(int(e["usage"]["totalTokens"]) for e in valid_g),
            "N": sum(int(e["usage"]["totalTokens"]) for e in valid_n),
        },
        "wall_clock_seconds": {
            "G": round(sum(e["wall_clock_seconds"] for e in valid_g), 1),
            "N": round(sum(e["wall_clock_seconds"] for e in valid_n), 1),
        },
    }
    ratios = {metric: (value["G"] / value["N"] if value["N"] else None) for metric, value in cost.items()}

    checks: dict[str, bool] = {}
    checks["c1_gate_triggers_ge_2of3_G_rows"] = sum(1 for e in valid_g if arm_count(e) > 0) >= 2
    checks["c2_denominator_guard_W_G_ge_2"] = g_rate["witnessable_W"] >= 2
    g_dir, n_dir = g_rate["ready_conditioned_strict_rate"], n_rate["ready_conditioned_strict_rate"]
    checks["c3_G_directionally_gt_N"] = g_dir is not None and n_dir is not None and g_dir > n_dir
    checks["c4_G_rate_ge_2of3_on_witnessable"] = g_dir is not None and g_dir >= 2 / 3 - 1e-12
    mechanism_hits = [
        task_id
        for task_id in tasks
        if started.get((task_id, "G")) is not None
        and started.get((task_id, "N")) is not None
        and started[(task_id, "G")].get("accepted")
        and started[(task_id, "N")]["W"]
        and started[(task_id, "N")]["timed_out"]
    ]
    checks["c5_mechanism_clause"] = len(mechanism_hits) >= 1
    false_completions = [e for e in valid_g if e["false_completion"]]
    checks["c6_no_false_completions"] = len(false_completions) == 0
    checks["c7_full_success_guard_G_ge_N"] = sum(bool(e["evaluation_success"]) for e in valid_g) >= sum(
        bool(e["evaluation_success"]) for e in valid_n
    )
    checks["c8_cost_le_2x"] = all(ratio is not None and ratio <= 2.0 for ratio in ratios.values())
    checks["c9_luck_table_archived"] = True
    go = all(checks.values())

    bucket: dict[str, Any]
    fd1_rows = sum(1 for e in valid_g if e["pass_witnesses"] > 0 and arm_count(e) == 0 and e.get("finalize_attempts", 0) == 0)
    if false_completions:
        bucket = {
            "primary": "F-B",
            "note": "gate 制造自信终止 → 理论以危险方式错误 → 即时 NO-GO，如实报告（false completion 行: %s）"
            % sorted(e["task_id"] for e in false_completions),
        }
    elif any(ratio is not None and ratio > 2.0 for ratio in ratios.values()):
        bucket = {
            "primary": "F-C",
            "note": "成本 >2×（含 gate 轮 cache 打断代价）→ 即便有效也不交付；ratios=%s" % json.dumps(ratios),
        }
    elif not any(e["pass_witnesses"] > 0 for e in valid_g + valid_n):
        bucket = {"primary": "F-D0", "note": "全轮 0 次合格证据 → 机制 NO-GO、理论未获评估；Round 2 需改证据采集面"}
    elif fd1_rows >= 2 and not g_armed:
        bucket = {
            "primary": "F-D1",
            "note": "trigger starvation：%d/3 G 行含 classifier 认可(pass_high|low) 的 exit-0 验证形命令却 0 arm ∧ 0 决策；"
            "Round 2 候选换句法触发（首个非基线指纹 Δ / turn 预算对半界标）" % fd1_rows,
        }
    elif g_armed and all(e["timed_out"] for e in g_armed) and not any(e.get("accepted") for e in g_armed):
        bucket = {
            "primary": "F-A",
            "subtyping": {
                "rule": "子型1 obligation cycling（arms 是改写、放行动作重复）→ 干预改称 binding gap，Round 2 = discharge/binding；"
                "子型2 genuine remaining work（每 arm 新 mutation/新 scope）→ capability wall，Round 2 = budget/任务重选",
                "per_row": [
                    {
                        "task": e["task_id"],
                        "arms": e.get("arms"),
                        "obligation_distinct_vs_arms": [e.get("obligation_distinct"), e.get("arms")],
                        "obligation_rewrite_ratio": e.get("obligation_rewrite_ratio"),
                        "cap_exit": e.get("cap_exit"),
                        "gate_text_escapes": e.get("gate_text_escapes"),
                    }
                    for e in g_armed
                ],
            },
            "note": "决策被强制索取仍全部超时未 accept → 见子型 FACT；censoring 条款：arms-CAP/backstop 耗尽亦入本桶",
        }
    elif go:
        bucket = {"primary": "GO", "note": "合取判据 ①–⑧ 全过；⑨概率表已随本 JSON 归档；Round 2 = 强制 confirmatory"}
    else:
        bucket = {
            "primary": "INCONCLUSIVE",
            "note": "不满足任何证伪桶且合取未全过（如 N 在 witnessable 子集全 strict 的无 headroom 形态）→ 如实报告，任务规则进 Round 2",
        }

    result = {
        "schema_version": 1,
        "source_run_index": str(args.run_index),
        "round_nature": "SCREENING/MECHANISM（ledger Finding 4）",
        "authority": manifest.get("authority"),
        "estimand": "ready-conditioned strict completion on witnessable rows; full success = guard ⑦ only",
        "itt": {
            "started_rows": len(started),
            "not_run_or_missing_pairs": incomplete_pairs,
            "resource_failure_rows": sorted(f"{t}__{c}" for (t, c), e in started.items() if not e["valid"]),
        },
        "per_row": {
            f"{task_id}__{cond}": entry for (task_id, cond), entry in sorted(started.items())
        },
        "w_g": g_rate["witnessable_W"],
        "w_n": n_rate["witnessable_W"],
        "primary_G": g_rate,
        "primary_N": n_rate,
        "pairs": pair_rows,
        "cost": {"aggregates": cost, "ratio_G_over_N": ratios},
        "secondary": {
            "post_first_ready_redundant_validations": {
                f"{t}__{c}": e["post_first_ready_redundant_validations"] for (t, c), e in started.items()
            },
            "post_ready_lost_in_situ": {f"{t}__{c}": e["post_ready_lost_in_situ"] for (t, c), e in started.items()},
            "pre_registered_prediction": "两通道预测 ≈0；零结果 = 预测被确认，不是实验失败",
        },
        "go_checks": checks,
        "GO": go,
        "falsification_bucket": bucket,
        "censoring_note": manifest.get("censoring_note"),
        "luck_table": LUCK_TABLE,
        "mechanism_clause_task_hits": mechanism_hits,
    }
    output = args.output or root / "comparison-index.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"GO": go, "bucket": bucket["primary"], "checks": checks, "primary_G": g_rate, "primary_N": n_rate, "cost_ratios": ratios},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

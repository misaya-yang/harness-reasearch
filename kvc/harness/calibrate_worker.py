"""Calibrate one task (base + gold) through the retargeted legacy pipeline.

Parallel-safe across different tasks: each worker writes only under
.cache/calibration/<task>/, and the shared dependency stage is read-only once
built. Base and gold of the SAME task run sequentially inside one worker so
two workers never contend on one task directory.

Exit code 0 iff the task calibrates as base-fail AND gold-pass.

Usage: python3 -m kvc.harness.calibrate_worker --task pi-find-root-relativization
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from kvc.harness.pi_bridge import KVC_ROOT, load_task, retarget

CAL_ROOT = KVC_ROOT / ".cache" / "calibration"


def _variant_state(summary: dict) -> str:
    if summary.get("error"):
        return "error"
    return "pass" if summary.get("success") else "fail"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--suite", default="v3")
    args = parser.parse_args()

    pt = retarget()
    task = load_task(args.task, args.suite)
    detail: dict[str, dict] = {}
    for variant in ("base", "gold"):
        outdir = CAL_ROOT / args.task / variant
        existing = outdir / "calibration.json"
        if existing.exists():
            summary = json.loads(existing.read_text(encoding="utf-8"))
        else:
            started = time.monotonic()
            try:
                summary = pt.calibrate(task, variant, outdir)
            except Exception as exc:  # record, keep going to the other variant
                summary = {
                    "task_id": args.task,
                    "variant": variant,
                    "success": False,
                    "error": repr(exc),
                }
                outdir.mkdir(parents=True, exist_ok=True)
                existing.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            summary["duration_seconds"] = round(time.monotonic() - started, 1)
        detail[variant] = {
            "state": _variant_state(summary),
            "success": summary.get("success"),
            "error": summary.get("error", ""),
            "duration_seconds": summary.get("duration_seconds"),
        }

    verdict = {
        "task_id": args.task,
        "suite": args.suite,
        "base": detail["base"]["state"],
        "gold": detail["gold"]["state"],
        "calibrated": detail["base"]["state"] == "fail" and detail["gold"]["state"] == "pass",
        "detail": detail,
    }
    print(json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict["calibrated"] else 1


if __name__ == "__main__":
    sys.exit(main())

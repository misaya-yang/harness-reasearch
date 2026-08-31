"""Materialize one task workspace through the retargeted legacy pipeline.

Usage (from harness-reasearch/):
    python3 -m kvc.harness.materialize --task pi-retry-attempt-timeout --out /tmp/kvc-mat-test

This exercises: git archive of base_commit from the local Pi checkout, git
init + benchmark-base commit, neutral dependency staging (first run clones
node_modules via APFS clonefiles), and workspace linking. No provider key.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from kvc.harness.pi_bridge import list_tasks, load_task, retarget


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--suite", default="v3")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--list", action="store_true", help="list task ids and exit")
    parser.add_argument("--clean", action="store_true", help="remove --out first if present")
    args = parser.parse_args()

    if args.list:
        for task_id in list_tasks(args.suite):
            print(task_id)
        return 0

    pt = retarget()
    task = load_task(args.task, args.suite)
    if args.clean and args.out.exists():
        shutil.rmtree(args.out)
    result = pt.prepare(task, args.out)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""R6 freeze manifest: sha256 every artifact that defines the frozen state.

Collects hashes of task suites, prompt templates, harness sources, oracle /
sham card constants, the model/provider configuration, and the exact Pi
source HEAD used as upstream reference. Output: kvc/freeze-manifest.json.
Idempotent and offline; rerun after any change and diff.

Usage: python3 -m kvc.harness.freeze_manifest
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

KVC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = KVC_ROOT.parent
LEGACY_TASKS = REPO_ROOT / "experiments" / "pi_trajectory" / "tasks"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect() -> dict:
    manifest: dict = {"schema": "kvc-freeze-manifest/1"}

    # Task suites (legacy location; read-only).
    suites = {}
    for path in sorted(LEGACY_TASKS.glob("pi_*tasks*.jsonl")):
        suites[path.name] = sha256_file(path)
    manifest["task_suites"] = suites

    # Prompt templates and config files under kvc/configs.
    configs = {}
    for path in sorted((KVC_ROOT / "configs").rglob("*")):
        if path.is_file():
            configs[str(path.relative_to(KVC_ROOT))] = sha256_file(path)
    manifest["configs"] = configs

    # Harness sources (the experiment IS these files).
    harness = {}
    for path in sorted((KVC_ROOT / "harness").glob("*.py")):
        harness[path.name] = sha256_file(path)
    manifest["harness"] = harness

    # Protocol documents.
    docs = {}
    for name in ("DESIGN.md", "DESIGN-FORK.md", "DESIGN-R5.md",
                 "PLAN-6ROUNDS.md"):
        path = KVC_ROOT / name
        if path.exists():
            docs[name] = sha256_file(path)
    manifest["protocol_docs"] = docs

    # Model / provider constants (imported, hashed as rendered strings).
    from kvc.harness.providers import KEY_ENV_NAME, QWEN_FLASH_ID

    manifest["model"] = {
        "provider": "dashscope-intl",
        "model_id": QWEN_FLASH_ID,
        "key_env_name": KEY_ENV_NAME,
        "thinking_level": "off",
        "budget_seconds_default": 420.0,
    }

    # Pi upstream reference commit (read-only clone).
    try:
        from kvc.harness.pi_bridge import LOCAL_PI_REPO

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=LOCAL_PI_REPO,
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        manifest["pi_upstream_head"] = head
    except Exception as error:  # pragma: no cover
        manifest["pi_upstream_head_error"] = repr(error)

    # Frozen fork-protocol constants, hashed from their source of record.
    from kvc.harness import run_fork_child as rfc

    manifest["fork_constants"] = {
        "STEER_DELAY_SECONDS": rfc.STEER_DELAY_SECONDS,
        "MIN_CHILD_BUDGET_SECONDS": rfc.MIN_CHILD_BUDGET_SECONDS,
        "MIN_SESSION_SNAPSHOT_BYTES": rfc.MIN_SESSION_SNAPSHOT_BYTES,
        "RESUME_PROMPT_sha256": sha256_text(rfc.RESUME_PROMPT),
        "SHAM_CARD_sha256": sha256_text(
            json.dumps(rfc.SHAM_CARD, sort_keys=True, ensure_ascii=False)
        ),
    }
    return manifest


def main() -> int:
    manifest = collect()
    out = KVC_ROOT / "freeze-manifest.json"
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    total = sum(len(v) for k, v in manifest.items() if isinstance(v, dict)
                and k != "model" and k != "fork_constants")
    print(f"wrote {out} ({total} hashed files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Retarget the legacy pi_trajectory task machinery onto this machine.

The original module hardcodes /Users/yang paths (PI_REPO, neutral cache root).
This bridge patches those module-level constants *before any staging call* and
rewrites per-task source_repo fields, without modifying the legacy file.

Usage:
    from kvc.harness.pi_bridge import retarget, load_task
    retarget()
    task = load_task("pi-retry-attempt-timeout")
"""

from __future__ import annotations

import json
import os
import pwd
import types
from pathlib import Path
from typing import Any

KVC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = KVC_ROOT.parent
LEGACY_DIR = REPO_ROOT / "experiments" / "pi_trajectory"


def _real_home() -> Path:
    # The validator subprocess runs with the actor's fake HOME; the Pi checkout
    # must still resolve to the real user home, so use the passwd entry, not $HOME.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


LOCAL_PI_REPO = _real_home() / "misaya_project" / "Agent_projects" / "pi"
LOCAL_CACHE_ROOT = KVC_ROOT / ".cache"

TASK_SUITES: dict[str, Path] = {
    "v1": LEGACY_DIR / "tasks" / "pi_tasks_v1.jsonl",
    "ai_v1": LEGACY_DIR / "tasks" / "pi_ai_tasks_v1.jsonl",
    "ai_v2": LEGACY_DIR / "tasks" / "pi_ai_tasks_v2.jsonl",
    "v3": LEGACY_DIR / "tasks" / "pi_coding_tasks_v3.jsonl",
}

_pi_tasks: Any = None

# The legacy module derives cache paths from PI_REPO at import time, so the
# retarget must happen in the source text before exec — patching attributes
# afterwards would be too late. Exactly two constant lines are rewritten;
# the asserts fail loudly if the legacy file drifts.
_ORIG_PI_REPO_LINE = 'PI_REPO = Path("/Users/yang/projects/opensource-harness/pi")'
_ORIG_CACHE_LINE = 'NEUTRAL_CACHE_ROOT = Path("/Users/Shared/pi-peac-experiment")'


def legacy_module() -> Any:
    """Import pi_tasks.py with the two machine paths rewritten to local ones."""
    global _pi_tasks
    if _pi_tasks is not None:
        return _pi_tasks
    path = LEGACY_DIR / "pi_tasks.py"
    source = path.read_text(encoding="utf-8")
    assert _ORIG_PI_REPO_LINE in source, "legacy PI_REPO constant moved or renamed"
    assert _ORIG_CACHE_LINE in source, "legacy NEUTRAL_CACHE_ROOT constant moved or renamed"
    source = source.replace(_ORIG_PI_REPO_LINE, f"PI_REPO = Path({str(LOCAL_PI_REPO)!r})")
    source = source.replace(
        _ORIG_CACHE_LINE, f"NEUTRAL_CACHE_ROOT = Path({str(LOCAL_CACHE_ROOT)!r})"
    )
    module = types.ModuleType("legacy_pi_tasks")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    _pi_tasks = module
    return module


def retarget() -> Any:
    """Return the legacy module retargeted at the local Pi checkout."""
    return legacy_module()


def load_task(task_id: str, suite: str = "v3") -> dict[str, Any]:
    """Load one task row and rewrite source_repo to the local Pi checkout."""
    path = TASK_SUITES[suite]
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("task_id") == task_id:
            row["source_repo"] = str(LOCAL_PI_REPO)
            return row
    raise KeyError(f"task {task_id!r} not found in suite {suite!r}")


def list_tasks(suite: str = "v3") -> list[str]:
    path = TASK_SUITES[suite]
    return [
        json.loads(line)["task_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def ensure_base_mirror(base_commit_spec: str, pi_repo: Path | None = None) -> tuple[Path, str]:
    """Materialize a synthetic repo holding ONLY the base tree.

    The validator's task.json names a source repo; if it names the real Pi
    checkout, an actor that finds that file can walk the repo history to the
    fix. The mirror contains a single commit whose tree equals the base tree —
    no fix commit, no history, no identifying remote. Cached per base sha.

    Returns (mirror_repo_path, mirror_commit_sha).
    """
    import shutil
    import subprocess
    import tempfile

    repo = pi_repo or LOCAL_PI_REPO
    base_sha = subprocess.run(
        ["git", "rev-parse", base_commit_spec],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    cache_root = LOCAL_CACHE_ROOT / "source-mirror"
    mirror = cache_root / f"base-{base_sha[:16]}.git"
    sha_file = cache_root / f"base-{base_sha[:16]}.sha"
    if mirror.exists() and sha_file.exists():
        return mirror, sha_file.read_text(encoding="utf-8").strip()

    cache_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="mirror-stage-", dir=cache_root))
    try:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", base_sha],
            cwd=repo, capture_output=True, check=True,
        ).stdout
        subprocess.run(["tar", "-x"], cwd=staging, input=archive, check=True)
        subprocess.run(["git", "init", "-q"], cwd=staging, check=True)
        subprocess.run(["git", "add", "-A"], cwd=staging, check=True)
        subprocess.run(
            ["git", "-c", "user.name=KVC", "-c", "user.email=kvc@invalid",
             "commit", "-q", "-m", "base tree"],
            cwd=staging, check=True,
        )
        mirror_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=staging, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if mirror.exists():
            shutil.rmtree(mirror)
        subprocess.run(
            ["git", "clone", "-q", "--bare", "--no-local", str(staging), str(mirror)],
            check=True,
        )
        sha_file.write_text(mirror_sha + "\n", encoding="utf-8")
        return mirror, mirror_sha
    finally:
        shutil.rmtree(staging, ignore_errors=True)

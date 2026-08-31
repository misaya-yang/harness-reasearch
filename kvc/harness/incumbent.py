"""Commit-based incumbent management (DESIGN.md section 3.5, deviation #1).

Every passing validation of the current epoch becomes a tagged commit in the
workspace's own git repository. On cutoff/abort without delivery, the latest
incumbent can be restored; such outcomes are scored separately as workspace
rescue and never merged into strict autonomous completion.

The Pi source repository is never touched: all git operations happen inside
the materialized per-run workspace.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TAG_PREFIX = "kvc/incumbent-"
GIT_IDENTITY = ("-c", "user.name=KVC Benchmark", "-c", "user.email=kvc@invalid")


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass
class RescueResult:
    rescued_to: str
    rescued_tag: str
    prior_head: str


class IncumbentManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def save(self, epoch: int) -> str:
        """Commit the full working tree and tag it as incumbent for this epoch."""
        _git(self.workspace, "add", "-A")
        # Nothing to commit means the passing state equals the previous tree;
        # still tag HEAD so rescue targets the validated state.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status:
            _git(self.workspace, "commit", "-q", "-m", f"kvc incumbent epoch {epoch}")
        tag = f"{TAG_PREFIX}{epoch}"
        _git(self.workspace, "tag", "-f", tag)
        return _git(self.workspace, "rev-parse", "HEAD")

    def latest(self) -> tuple[str, str] | None:
        """Newest incumbent (tag, commit sha) by epoch number, or None."""
        output = subprocess.run(
            ["git", "tag", "--list", f"{TAG_PREFIX}*"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        best: tuple[int, str] | None = None
        for tag in output:
            match = re.fullmatch(re.escape(TAG_PREFIX) + r"(\d+)", tag)
            if match:
                epoch = int(match.group(1))
                if best is None or epoch > best[0]:
                    best = (epoch, tag)
        if best is None:
            return None
        tag = best[1]
        sha = _git(self.workspace, "rev-parse", f"{tag}^{{commit}}")
        return tag, sha

    def rescue(self) -> RescueResult | None:
        """Reset the workspace to the latest incumbent. Returns None if absent."""
        latest = self.latest()
        if latest is None:
            return None
        tag, sha = latest
        prior_head = _git(self.workspace, "rev-parse", "HEAD")
        _git(self.workspace, "reset", "--hard", sha)
        _git(self.workspace, "clean", "-fd")
        return RescueResult(rescued_to=sha, rescued_tag=tag, prior_head=prior_head)

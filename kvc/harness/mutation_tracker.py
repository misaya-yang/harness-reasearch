"""Mutation epoch detection from workspace git state, not tool names.

The agent can mutate files via bash as well as edit/write, so epoch decisions
rest on content fingerprints of the production path set after every
tool_execution_end. Scratch paths never count (DESIGN.md section 3.2).
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

IGNORED_PREFIXES = ("node_modules/", ".git/", "scratch/")


@dataclass
class EpochEvent:
    epoch: int
    at_monotonic: float
    tool_name: str
    paths_changed: list[str]
    fingerprint: str
    diff_stat: str


@dataclass
class MutationTracker:
    workspace: Path
    scratch_prefixes: tuple[str, ...] = ("scratch/",)
    epoch: int = 0
    last_fingerprint: str = ""
    history: list[EpochEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Baseline at construction: the freshly materialized workspace state.
        self.last_fingerprint, _ = self.fingerprint()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def _is_production(self, path: str) -> bool:
        if any(path.startswith(prefix) for prefix in IGNORED_PREFIXES):
            return False
        return not any(path.startswith(prefix) for prefix in self.scratch_prefixes)

    def _changed_production(self) -> list[tuple[str, str]]:
        """(status, path) pairs for production-path working-tree changes vs HEAD."""
        changes: list[tuple[str, str]] = []
        for line in self._git("diff", "HEAD", "--name-status").splitlines():
            if not line.strip():
                continue
            status, path = line.split("\t", 1)
            if self._is_production(path):
                changes.append((status, path))
        for line in self._git("ls-files", "--others", "--exclude-standard").splitlines():
            path = line.strip()
            if path and self._is_production(path):
                changes.append(("A?", path))
        return sorted(changes)

    def fingerprint(self) -> tuple[str, list[str]]:
        """Content fingerprint over production changes; also returns the paths."""
        changes = self._changed_production()
        digest = hashlib.sha256()
        paths: list[str] = []
        for status, path in changes:
            digest.update(status.encode())
            digest.update(b"\0")
            digest.update(path.encode())
            file_path = self.workspace / path
            if file_path.is_file() and status != "D":
                blob = self._git("hash-object", "--", path).strip()
                digest.update(blob.encode())
            digest.update(b"\n")
            paths.append(path)
        return digest.hexdigest(), paths

    def diff_stat(self) -> str:
        return self._git("diff", "HEAD", "--stat").strip()

    def observe(self, tool_name: str) -> EpochEvent | None:
        """Call after each bash/edit/write tool_execution_end.

        Returns an EpochEvent exactly when the production content fingerprint
        changed since the previous observation.
        """
        current, paths = self.fingerprint()
        if current == self.last_fingerprint:
            return None
        self.epoch += 1
        event = EpochEvent(
            epoch=self.epoch,
            at_monotonic=time.monotonic(),
            tool_name=tool_name,
            paths_changed=paths,
            fingerprint=current,
            diff_stat=self.diff_stat(),
        )
        self.last_fingerprint = current
        self.history.append(event)
        return event

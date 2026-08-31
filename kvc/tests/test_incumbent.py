"""Offline tests for commit-based incumbent save/restore."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from kvc.harness.incumbent import IncumbentManager

GIT_IDENTITY = ("-c", "user.name=KVC Test", "-c", "user.email=kvc-test@invalid")


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="kvc-inc-"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "main.ts").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", *GIT_IDENTITY, "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *GIT_IDENTITY, "commit", "-q", "-m", "base"], cwd=root, check=True)
    return root


class TestIncumbent(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.manager = IncumbentManager(self.repo)

    def head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_no_incumbent_rescue_is_none(self) -> None:
        self.assertIsNone(self.manager.latest())
        self.assertIsNone(self.manager.rescue())

    def test_save_tags_validated_state(self) -> None:
        (self.repo / "src" / "main.ts").write_text("v2-correct\n", encoding="utf-8")
        sha = self.manager.save(epoch=1)
        tag, tagged_sha = self.manager.latest()
        assert tag is not None
        self.assertEqual(tag, "kvc/incumbent-1")
        self.assertEqual(tagged_sha, sha)
        self.assertEqual(sha, self.head())

    def test_rescue_restores_latest_incumbent(self) -> None:
        (self.repo / "src" / "main.ts").write_text("v2-correct\n", encoding="utf-8")
        good_sha = self.manager.save(epoch=1)
        # Regressing exploration after the pass:
        (self.repo / "src" / "main.ts").write_text("v3-broken\n", encoding="utf-8")
        (self.repo / "src" / "junk.ts").write_text("scratch-like\n", encoding="utf-8")
        result = self.manager.rescue()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.rescued_to, good_sha)
        self.assertEqual(self.head(), good_sha)
        self.assertEqual((self.repo / "src" / "main.ts").read_text(encoding="utf-8"), "v2-correct\n")
        self.assertFalse((self.repo / "src" / "junk.ts").exists())

    def test_latest_picks_highest_epoch(self) -> None:
        self.manager.save(epoch=1)
        (self.repo / "src" / "main.ts").write_text("v3\n", encoding="utf-8")
        self.manager.save(epoch=2)
        tag, _ = self.manager.latest()
        assert tag is not None
        self.assertEqual(tag, "kvc/incumbent-2")


if __name__ == "__main__":
    unittest.main()

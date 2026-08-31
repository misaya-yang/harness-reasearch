"""Offline tests for mutation epoch detection on a scratch git workspace."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from kvc.harness.mutation_tracker import MutationTracker

GIT_IDENTITY = ("-c", "user.name=KVC Test", "-c", "user.email=kvc-test@invalid")


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="kvc-mt-"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "main.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    subprocess.run(["git", *GIT_IDENTITY, "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *GIT_IDENTITY, "commit", "-q", "-m", "base"], cwd=root, check=True)
    return root


class TestMutationTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.tracker = MutationTracker(workspace=self.repo)

    def test_no_change_no_epoch(self) -> None:
        self.assertIsNone(self.tracker.observe("read"))
        self.assertIsNone(self.tracker.observe("bash"))
        self.assertEqual(self.tracker.epoch, 0)

    def test_edit_creates_epoch(self) -> None:
        (self.repo / "src" / "main.ts").write_text("export const a = 2;\n", encoding="utf-8")
        event = self.tracker.observe("edit")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.epoch, 1)
        self.assertEqual(event.paths_changed, ["src/main.ts"])
        self.assertEqual(self.tracker.observe("edit"), None)  # idempotent

    def test_bash_mutation_counts_too(self) -> None:
        subprocess.run(
            ["sed", "-i", "", "s/a = 1/a = 9/", "src/main.ts"],
            cwd=self.repo,
            check=True,
        )
        event = self.tracker.observe("bash")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.tool_name, "bash")
        self.assertEqual(event.epoch, 1)

    def test_new_untracked_production_file_counts(self) -> None:
        (self.repo / "src" / "helper.ts").write_text("export const b = 0;\n", encoding="utf-8")
        event = self.tracker.observe("write")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("src/helper.ts", event.paths_changed)

    def test_scratch_and_ignored_paths_never_count(self) -> None:
        (self.repo / "scratch").mkdir()
        (self.repo / "scratch" / "probe.ts").write_text("// probe\n", encoding="utf-8")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "junk.js").write_text("//x\n", encoding="utf-8")
        self.assertIsNone(self.tracker.observe("bash"))
        self.assertEqual(self.tracker.epoch, 0)

    def test_two_mutations_two_epochs(self) -> None:
        (self.repo / "src" / "main.ts").write_text("export const a = 2;\n", encoding="utf-8")
        self.tracker.observe("edit")
        (self.repo / "src" / "main.ts").write_text("export const a = 3;\n", encoding="utf-8")
        event = self.tracker.observe("edit")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.epoch, 2)
        self.assertEqual(len(self.tracker.history), 2)


if __name__ == "__main__":
    unittest.main()

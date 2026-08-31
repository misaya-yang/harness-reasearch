from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pi_tasks import (
    PI_REPO,
    evaluator_sandbox_settings,
    invalid_source_entries,
    load_tasks,
    prepare,
    run_command,
    workspace_patch,
)


class PiTaskIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pi-task-isolation-test-")
        self.root = Path(self.temporary.name)
        self.task = load_tasks()["pi-custom-message-tool-result-order"]
        self.workspace = self.root / "workspace"
        prepare(self.task, self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_monorepo_links_resolve_to_base_pinned_workspace(self) -> None:
        coding_agent = (
            self.workspace / "node_modules" / "@earendil-works" / "pi-coding-agent"
        ).resolve()
        sandbox_extension = self.workspace / "node_modules" / "pi-extension-sandbox"
        legacy_glob = (self.workspace / "node_modules" / "glob").resolve()

        self.assertEqual(
            coding_agent,
            (self.workspace / "packages" / "coding-agent").resolve(),
        )
        self.assertFalse(sandbox_extension.exists())
        self.assertEqual(
            legacy_glob,
            (self.workspace / "node_modules" / "glob").resolve(),
        )

        forbidden_roots = [PI_REPO / "packages", Path(__file__).resolve().parents[2]]
        for link in (self.workspace / "node_modules").rglob("*"):
            if not link.is_symlink():
                continue
            resolved = link.resolve()
            self.assertFalse(
                any(resolved == root or root in resolved.parents for root in forbidden_roots),
                f"dependency leak: {link} -> {resolved}",
            )

    def test_source_only_patch_excludes_tests_and_configuration(self) -> None:
        source = self.workspace / "packages" / "coding-agent" / "src" / "index.ts"
        test_file = self.workspace / "packages" / "coding-agent" / "test" / "ignored.test.ts"
        config = self.workspace / "packages" / "coding-agent" / "vitest.config.ts"
        source.write_text(source.read_text(encoding="utf-8") + "\n// source-only-test\n", encoding="utf-8")
        test_file.write_text("throw new Error('ignored');\n", encoding="utf-8")
        config.write_text(config.read_text(encoding="utf-8") + "\n// ignored-config\n", encoding="utf-8")

        patch = workspace_patch(
            self.workspace,
            included_pathspecs=[":(glob)packages/*/src/**"],
        ).decode()
        self.assertIn("source-only-test", patch)
        self.assertNotIn("ignored.test.ts", patch)
        self.assertNotIn("ignored-config", patch)

    def test_source_symlink_escape_is_rejected(self) -> None:
        escape = self.workspace / "packages" / "coding-agent" / "src" / "escape.ts"
        escape.symlink_to(PI_REPO / "packages" / "coding-agent" / "src" / "index.ts")
        self.assertIn("packages/coding-agent/src/escape.ts", invalid_source_entries(self.workspace))

    def test_hidden_evaluator_sandbox_allows_workspace_and_denies_user_home(self) -> None:
        output = self.root / "evaluation-output"
        output.mkdir()
        settings = evaluator_sandbox_settings(self.workspace, output)
        allowed = run_command(
            "node --version >/dev/null && test -r package.json",
            self.workspace,
            20,
            settings,
        )
        self.assertEqual(allowed["exit_code"], 0, allowed["stderr"])

        blocked = run_command(
            f"cat {PI_REPO / 'package.json'} >/dev/null",
            self.workspace,
            20,
            settings,
        )
        self.assertNotEqual(blocked["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()

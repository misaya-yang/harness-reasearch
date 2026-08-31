"""Offline tests for the actor driver against the fake Pi RPC subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kvc.harness.kvc_run import KvcRunner, RunConfig

GIT_IDENTITY = ("-c", "user.name=KVC Test", "-c", "user.email=kvc-test@invalid")
FAKE_PI = Path(__file__).resolve().parent / "fake_pi.py"


def make_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="kvc-run-"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "main.ts").write_text("export const broken = true;\n", encoding="utf-8")
    subprocess.run(["git", *GIT_IDENTITY, "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *GIT_IDENTITY, "commit", "-q", "-m", "benchmark base"], cwd=root, check=True)
    return root


class FakePiRunner(KvcRunner):
    def _argv(self) -> list[str]:
        return [sys.executable, str(FAKE_PI)]


def make_config(workspace: Path, scenario: str, **overrides) -> RunConfig:
    run_dir = Path(tempfile.mkdtemp(prefix="kvc-rundir-"))
    config = RunConfig(
        workspace=workspace,
        run_dir=run_dir,
        task_prompt="fix the bug",
        objective_anchor="fix the bug",
        key_env_name="FAKE_KEY",
        key_value="unused-offline",
        extra_env={"FAKE_PI_SCENARIO": scenario},
        resource_poll_seconds=100.0,  # effectively disable sampling noise
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestSettledRun(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = make_workspace()
        self.runner = FakePiRunner(make_config(self.workspace, "settle"))
        self.runner.start()
        self.outcome = self.runner.run_prompt("fix the bug")

    def test_settles_with_epoch_and_validation(self) -> None:
        self.assertEqual(self.outcome.reason, "settled")
        self.assertEqual(self.outcome.epochs, 1)
        self.assertEqual(self.outcome.validations, 1)
        self.assertEqual(self.runner.gps.mutation_epoch, 1)
        self.assertEqual(self.runner.gps.incumbent_validated_epoch, 1)

    def test_incumbent_tagged_and_not_rescued_on_settle(self) -> None:
        latest = self.runner.incumbent.latest()
        assert latest is not None
        self.assertEqual(latest[0], "kvc/incumbent-1")
        self.assertIsNone(self.outcome.rescued)

    def test_epoch_file_tracks_mutation_epoch(self) -> None:
        epoch_file = self.runner.config.run_dir / "state" / "epoch.txt"
        self.assertEqual(epoch_file.read_text(encoding="utf-8").strip(), "1")

    def test_manifest_written(self) -> None:
        manifest = json.loads(
            (self.runner.config.run_dir / "run-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema"], "kvc-run-manifest/1")
        self.assertEqual(manifest["outcome"]["reason"], "settled")
        self.assertEqual(manifest["outcome"]["mutation_epochs"], 1)
        self.assertEqual(manifest["gps_final"]["incumbent_validated_epoch"], 1)
        self.assertIsNotNone(manifest["outcome"]["session_stats"])

    def test_events_contain_kvc_markers(self) -> None:
        types = [event.get("type") for event in read_events(self.runner.config.run_dir)]
        self.assertIn("kvc_epoch", types)
        self.assertIn("kvc_validation", types)
        self.assertIn("kvc_incumbent_saved", types)
        self.assertIn("agent_settled", types)


class TestBudgetCutoff(unittest.TestCase):
    def test_budget_kill_and_rescue(self) -> None:
        workspace = make_workspace()
        runner = FakePiRunner(
            make_config(workspace, "hang_after_pass", budget_seconds=2.0, abort_grace_seconds=1.0)
        )
        runner.start()
        outcome = runner.run_prompt("fix the bug")
        self.assertEqual(outcome.reason, "budget")
        self.assertEqual(runner.gps.incumbent_validated_epoch, 1)
        # Cutoff with a validated incumbent rescues the workspace to it.
        self.assertIsNotNone(outcome.rescued)
        assert outcome.rescued is not None
        self.assertEqual(outcome.rescued.rescued_tag, "kvc/incumbent-1")
        manifest = json.loads(
            (runner.config.run_dir / "run-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["outcome"]["reason"], "budget")
        self.assertIsNotNone(manifest["outcome"]["rescued_to"])


if __name__ == "__main__":
    unittest.main()

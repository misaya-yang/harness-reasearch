#!/usr/bin/env python3
"""Calibration-only fixture repair for the frozen R4/R5 behavioral regrade.

The v1 scorer stopped before scoring any Agent patch because the thinking fixture
assigned through getter-only production properties. This wrapper changes only that
fixture, verifies the frozen v1 manifest and v2 overlay, and reuses the v1 scorer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import regrade as base


HERE = Path(__file__).resolve().parent
ORIGINAL_MANIFEST = HERE / "manifest.json"
OVERLAY = HERE / "v2_overlay.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_v2_manifest() -> dict[str, object]:
    overlay = json.loads(OVERLAY.read_text(encoding="utf-8"))
    if sha256_file(ORIGINAL_MANIFEST) != overlay["base_manifest_sha256"]:
        raise RuntimeError("v1 behavioral manifest drift")
    test_path = HERE / "tests" / overlay["thinking_test"]
    if sha256_file(test_path) != overlay["thinking_test_sha256"]:
        raise RuntimeError("v2 thinking fixture drift")
    manifest = json.loads(ORIGINAL_MANIFEST.read_text(encoding="utf-8"))
    task = manifest["tasks"]["pi-thinking-toggle-preserves-bash-output"]
    task["tests"] = [overlay["thinking_test"]]
    task["test_sha256"] = {overlay["thinking_test"]: overlay["thinking_test_sha256"]}
    return manifest


base.MANIFEST_PATH = OVERLAY
base.load_manifest = load_v2_manifest

if __name__ == "__main__":
    raise SystemExit(base.main())

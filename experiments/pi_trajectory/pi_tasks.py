"""Materialize and evaluate lightweight historical Pi repository repair tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
TASKS_PATH = HERE / "tasks" / "pi_coding_tasks_v3.jsonl"
PI_REPO = Path("/Users/yang/projects/opensource-harness/pi")
LEGACY_MODULES = HERE / "legacy_deps" / "node_modules"
NEUTRAL_CACHE_ROOT = Path("/Users/Shared/pi-peac-experiment")


def _dependency_id() -> str:
    digest = hashlib.sha256()
    for path in (PI_REPO / "package-lock.json", PI_REPO / "node_modules" / ".package-lock.json"):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


NEUTRAL_DEPENDENCY_ROOT = NEUTRAL_CACHE_ROOT / "dependencies" / _dependency_id()
NEUTRAL_MODULES = NEUTRAL_DEPENDENCY_ROOT / "node_modules"
NEUTRAL_DEPENDENCY_MANIFEST = NEUTRAL_DEPENDENCY_ROOT / "manifest.json"
NEUTRAL_RUNTIME_ROOT = NEUTRAL_CACHE_ROOT / "runtime"
NEUTRAL_NODE = NEUTRAL_RUNTIME_ROOT / "node"
NEUTRAL_NODE_MANIFEST = NEUTRAL_RUNTIME_ROOT / "node-manifest.json"
NEUTRAL_RG = NEUTRAL_RUNTIME_ROOT / "rg"
NEUTRAL_RG_MANIFEST = NEUTRAL_RUNTIME_ROOT / "rg-manifest.json"
SRT_CLI = PI_REPO / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist" / "cli.js"
_NEUTRAL_NODE_VERIFIED = False
_NEUTRAL_RG_VERIFIED = False
_NEUTRAL_DEPENDENCIES_VERIFIED = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_loader_dylibs(source: Path, runtime_root: Path) -> None:
    """Clone @loader_path-resolved dylibs beside a relocated binary.

    Brew builds of node link @rpath/libnode.<abi>.dylib, resolved at the
    source location via LC_RPATH (@loader_path, @loader_path/../lib). A flat
    clone outside the cellar loses those search paths, so the referenced
    libraries are cloned beside the binary, where @loader_path finds them.
    """
    try:
        deps = subprocess.check_output(["otool", "-L", str(source)], text=True)
        load_commands = subprocess.check_output(["otool", "-l", str(source)], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    # LC_RPATH entries are the @rpath search dirs, typically @loader_path and
    # @loader_path/../lib; resolve them against the source's location.
    search_dirs: list[Path] = []
    for match in re.finditer(r"cmd LC_RPATH\n.*?path (.+?) \(", load_commands, re.DOTALL):
        search_dirs.append(Path(match.group(1).replace("@loader_path", str(source.parent))))
    for line in deps.splitlines()[1:]:
        dep = line.split("(")[0].strip()
        if not dep.startswith("@rpath/"):
            continue
        relative = dep[len("@rpath/"):]
        resolved: Path | None = None
        for base in search_dirs:
            candidate = base / relative
            if candidate.is_file():
                resolved = candidate
                break
        if resolved is None:
            continue
        target = runtime_root / resolved.name
        if target.exists() and _sha256_file(target) == _sha256_file(resolved):
            continue
        staging = runtime_root / f".{resolved.name}-{os.getpid()}"
        subprocess.run(["cp", "-c", str(resolved), str(staging)], check=True)
        staging.chmod(resolved.stat().st_mode)
        staging.rename(target)


def stage_neutral_node() -> Path:
    """Clone the Node executable outside the denied user home for sandboxed children."""
    global _NEUTRAL_NODE_VERIFIED
    if _NEUTRAL_NODE_VERIFIED:
        return NEUTRAL_NODE
    source = Path(shutil.which("node") or "").resolve()
    if not source.is_file():
        raise RuntimeError("node executable was not found")
    source_sha256 = _sha256_file(source)
    if NEUTRAL_NODE.exists() and NEUTRAL_NODE_MANIFEST.exists():
        manifest = json.loads(NEUTRAL_NODE_MANIFEST.read_text(encoding="utf-8"))
        if (
            manifest.get("source_sha256") != source_sha256
            or _sha256_file(NEUTRAL_NODE) != manifest.get("target_sha256")
        ):
            raise RuntimeError("neutral Node runtime integrity mismatch")
        _stage_loader_dylibs(source, NEUTRAL_RUNTIME_ROOT)
        _NEUTRAL_NODE_VERIFIED = True
        return NEUTRAL_NODE
    NEUTRAL_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    staging = NEUTRAL_RUNTIME_ROOT / f".node-{os.getpid()}"
    subprocess.run(["cp", "-c", str(source), str(staging)], check=True)
    staging.chmod(source.stat().st_mode)
    target_sha256 = _sha256_file(staging)
    if target_sha256 != source_sha256:
        raise RuntimeError("neutral Node runtime clone mismatch")
    staging.rename(NEUTRAL_NODE)
    NEUTRAL_NODE_MANIFEST.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": source_sha256,
                "target_sha256": target_sha256,
                "copy_mode": "macOS cp -c clone",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _stage_loader_dylibs(source, NEUTRAL_RUNTIME_ROOT)
    _NEUTRAL_NODE_VERIFIED = True
    return NEUTRAL_NODE


def stage_neutral_rg() -> Path:
    """Clone ripgrep beside Node so sandbox dependency checks avoid the denied home."""
    global _NEUTRAL_RG_VERIFIED
    if _NEUTRAL_RG_VERIFIED:
        return NEUTRAL_RG
    source = Path(shutil.which("rg") or "").resolve()
    if not source.is_file():
        raise RuntimeError("ripgrep executable was not found")
    source_sha256 = _sha256_file(source)
    if NEUTRAL_RG.exists() and NEUTRAL_RG_MANIFEST.exists():
        manifest = json.loads(NEUTRAL_RG_MANIFEST.read_text(encoding="utf-8"))
        if (
            manifest.get("source_sha256") != source_sha256
            or _sha256_file(NEUTRAL_RG) != manifest.get("target_sha256")
        ):
            raise RuntimeError("neutral ripgrep runtime integrity mismatch")
        _NEUTRAL_RG_VERIFIED = True
        return NEUTRAL_RG
    NEUTRAL_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    staging = NEUTRAL_RUNTIME_ROOT / f".rg-{os.getpid()}"
    subprocess.run(["cp", "-c", str(source), str(staging)], check=True)
    staging.chmod(source.stat().st_mode)
    target_sha256 = _sha256_file(staging)
    if target_sha256 != source_sha256:
        raise RuntimeError("neutral ripgrep runtime clone mismatch")
    staging.rename(NEUTRAL_RG)
    NEUTRAL_RG_MANIFEST.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": source_sha256,
                "target_sha256": target_sha256,
                "copy_mode": "macOS cp -c clone",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _NEUTRAL_RG_VERIFIED = True
    return NEUTRAL_RG


def neutral_symlink_closure(root: Path = NEUTRAL_MODULES) -> dict[str, Any]:
    """Verify that every cached dependency symlink resolves within the neutral store."""
    digest = hashlib.sha256()
    count = 0
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in sorted([*subdirectories, *files]):
            candidate = parent / name
            if not candidate.is_symlink():
                continue
            count += 1
            relative = candidate.relative_to(root)
            raw_target = os.readlink(candidate)
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise RuntimeError(f"broken neutral dependency symlink: {relative}: {error}") from error
            if resolved != root and root not in resolved.parents:
                raise RuntimeError(f"external neutral dependency symlink: {relative} -> {resolved}")
            digest.update(str(relative).encode())
            digest.update(b"\0")
            digest.update(raw_target.encode())
            digest.update(b"\0")
            digest.update(str(resolved.relative_to(root)).encode())
            digest.update(b"\0")
    return {"count": count, "sha256": digest.hexdigest()}


def verify_neutral_dependencies() -> dict[str, Any]:
    manifest = json.loads(NEUTRAL_DEPENDENCY_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("dependency_id") != _dependency_id():
        raise RuntimeError("neutral dependency manifest ID mismatch")
    target_lock = NEUTRAL_MODULES / ".package-lock.json"
    target_lock_sha256 = hashlib.sha256(target_lock.read_bytes()).hexdigest()
    if target_lock_sha256 != manifest.get("target_node_modules_lock_sha256"):
        raise RuntimeError("neutral dependency target lock mismatch")
    closure = neutral_symlink_closure()
    if closure != manifest.get("symlink_closure"):
        raise RuntimeError("neutral dependency symlink closure mismatch")
    return closure


def load_tasks() -> dict[str, dict[str, Any]]:
    with TASKS_PATH.open(encoding="utf-8") as handle:
        tasks = [json.loads(line) for line in handle if line.strip()]
    return {str(task["task_id"]): task for task in tasks}


def resolve_commit(repo: Path, revision: str) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", f"{revision}^{{commit}}"], cwd=repo, text=True
    ).strip()


def archive_revision(repo: Path, revision: str, output: Path) -> None:
    output.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", revision], cwd=repo, stdout=subprocess.PIPE
    )
    assert archive.stdout is not None
    with archive.stdout as stream:
        with tarfile.open(fileobj=stream, mode="r|") as bundle:
            bundle.extractall(output, filter="data")
    if archive.wait() != 0:
        raise RuntimeError(f"git archive failed for {revision}")


def _is_live_monorepo_link(relative: Path) -> bool:
    source = PI_REPO / "node_modules" / relative
    if not source.is_symlink():
        return False
    resolved = source.resolve()
    packages_root = PI_REPO / "packages"
    return resolved == packages_root or packages_root in resolved.parents


def stage_neutral_dependencies() -> Path:
    """Clone third-party dependencies once and remove every live source link."""
    global _NEUTRAL_DEPENDENCIES_VERIFIED
    if _NEUTRAL_DEPENDENCIES_VERIFIED:
        return NEUTRAL_MODULES
    if NEUTRAL_DEPENDENCY_MANIFEST.exists() and NEUTRAL_MODULES.exists():
        verify_neutral_dependencies()
        _NEUTRAL_DEPENDENCIES_VERIFIED = True
        return NEUTRAL_MODULES

    parent = NEUTRAL_DEPENDENCY_ROOT.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{NEUTRAL_DEPENDENCY_ROOT.name}-", dir=parent))
    target_modules = staging / "node_modules"
    subprocess.run(
        ["cp", "-cR", str(PI_REPO / "node_modules"), str(target_modules)],
        check=True,
    )
    removed_links: list[str] = []
    for directory, subdirectories, files in os.walk(target_modules, followlinks=False):
        root = Path(directory)
        for name in [*subdirectories, *files]:
            candidate = root / name
            if not candidate.is_symlink():
                continue
            relative = candidate.relative_to(target_modules)
            if _is_live_monorepo_link(relative):
                candidate.unlink()
                removed_links.append(str(relative))
    closure = neutral_symlink_closure(target_modules)
    manifest = {
        "schema_version": 1,
        "dependency_id": _dependency_id(),
        "source_package_lock_sha256": hashlib.sha256(
            (PI_REPO / "package-lock.json").read_bytes()
        ).hexdigest(),
        "source_node_modules_lock_sha256": hashlib.sha256(
            (PI_REPO / "node_modules" / ".package-lock.json").read_bytes()
        ).hexdigest(),
        "target_node_modules_lock_sha256": hashlib.sha256(
            (target_modules / ".package-lock.json").read_bytes()
        ).hexdigest(),
        "removed_live_monorepo_links": sorted(removed_links),
        "symlink_closure": closure,
        "copy_mode": "macOS cp -cR clone",
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    try:
        staging.rename(NEUTRAL_DEPENDENCY_ROOT)
    except FileExistsError:
        if not NEUTRAL_DEPENDENCY_MANIFEST.exists():
            raise
    verify_neutral_dependencies()
    _NEUTRAL_DEPENDENCIES_VERIFIED = True
    return NEUTRAL_MODULES


def _link_dependency(source: Path, target: Path) -> None:
    target.symlink_to(source)


def stage_root_test_config(workspace: Path) -> None:
    """Make root-invoked focused Vitest use base-pinned workspace source aliases."""
    config = workspace / "vitest.config.ts"
    if config.exists():
        return
    config.write_text(
        'export { default } from "./packages/coding-agent/vitest.config.ts";\n',
        encoding="utf-8",
    )


def link_dependencies(workspace: Path) -> None:
    """Stage dependencies without resolving workspace packages to the live checkout."""
    stage_root_test_config(workspace)
    source_modules = stage_neutral_dependencies()
    target_modules = workspace / "node_modules"
    target_modules.mkdir()
    for source in source_modules.iterdir():
        target = target_modules / source.name
        if source.name == "@earendil-works":
            target.mkdir()
            for scoped_source in source.iterdir():
                _link_dependency(scoped_source, target / scoped_source.name)
        else:
            _link_dependency(source, target)
    namespace = target_modules / "@earendil-works"
    namespace.mkdir(exist_ok=True)
    for package_json in sorted(workspace.glob("packages/**/package.json")):
        package = json.loads(package_json.read_text(encoding="utf-8"))
        name = package.get("name")
        if not isinstance(name, str) or not name.startswith("@earendil-works/"):
            continue
        target = namespace / name.split("/", 1)[1]
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to(os.path.relpath(package_json.parent, target.parent))
    if LEGACY_MODULES.exists():
        for source in LEGACY_MODULES.iterdir():
            target = target_modules / source.name
            if not target.exists():
                shutil.copytree(source, target, symlinks=True)
    generated_models = PI_REPO / "packages" / "ai" / "src" / "providers" / "data"
    target_models = workspace / "packages" / "ai" / "src" / "providers" / "data"
    if generated_models.exists() and target_models.parent.exists():
        shutil.copytree(generated_models, target_models)


def initialize_workspace(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Trajectory Benchmark",
            "-c",
            "user.email=benchmark@invalid",
            "commit",
            "-q",
            "-m",
            "benchmark base",
        ],
        cwd=path,
        check=True,
    )


def prepare(task: dict[str, Any], output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    repo = Path(task["source_repo"])
    base_commit = resolve_commit(repo, str(task["base_commit"]))
    archive_revision(repo, base_commit, output)
    stage_root_test_config(output)
    initialize_workspace(output)
    link_dependencies(output)
    return {
        "task_id": task["task_id"],
        "base_commit": base_commit,
        "workspace": str(output.resolve()),
    }


def workspace_patch(
    workspace: Path,
    excluded_paths: list[str] | None = None,
    included_pathspecs: list[str] | None = None,
) -> bytes:
    subprocess.run(["git", "add", "-N", "."], cwd=workspace, check=True)
    root_commit = subprocess.check_output(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=workspace, text=True
    ).splitlines()[0]
    command = [
        "git",
        "diff",
        "--binary",
        root_commit,
        "--",
        *(included_pathspecs or ["."]),
    ]
    command.extend(f":(exclude){path}" for path in excluded_paths or [])
    return subprocess.check_output(command, cwd=workspace)


def invalid_source_entries(workspace: Path) -> list[str]:
    """Reject special files in evaluator-owned source scope."""
    invalid: list[str] = []
    for source_root in sorted(workspace.glob("packages/*/src")):
        for item in source_root.rglob("*"):
            mode = item.lstat().st_mode
            if item.is_symlink() or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                invalid.append(str(item.relative_to(workspace)))
    return invalid


def hidden_test_patch(task: dict[str, Any]) -> bytes:
    repo = Path(task["source_repo"])
    gold = resolve_commit(repo, str(task["gold_commit"]))
    parent = resolve_commit(repo, f"{gold}^")
    return subprocess.check_output(
        ["git", "diff", "--binary", parent, gold, "--", *task["hidden_test_files"]],
        cwd=repo,
    )


def evaluator_sandbox_settings(cwd: Path, output_dir: Path) -> Path:
    isolated_home = cwd / ".evaluator-home"
    isolated_tmp = cwd / ".evaluator-tmp"
    isolated_home.mkdir(exist_ok=True)
    isolated_tmp.mkdir(exist_ok=True)
    settings = {
        "network": {"allowedDomains": [], "deniedDomains": []},
        "filesystem": {
            "denyRead": [
                "/Users/yang",
                str(NEUTRAL_CACHE_ROOT / "runs"),
                "/private/tmp",
                "/tmp",
            ],
            "allowWrite": [str(cwd), str(isolated_home), str(isolated_tmp)],
            "denyWrite": [".env", ".env.*", "**/.git/hooks/**"],
        },
    }
    serialized = json.dumps(settings, indent=2) + "\n"
    settings_path = cwd.parent / ".evaluator-sandbox.json"
    settings_path.write_text(serialized, encoding="utf-8")
    (output_dir / "evaluator-sandbox.json").write_text(serialized, encoding="utf-8")
    return settings_path


def run_command(
    command: str,
    cwd: Path,
    timeout_seconds: int,
    sandbox_settings: Path,
) -> dict[str, Any]:
    env = os.environ.copy()
    for name in list(env):
        upper = name.upper()
        if (
            "TOKEN" in upper
            or "API_KEY" in upper
            or "SECRET" in upper
            or "PASSWORD" in upper
            or upper.startswith("PI_RESEARCH_")
            or upper.startswith("PI_EXPERIMENT_")
            or upper.startswith("PI_PEAC_")
        ):
            env.pop(name, None)
    neutral_node = stage_neutral_node()
    stage_neutral_rg()
    isolated_home = cwd / ".evaluator-home"
    isolated_tmp = cwd / ".evaluator-tmp"
    env.update(
        {
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "GOMAXPROCS": "1",
            "VITEST_MAX_WORKERS": "1",
            "UV_THREADPOOL_SIZE": "2",
            "npm_config_jobs": "1",
            "HOME": str(isolated_home),
            "ZDOTDIR": str(isolated_home),
            "TMPDIR": str(isolated_tmp),
            "CLAUDE_TMPDIR": str(isolated_tmp),
            "PATH": ":".join(
                [
                    str(neutral_node.parent),
                    str(cwd / "node_modules" / ".bin"),
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            ),
        }
    )
    started = time.monotonic()
    process = subprocess.Popen(
        [
            "/usr/bin/nice",
            "-n",
            "15",
            str(neutral_node),
            str(SRT_CLI),
            "--settings",
            str(sandbox_settings),
            "-c",
            command,
        ],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return {
            "command": command,
            "exit_code": process.returncode,
            "timed_out": False,
            "wall_clock_seconds": time.monotonic() - started,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return {
            "command": command,
            "exit_code": None,
            "timed_out": True,
            "wall_clock_seconds": time.monotonic() - started,
            "stdout": stdout,
            "stderr": stderr,
        }


def run_tests(task: dict[str, Any], workspace: Path, output_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    timeout = int(task["evaluation_timeout_seconds"])
    sandbox_settings = evaluator_sandbox_settings(workspace, output_dir)
    for index, command in enumerate(task["test_commands"], start=1):
        result = run_command(command, workspace, timeout, sandbox_settings)
        (output_dir / f"test-{index}.stdout.log").write_text(
            str(result.pop("stdout")), encoding="utf-8"
        )
        (output_dir / f"test-{index}.stderr.log").write_text(
            str(result.pop("stderr")), encoding="utf-8"
        )
        results.append(result)
    return results


def evaluate(task: dict[str, Any], workspace: Path, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    repo = Path(task["source_repo"])
    base_commit = resolve_commit(repo, str(task["base_commit"]))
    with tempfile.TemporaryDirectory(prefix="pi-trajectory-eval-") as temp:
        evaluation_workspace = Path(temp) / "workspace"
        archive_revision(repo, base_commit, evaluation_workspace)
        link_dependencies(evaluation_workspace)
        invalid_entries = invalid_source_entries(workspace)
        agent_patch = (
            b""
            if invalid_entries
            else workspace_patch(
                workspace,
                included_pathspecs=[":(glob)packages/*/src/**"],
            )
        )
        test_patch = hidden_test_patch(task)
        (output_dir / "agent.patch").write_bytes(agent_patch)
        (output_dir / "hidden-tests.patch").write_bytes(test_patch)
        agent_apply = (
            subprocess.run(
                ["git", "apply", "--binary", "-"],
                cwd=evaluation_workspace,
                input=agent_patch,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if agent_patch and not invalid_entries
            else subprocess.CompletedProcess([], 0, b"", b"")
        )
        test_apply = subprocess.run(
            ["git", "apply", "--binary", "-"],
            cwd=evaluation_workspace,
            input=test_patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        results = (
            run_tests(task, evaluation_workspace, output_dir)
            if not invalid_entries
            and agent_apply.returncode == 0
            and test_apply.returncode == 0
            else []
        )
        summary = {
            "schema_version": 1,
            "task_id": task["task_id"],
            "base_commit": base_commit,
            "gold_commit": resolve_commit(repo, str(task["gold_commit"])),
            "agent_patch_applied": agent_apply.returncode == 0,
            "agent_patch_error": (
                f"invalid source entries: {invalid_entries}"
                if invalid_entries
                else agent_apply.stderr.decode(errors="replace")
            ),
            "invalid_source_entries": invalid_entries,
            "hidden_tests_applied": test_apply.returncode == 0,
            "hidden_tests_error": test_apply.stderr.decode(errors="replace"),
            "tests": results,
            "success": not invalid_entries
            and bool(results)
            and all(result["exit_code"] == 0 and not result["timed_out"] for result in results),
        }
        (output_dir / "evaluation.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return summary


def calibrate(task: dict[str, Any], variant: str, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    repo = Path(task["source_repo"])
    revision = str(task["base_commit"] if variant == "base" else task["gold_commit"])
    # Vitest 4.1.9 mis-resolves Node builtins from the exact `pi-calibration-` temp prefix
    # under Seatbelt; the shorter neutral prefix avoids that upstream path-sensitive bug.
    with tempfile.TemporaryDirectory(prefix="pi-calib-") as temp:
        workspace = Path(temp) / "workspace"
        archive_revision(repo, resolve_commit(repo, revision), workspace)
        link_dependencies(workspace)
        hidden_apply_code = 0
        hidden_apply_error = ""
        if variant == "base":
            applied = subprocess.run(
                ["git", "apply", "--binary", "-"],
                cwd=workspace,
                input=hidden_test_patch(task),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            hidden_apply_code = applied.returncode
            hidden_apply_error = applied.stderr.decode(errors="replace")
        results = run_tests(task, workspace, output_dir) if hidden_apply_code == 0 else []
    summary = {
        "schema_version": 1,
        "task_id": task["task_id"],
        "variant": variant,
        "revision": resolve_commit(repo, revision),
        "hidden_tests_applied": hidden_apply_code == 0,
        "hidden_tests_error": hidden_apply_error,
        "tests": results,
        "success": bool(results)
        and all(result["exit_code"] == 0 and not result["timed_out"] for result in results),
    }
    (output_dir / "calibration.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "prepare", "evaluate", "calibrate"))
    parser.add_argument("--task-id")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--variant", choices=("base", "gold"))
    args = parser.parse_args()
    tasks = load_tasks()
    if args.command == "list":
        print(json.dumps(list(tasks.values()), ensure_ascii=False, indent=2))
        return 0
    if not args.task_id or args.task_id not in tasks:
        parser.error("--task-id must name a known task")
    if args.output is None:
        parser.error(f"{args.command} requires --output")
    task = tasks[args.task_id]
    if args.command == "prepare":
        result = prepare(task, args.output.resolve())
    elif args.command == "evaluate":
        if args.workspace is None:
            parser.error("evaluate requires --workspace")
        result = evaluate(task, args.workspace.resolve(), args.output.resolve())
    else:
        if args.variant is None:
            parser.error("calibrate requires --variant")
        result = calibrate(task, args.variant, args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

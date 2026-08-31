"""Offline self-test for the R3 ResourceMonitor fix — NO key, NO provider request.

Synthetic process tables reproduce the exact R2-batch row-3 shape that caused the
EXEC-ABORT (agent-run `npx tsgo` leading its own pgid because Pi's bash tool
setpgid's every command) and assert it is now SANCTIONED, while the same names
outside the trajectory tree remain violations. Pure functions only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_pi_termination_control import MEMORY_PRESSURE_RSS_KB, check_procs, descendant_closure  # noqa: E402

ROOT = 100  # launched pi-test pid


def proc(pid: int, ppid: int, pgid: int, comm: str, args: str, pcpu: float = 1.0, rss_kb: int = 10_000) -> dict:
    return {"pid": pid, "ppid": ppid, "pgid": pgid, "pcpu": pcpu, "rss_kb": rss_kb, "comm": comm, "args": args}


def sanction(procs: list[dict]) -> list[dict]:
    sanctioned = descendant_closure(procs, ROOT)
    return [dict(p, sanctioned=p["pid"] in sanctioned) for p in procs]


def main() -> int:
    failures: list[str] = []

    def expect(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    # Scenario A — R2 row-3 regression: agent's own tsgo inside a setpgid'd bash shell.
    # pi-test(100) -> node pi(101) -> /bin/bash(24889, leads pgid 24889) -> npx(24890) -> tsgo(24891, pgid 24889)
    a = [
        proc(ROOT, 1, ROOT, "pi-test", "pi-test --session …"),
        proc(101, ROOT, ROOT, "node", "node dist/cli.js"),
        proc(24889, 101, 24889, "/bin/bash", "/bin/bash -c cd …/workspace/packages/coding-agent && npx tsgo --noEmit -p ../../tsconfig.json 2>&1 | head -30"),
        proc(24890, 24889, 24889, "node", "node /…/npx tsgo --noEmit"),
        proc(24891, 24890, 24889, "tsgo", "tsgo --noEmit -p ../../tsconfig.json"),
    ]
    expect("A.tsgo_sanctioned", sanction(a)[-1]["sanctioned"], True)
    expect("A.no_failure", check_procs(sanction(a), 50_000, 0, ROOT), (None, 0))

    # Scenario B — same tsgo but an EXTERNAL offline run (ppid = some other shell).
    b = a[:3] + [proc(9999, 5, 9999, "tsgo", "tsgo --noEmit -p tsconfig.json")]
    expect("B.tsgo_unsanctioned", sanction(b)[-1]["sanctioned"], False)
    expect("B.fails", check_procs(sanction(b), 50_000, 0, ROOT)[0], "UNSANCTIONED_HEAVY_WORKER:tsgo")

    # Scenario C — external vitest (offline test leaking into the batch window).
    c = a[:3] + [proc(8888, 6, 8888, "node", "node node_modules/vitest/vitest.mjs run gate.test.ts")]
    expect("C.fails", check_procs(sanction(c), 50_000, 0, ROOT)[0], "UNSANCTIONED_HEAVY_WORKER:vitest")

    # Scenario D — sanctioned agent-run vitest (task work; R2 row-2 shape) does NOT kill.
    d = a[:3] + [proc(7777, 24889, 7777, "node", "node …/vitest run — inside workspace task")]
    expect("D.sanctioned", sanction(d)[-1]["sanctioned"], True)
    expect("D.no_failure", check_procs(sanction(d), 50_000, 0, ROOT), (None, 0))

    # Scenario E — TSGO_CPU_SUSTAINED applies to ANY tsgo (sanctioned or not), 2 consecutive samples.
    a_hot = [dict(p, pcpu=150.0) if p["pid"] == 24891 else p for p in a]  # sanctioned tree, hot
    expect("E.sample1_hits1", check_procs(sanction(a_hot), 50_000, 0, ROOT), (None, 1))
    expect("E.sample2_kills", check_procs(sanction(a_hot), 50_000, 1, ROOT)[0], "TSGO_CPU_SUSTAINED_2samples")
    expect("E.cool_resets", check_procs(sanction(a), 50_000, 1, ROOT), (None, 0))  # not hot this sample → hits 0

    # Scenario F — SECOND_LIVE_TRAJECTORY: verbatim pid inequality, NOT closure-based.
    f = [proc(ROOT, 1, ROOT, "pi-test", "pi-test row1"), proc(555, 9, 555, "pi-test", "pi-test row2-external")]
    expect("F.external", check_procs(sanction(f), 50_000, 0, ROOT)[0], "SECOND_LIVE_TRAJECTORY")
    # even a pi-test that is a DESCENDANT (sanctioned) still violates the ≤1-row cap
    g = [proc(ROOT, 1, ROOT, "pi-test", "pi-test row1"), proc(556, ROOT, ROOT, "pi-test", "pi-test nested")]
    expect("G.nested_still_violates", check_procs(sanction(g), 50_000, 0, ROOT)[0], "SECOND_LIVE_TRAJECTORY")

    # Scenario H — memory pressure over the tree, not per-pgid.
    big = a[:3] + [proc(24892, 24889, 24889, "node", "node helper", rss_kb=MEMORY_PRESSURE_RSS_KB + 1)]
    expect("H.memory", check_procs(sanction(big), MEMORY_PRESSURE_RSS_KB + 1, 0, ROOT)[0].startswith("MEMORY_PRESSURE"), True)
    expect("H.below", check_procs(sanction(big), MEMORY_PRESSURE_RSS_KB - 1, 0, ROOT)[0] is None, True)

    # Scenario I — descendant_closure robustness: orphaned pgid-sharing stranger is NOT sanctioned.
    i = [proc(ROOT, 1, ROOT, "pi-test", "pi-test"), proc(4321, 4000, ROOT, "tsgo", "tsgo --watch")]  # same pgid, different parent (old bug's mirror)
    expect("I.stranger_unsanctioned", sanction(i)[-1]["sanctioned"], False)
    expect("I.fails", check_procs(sanction(i), 50_000, 0, ROOT)[0], "UNSANCTIONED_HEAVY_WORKER:tsgo")

    if failures:
        print("MONITOR_SELFTEST_FAIL")
        for line in failures:
            print("  " + line)
        return 1
    print("MONITOR_SELFTEST_OK — 9 scenarios / R2 row-3 regression covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Offline Evidence-Bounded Commit Protocol

This directory is a provider-free Python 3.11 reference state machine for the
action/commit protocol proposed in report 17. It does not execute tests, read a
workspace, call a verifier, or infer whether a test is valid. A caller records
those observations explicitly.

The state machine tracks:

- an opaque `workspace_revision` (for example, `r1` and `r2`);
- validation scope, revision, pass/fail result, and `TestProvenance`;
- unresolved failures, cleared only by an authoritative pass for that scope;
- completion proposals that snapshot the current revision and evidence IDs;
- a finite action budget and terminal status;
- typed `CommitAccepted` or `CommitRejected` results.

An authoritative pass must have `source="repository"` or `source="external"`
and no `modified_test_paths`. A mutation advances the workspace revision, so an
older pass is no longer current. A proposal made before that mutation receives
`WORKSPACE_REVISION_MISMATCH`; a new proposal that still points at the old pass
receives `STALE_VALIDATION`. Passes over modified tests remain auditable but are
rejected with `MODIFIED_TESTS`.

Run the deterministic tests from the repository root:

```bash
python3.11 -m unittest discover -s experiments/pi_commit_protocol/prototype -t . -v
```

The tests cover the clean commit path, stale evidence, modified tests,
unresolved failures, revision mismatch, double commit, and budget terminal
behavior.

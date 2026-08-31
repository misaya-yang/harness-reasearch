# Incident: neutral dependency cache corrupted by actor npm activity (2026-08-31)

## Symptom

R5 batch `r5-rescue` (8 jobs) failed in ~3 s each at workspace preparation:
`neutral_symlink_closure` (pi_tasks.py) raised FileNotFoundError resolving
`kvc/.cache/dependencies/89e5f42ec7363fa2874d/node_modules/@anthropic-ai/sdk`
— the package directory was gone from the shared cache.

## Root cause

`link_dependencies()` symlinks each workspace's `node_modules/<pkg>` entries
DIRECTLY into the shared neutral dependency cache. Any actor npm/mv/rm
operation that touches node_modules therefore writes through the symlink
into the shared store. Forensics (partial): cache entry mtimes cluster at
10:29 (batch 102341 — the two runs later classified gold-tainted for
filesystem wandering), with an npm-style interrupted-mutation signature:
`sdk` gone, empty `sandbox-runtime` created, five `.bin` launcher links
dangling. The exact timing of the fatal removal is not fully reconstructable
(later prepares succeeded, and child actors reach the same store through
their own symlinked node_modules until ~14:4x); the operative defect is
unambiguous: the shared store was writable by actors at all times.

## Repair (2026-08-31 ~14:55)

1. Corrupt store preserved as
   `kvc/.cache/dependencies/89e5f42ec7363fa2874d.corrupt-20260831` (delete
   after R5 verifies clean).
2. pi checkout `node_modules` verified intact (read-only discipline held).
3. Cache restaged via `pi_bridge.retarget().stage_neutral_dependencies()`
   (fresh clonefile copy from the pi checkout; manifest + 30-link closure
   re-verified).
4. **Guard**: `chmod -R a-w` over the restaged cache tree. Actor npm writes
   into dependency packages now fail loudly; replacing a symlink with a real
   dir inside the WORKSPACE's own node_modules still works and leaves the
   shared store untouched. Future restage: `mv` the old store aside (rename
   needs parent write only, unaffected), restage, chmod again.

## Consequences for claims

- No completed run's workspace content is suspect from this alone: actor
  workspaces read dependencies; the corruption changed availability, not
  test semantics. Every run is leak-audited regardless.
- R5 relaunched as batch `r5-rescue-2` after a successful prepare smoke.
- Same-class risk remains for anything else shared and writable (none
  identified: snapshots are per-spec clones; source mirror is git-managed
  and not written by actors).

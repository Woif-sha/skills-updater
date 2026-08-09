# Repair/Review Scope And Registry Invariants

Read this only when repairing or reviewing the updater. [SKILL.md](../SKILL.md#canonical-safety-boundaries) owns the canonical cross-route safety boundaries; this file records their implementation consequences for registry and payload code.

## Ownership

- `~/.agents/skills` is the only installed-Skill source of truth.
- `.skills-list.json` is generated state. Change Skill folders or `.openskills.json`, then run the registry script; do not hand-maintain registry entries.
- A root with `SKILL.md` is a `single-skill`. A root Git worktree containing `skills/` is a `skill-pack`.
- Hidden directories and directories without a valid Skill contract are not registry entries.

## Boundary Enforcement

- One shared payload classifier drives signatures, merge inputs, transaction evidence, copies, validation, deletion, and rollback.
- Validate portable child names and filesystem object types before material enters a Transaction; a later stage must not reinterpret the boundary.

## Provenance State Reduction

- A non-Git folder with missing provenance remains unmanaged and `unknown_version`; it is not silently treated as a remote Skill.
- A remotely managed root Git worktree or skill pack without complete canonical provenance is `error`, because its origin and update branch must never be inferred.
- Derive management and mode from validated on-disk metadata once, then pass that result forward instead of reconstructing identity in callers.

## Registry Contract

- `managed` and `updateMode` are derived from validated on-disk state.
- The local-only reduction is `managed: false`, `updateMode: "local-only"`, and `remoteVersion: null`.
- A present registry must use the current schema and contain an `entries` object; malformed or obsolete files fail visibly.
- Registry writes are locked and atomic. Reports must come from the refreshed registry, not a stale in-memory copy.

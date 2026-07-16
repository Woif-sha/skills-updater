# Scope And Registry Rules

These constraints apply to every route.

## Ownership

- `~/.agents/skills` is the only installed-Skill source of truth.
- `.skills-list.json` is generated state. Change Skill folders or `.openskills.json`, then run the registry script; do not hand-maintain registry entries.
- A root with `SKILL.md` is a `single-skill`. A root Git worktree containing `skills/` is a `skill-pack`.
- Hidden directories and directories without a valid Skill contract are not registry entries.

## Payload Boundary

- `.git` and `.openskills.json` are control-plane entries.
- Signatures, merge inputs, backups, copies, validation, deletion, and rollback all use the same payload boundary.
- Symlinks, junctions, path traversal, and case-colliding payload names are rejected.

## Provenance

- Remotely managed entries require explicit `source`, `sourceType`, `repoUrl`, `subpath`, and the version fields required by their mode.
- Do not infer a repository, branch, source type, or installed base from a directory name or a hard-coded source table.
- A non-Git folder with missing provenance remains unmanaged and `unknown_version`; it is not silently treated as a remote Skill.
- A remotely managed root Git worktree or skill pack without complete canonical provenance is `error`, because its origin and update branch must never be inferred.
- A self-authored or intentionally frozen Skill must declare `updatePolicy: "local-only"` in `.openskills.json`.

## Registry Contract

- `managed` and `updateMode` are derived from validated on-disk state.
- `local-only` is a first-class status with `managed: false` and `remoteVersion: null`.
- A present registry must use the current schema and contain an `entries` object; malformed or obsolete files fail visibly.
- Registry writes are locked and atomic. Reports must come from the refreshed registry, not a stale in-memory copy.

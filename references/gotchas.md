# Known Gotchas

These are the failure modes most likely to cause wrong behavior.

## Exit Codes

- `check_updates.py` exits with `1` when updates are available.
- `check_updates.py` also exits with `1` when the requested skill is missing.
- Treat the output and scope together before deciding whether the script actually failed.

## Self-Update Guard

- `skills-updater` is intentionally detected as a managed source but forced to `autoUpdate: false`.
- Batch update runs must report the self-update guard instead of overwriting the local customized copy.

## Skill-Pack Detection

- `superpowers` must remain a single `skill-pack` registry entry.
- Do not register each child skill inside that repo as its own top-level managed entry.
- Skill-pack updates are git pulls, not staged folder replacements.

## Local Skill Edits During Updates

- Do not treat the backup as permission to overwrite local customizations. Backups are recovery material; the normal update path must preserve non-conflicting local edits.
- Git-backed `single-skill` updates depend on `sourceCommitSha` in `.openskills.json` to reconstruct the installed base. If that base cannot be fetched, the safe behavior is to block the update rather than discard possible local edits.
- If a user has added local instructions to a skill file, such as extra formatting rules in `SKILL.md`, and the remote skill also updates that file, the desired result is a merged file containing both non-conflicting changes.
- When the same lines are changed locally and remotely, report the merge conflict and point to `<backup-root>/<skill>.merge-conflicts/`; do not silently choose either side.

## OpenSpec

- OpenSpec skills use generated content and compare upstream package versions, not repo commit SHAs.
- Treat them as `sourceType: "git-generated"` with `workflowId` and generator metadata.

## Registry Assumptions

- `check_updates.py`, `update_agent_skills.py`, and `install_agent_skill.py` already refresh registry state as part of their flow.
- Running `sync_skills_registry.py` before every command is redundant and can hide the real task boundary.
- Unmanaged or local-only skills can legitimately stay at `unknown_version`.

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

## OpenSpec

- OpenSpec skills use generated content and compare upstream package versions, not repo commit SHAs.
- Treat them as `sourceType: "git-generated"` with `workflowId` and generator metadata.

## Registry Assumptions

- `check_updates.py`, `update_agent_skills.py`, and `install_agent_skill.py` already refresh registry state as part of their flow.
- Running `sync_skills_registry.py` before every command is redundant and can hide the real task boundary.
- Unmanaged or local-only skills can legitimately stay at `unknown_version`.

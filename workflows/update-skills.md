# Update Skills

Use this workflow for "update all skills", "update skill X", or after bare `skills-updater` found updates.

## Commands

Run one of:

```bash
python scripts/update_agent_skills.py
python scripts/update_agent_skills.py --skill <name>
python scripts/update_agent_skills.py --check-only
python scripts/update_agent_skills.py --json
```

## Steps

1. Scope the command to the user's request.
2. Let the script sync the registry and probe remote versions.
3. Interpret update behavior by entry type:
   - `single-skill`: staged replacement if content changed
   - `skill-pack`: `git pull --ff-only` when the remote commit differs
   - OpenSpec `git-generated`: regenerate from upstream when the package version differs
4. Watch for `autoUpdate: false` entries and report them as intentionally skipped.
5. After completion, report:
   - updated entries
   - skipped entries
   - errors
   - backup path if one was created

## Important Behaviors

- `--check-only` probes without applying changes.
- `skill-pack` updates do not create the same staged backup flow used for single-skill replacements.
- When a single skill has `unknown_version` but a remote version can be recovered, the updater may refresh metadata without replacing files.
- The script syncs the registry again after updates so the final registry reflects the new state.

## Completion Checklist

- The user knows exactly which entries changed.
- The backup location is included when relevant.
- Self-update protection for `skills-updater` was preserved.

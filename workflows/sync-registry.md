# Sync Registry

Use this workflow when the user manually added, deleted, or renamed folders under `~/.agents/skills`.

## Command

```bash
python scripts/sync_skills_registry.py
python scripts/sync_skills_registry.py --json
```

## Steps

1. Run the sync command.
2. Rebuild `.skills-list.json` from the current filesystem state.
3. Confirm how many entries were detected.
4. If the user deleted something manually, verify that the stale registry entry is gone.
5. If the user added a folder manually, verify whether it now qualifies as a `single-skill` or `skill-pack`.

## Important Behaviors

- This workflow does not probe remote versions.
- Hidden directories and non-skill folders stay out of the registry.
- Use this workflow only for filesystem reconciliation, not normal update checks.

## Completion Checklist

- The registry now matches the filesystem.
- The user knows whether the new or removed folder was recognized.

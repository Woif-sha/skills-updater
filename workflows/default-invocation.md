# Default Invocation

Use this workflow when the user invokes `skills-updater` with no extra instruction.

## Goal

Treat the invocation as an operational request to check for updates and apply them when needed.

## Steps

1. Run `python scripts/check_updates.py`.
2. Read the result as a status probe, not just a success/failure signal.
3. If the output shows any `update_available` entries, run `python scripts/update_agent_skills.py`.
4. Report both phases:
   - what was up to date
   - what had updates available
   - what was actually changed
   - whether any entry was skipped because `autoUpdate` is disabled

## Notes

- `check_updates.py` already syncs `.skills-list.json` before probing remote versions.
- Do not insert a separate `sync_skills_registry.py` step unless the user also mentioned manual filesystem changes.
- If the check step returns `1`, inspect whether that means "updates available" instead of assuming the script failed.

## Completion Checklist

- The user sees the check result.
- The user sees whether updates were applied.
- Any backup path created by the update step is reported.

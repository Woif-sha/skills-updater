# Sync Registry

Use this route after manual Skill folder or metadata changes.

```powershell
python scripts/sync_skills_registry.py --json
```

1. Run the command once.
2. Confirm the generated registry path and entry count.
3. For a named Skill, inspect its derived `entryType`, `updateMode`, `managed`, `installedBaseVersion`, and `metadataError`.
4. If metadata is invalid, report the exact error and fix the source metadata; do not patch `.skills-list.json`.

This route performs no remote probe.

If the generated registry itself has an unsupported schema, stop and request explicit authorization to remove it before regenerating; do not silently discard its contents.

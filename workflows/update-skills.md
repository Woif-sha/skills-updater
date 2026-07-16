# Update Skills

Use this route only when the user authorizes updating all entries or one named entry.

## Run

```powershell
python scripts/update_agent_skills.py --json
python scripts/update_agent_skills.py --skill <name> --json
python scripts/update_agent_skills.py --skill <name> --check-only --json
```

1. Preserve the user's exact selection.
2. Use `--check-only` only when mutation was not authorized.
3. Run one command and interpret each structured item.
4. Report `action`, `applied`, committed version, errors, and any backup or conflict path.
5. Do not retry an error through another source, branch, or update method.

Expected actions include `none`, `skipped_local`, `metadata_refreshed`, `fast_forwarded`, and `payload_merged`. A local-only item must be `skipped_local` with `applied: false`.

If a journal remains, report its path and stop. Do not delete it or overwrite metadata to make the run appear successful.

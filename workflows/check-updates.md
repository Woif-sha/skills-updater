# Check Updates

Use this route for read-only update status.

## Run

```powershell
python scripts/check_updates.py --json
python scripts/check_updates.py --skill <name> --json
```

1. Match the user's scope exactly.
2. Run the JSON command once; it already refreshes the registry.
3. Report `up_to_date`, `update_available`, `local_only`, `unknown_version`, and `error` separately.
4. Stop after reporting. A check request does not authorize an update.

Exit code `1` means at least one update or error exists, or the selection was empty. Classify it from the JSON payload, never from the exit code alone.

For `local_only`, verify `remote_version` is `null`; no remote probe is permitted.

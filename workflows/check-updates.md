# Check Updates

Use this workflow for "check updates", "what is outdated?", or "is skill X current?" requests.

## Command

Run one of:

```bash
python scripts/check_updates.py
python scripts/check_updates.py --skill <name>
python scripts/check_updates.py --json
```

## Steps

1. Run the command that matches the user's scope.
2. Read the registry-backed status for each returned entry.
3. Classify each entry as `up_to_date`, `update_available`, `unknown_version`, or `error`.
4. Explain ambiguous cases:
   - `unknown_version` means comparison metadata is missing or incomplete
   - `error` means the remote probe failed
5. If the user asked only to check, stop here. Do not auto-update unless they invoked bare `skills-updater`.

## Important Behaviors

- The script syncs `.skills-list.json` before probing and writes `remoteVersion`, `lastStatus`, and `lastCheckedAt` back into the registry.
- The script exits `1` when updates are available, and also exits `1` when no matching entry was found.
- JSON mode is preferable when another tool or script will consume the result.

## Completion Checklist

- The report matches the requested scope.
- Exit code handling did not misclassify "updates available" as a broken script.
- Follow-up action is suggested only if the user asked for it.

# Default Invocation

Bare `skills-updater` means check first, then update only entries reported as updateable.

1. Run `python scripts/check_updates.py --json`.
2. Classify the JSON result; exit code `1` alone is not a diagnosis.
3. If at least one entry is `update_available`, run `python scripts/update_agent_skills.py --json`.
4. If `reverse-engineer-anything` is installed, also follow [maintain-rea.md](maintain-rea.md) with the same check-then-update scope.
5. Report the check result and each applied, skipped, or failed action.

Do not run a separate registry sync, retry failures, or override `local_only`.

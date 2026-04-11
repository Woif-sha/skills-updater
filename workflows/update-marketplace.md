# Update Marketplace

Use this workflow only when the user explicitly asks to maintain a marketplace clone under `~/.claude/plugins/marketplaces`.

## Commands

Run one of:

```bash
python scripts/update_marketplace.py <marketplace_name>
python scripts/update_marketplace.py <marketplace_name> --auto-install
python scripts/update_marketplace.py <marketplace_name> --json
```

## Steps

1. Confirm the marketplace name the user wants to update.
2. Run the updater for that marketplace.
3. Report:
   - local and remote commit
   - how many commits behind the marketplace was
   - affected installed skills
   - whether `--auto-install` queued reinstalls
4. If pending installs are emitted, surface them clearly to the user.

## Important Behaviors

- This workflow operates on `~/.claude/plugins`, not the `~/.agents/skills` registry.
- `--auto-install` appends pending `/install ...@marketplace` commands instead of mutating `~/.agents/skills`.
- Zero commits behind means "no marketplace update", not a script failure.

## Completion Checklist

- The marketplace result is reported separately from agent skill registry state.
- Any queued reinstall commands are surfaced.

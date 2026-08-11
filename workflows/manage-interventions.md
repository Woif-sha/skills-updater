# Manage Intervention Records

Use the dedicated CLI for updater-created records under `~/.agents/interventions`.

1. Run `python scripts/manage_interventions.py --json` first. This inventory is read-only and returns stable artifact IDs, states, retention groups, and diagnostic references.
2. Use exactly one explicit artifact ID for a requested mutation:
   - `--resolve <artifact-id>` or `--abandon <artifact-id>` starts content-conflict retention.
   - `--validate <artifact-id>` settles the referenced Diagnostic Journal; retention starts only after `committed` or `rolled_back` is proven.
   - `--cleanup <artifact-id>` removes an expired whole retention group through its recoverable tombstone.
3. Report the structured result, including any `cleanup_residue`. A residue does not change a proven Installed State.

The CLI has no path, glob, partial, force, or default-all mutation. Legacy `.backup-*` directories are outside inventory and cleanup.

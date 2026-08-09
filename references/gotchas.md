# Repair/Review Gotchas

Read this only when changing or reviewing updater implementation. The canonical boundaries remain in [SKILL.md](../SKILL.md#canonical-safety-boundaries); these are failure patterns that explain why the implementation checks exist.

## Status And Output

- `check_updates.py` exits `1` for `update_available`, `error`, or an empty selection. Inspect JSON status instead of treating every `1` as the same failure.
- `unknown_version` applies to unmanaged non-Git folders. A remotely managed root Git worktree with missing provenance is an error, not an unmanaged fallback.

## Control Data

- `.openskills.json` is commonly Git-ignored but still participates in Committed Update verification as control metadata; treating it as payload corrupts dirty checks and merge inputs.
- Detect a root `.git` directory or gitfile before choosing a payload mode; snapshot replacement over a worktree damages repository state.

## Version Semantics

- `installedBaseVersion` is the incorporated upstream base; Git HEAD is the current local commit. They may differ.
- An ahead worktree is not an update target. A diverged or dirty-behind worktree must remain unchanged.

## Transactions

- Transaction Evidence preserves recovery inputs; it is not permission to overwrite local edits.
- Snapshot conflicts leave the installed payload unchanged and keep `.base`, `.local`, and `.remote` artifacts.
- Metadata publication requires same-volume hard-link support. If unavailable, fail explicitly; do not switch to a weaker copy/replace path.
- A retained `.skill-update-*`, `.git-update-*`, or `.metadata-update-*` journal means recovery was not proven safe. Preserve it and report the error.

## Generated And Packed Skills

- A Git repository with a root `skills/` payload is one `skill-pack`; do not register each child as an independent top-level install.
- OpenSpec entries are `git-generated` and compare the package version at the exact resolved revision.

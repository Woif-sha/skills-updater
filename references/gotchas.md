# Known Gotchas

Read this for default, mutation, or updater repair/review routes.

## Status And Output

- `check_updates.py` exits `1` for `update_available`, `error`, or an empty selection. Inspect JSON status instead of treating every `1` as the same failure.
- `local_only` is distinct from `unknown_version`: the former is an explicit no-network policy; the latter means provenance is insufficient.
- `unknown_version` applies to unmanaged non-Git folders. A remotely managed root Git worktree with missing provenance is an error, not an unmanaged fallback.
- Operational failures must stay structured in JSON mode. A traceback indicates a bug, not an acceptable error response.

## Control Data

- Never recurse into `.git`; Git objects, refs, locks, and index files are not payload.
- `.openskills.json` is mutable control metadata even when Git ignores it. It never participates in payload dirty checks or merges.
- A root `.git` directory or gitfile selects Git worktree mode. Do not route that Skill through snapshot replacement.

## Version Semantics

- `installedBaseVersion` is the incorporated upstream base; Git HEAD is the current local commit. They may differ.
- `sourceCommitSha` is invalid for remote management, not a compatibility alias. It may remain inert under `local-only` because provenance is not interpreted there.
- An ahead worktree is not an update target. A diverged or dirty-behind worktree must remain unchanged.

## Transactions

- Backups exclude control data and are recovery evidence, not permission to overwrite local edits.
- Snapshot conflicts leave the installed payload unchanged and keep `.base`, `.local`, and `.remote` artifacts.
- Metadata publication requires same-volume hard-link support. If unavailable, fail explicitly; do not switch to a weaker copy/replace path.
- A retained `.skill-update-*`, `.git-update-*`, or `.metadata-update-*` journal means recovery was not proven safe. Preserve it and report the error.

## Generated And Packed Skills

- A Git repository with a root `skills/` payload is one `skill-pack`; do not register each child as an independent top-level install.
- OpenSpec entries are `git-generated` and compare the package version at the exact resolved revision.

# Update Policies

Read this file for check, update, install, local-only, or updater repair/review routes.

## No Fallback

- Snapshot updates resolve the exact configured GitHub repository HEAD; Git worktrees fetch only their explicit upstream branch from the exact metadata repo URL.
- Do not guess `main`, `master`, another source, an older metadata field, or a cached value when required data is missing.
- Remotely managed entries reject `sourceCommitSha`; `installedBaseVersion` is their only installed-base field. Local-only provenance is inert and is not migrated or interpreted.
- Store full 40-character Git SHAs. A validated 12–39 character SHA may only be compared as a prefix of the same commit.
- Any missing, invalid, or contradictory state required for a remote operation returns `error` without mutation.

## Local-Only

- `updatePolicy: "local-only"` is checked from disk inside the Skill lock and again immediately before network or mutation boundaries.
- Local-only entries never resolve a remote branch, fetch, stage, back up, merge, refresh upstream metadata, or update.
- Check returns `status: "local_only"`; update returns `action: "skipped_local"`, `applied: false`, and `remote_version: null`.

## Root Git Worktrees

- Root `.git` selects the dedicated Git transaction path; file-level replacement is forbidden.
- Require `branch.<name>.remote=origin` and an explicit `branch.<name>.merge`; do not infer the remote default branch.
- Clean `equal`: refresh only stale installed-base metadata.
- Clean `behind`: fetch the configured branch and fast-forward transactionally.
- `ahead`: keep local HEAD and refresh metadata only when the remote commit is already incorporated.
- Dirty, detached, mismatched-origin, invalid, or diverged state returns a structured error without changing the worktree.
- `skill-pack` uses this same Git state machine; no raw `git pull` path exists.

## Snapshot Skills

- Stage the exact remote revision before comparing payload signatures.
- Reconstruct the exact `installedBaseVersion`, then merge `base + local + remote`.
- Conflicts leave the installed payload unchanged and write explicit conflict artifacts beside the backup.
- Build and validate the complete result before touching the installed payload.
- Application and metadata publication are journaled transactions. Any apply failure restores the exact original payload; ambiguous metadata recovery retains the journal and fails visibly.

## Generated Skills And Output

- OpenSpec `git-generated` entries require the exact OpenSpec repository, subpath `.`, generator, and `workflowId`; they read `package.json` at the exact resolved revision and regenerate that workflow only.
- JSON mode emits a structured result for argument and operational errors; it must not leak argparse usage or a traceback into stdout.
- A committed update whose cleanup fails reports `applied: true` with its committed action and version.

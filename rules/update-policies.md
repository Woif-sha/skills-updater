# Update Policies

These rules govern how version checks and updates must behave.

## Version-First Policy

- Probe upstream version information first.
- Do not download and diff every skill just to decide whether an update exists.
- Status values are `up_to_date`, `update_available`, `unknown_version`, or `error`.
- `unknown_version` means the entry lacks enough comparable metadata, not that an update definitely exists.

## Update Strategy By Entry Type

- `single-skill`: stage the remote skill, compare content, then apply the update only when the staged copy is different.
- Git-backed `single-skill` updates must preserve local edits. Reconstruct the installed base from `.openskills.json` / `sourceCommitSha`, then three-way merge `base + current local + staged remote`.
- If the local skill contains custom additions such as user-specific `SKILL.md` rules and the remote also changed the same file in a non-conflicting area, keep both changes in the final skill.
- If local and remote changed the same lines or a safe base cannot be reconstructed, do not overwrite the local skill. Leave the current local folder unchanged, keep a backup, write conflict artifacts, and report an `error` status for that entry.
- `skill-pack`: compare remote commit first, then `git pull --ff-only` the whole repo if it is behind.
- `git-generated` OpenSpec skill: compare upstream package version first, then regenerate the skill from upstream when the version changes.

## Backup And Metadata Rules

- Backups are created only when a staged single-skill replacement is actually applied.
- Backup roots live under `~/.agents/skills/.backup-YYYYMMDD-HHMMSS`.
- Merge conflict artifacts live beside the backup as `~/.agents/skills/.backup-YYYYMMDD-HHMMSS/<skill>.merge-conflicts/` and contain `.base`, `.local`, and `.remote` versions of conflicted files when available.
- Refresh `.openskills.json` metadata after installs or applied updates when the script does so.
- For OpenSpec entries, track `generatedByVersion`; for git-backed single skills, track `sourceCommitSha`.

## Special Cases

- `skills-updater` is a locally customized copy and must remain registered with `autoUpdate: false`.
- When `update_agent_skills.py` reaches an entry with `autoUpdate: false`, report that self-update is disabled instead of overwriting it.
- `superpowers` remains one `skill-pack` even when it contains many child skills.
- OpenSpec entries come from `https://github.com/Fission-AI/OpenSpec` with `sourceType: "git-generated"`.

## Reporting Rules

- Distinguish "updates available" from "command failed".
- Report the backup path when an update created one.
- When the user targets a single skill, scope both the command and the report to that entry.
- If a command uses JSON mode, preserve the machine-readable payload instead of paraphrasing it away.

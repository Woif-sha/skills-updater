# Skills Updater

Install, inspect, transactionally update, and register Skills under `~/.agents/skills`.

[中文](README.md)

## Features

- Install a single Skill, a Skill Pack, or an OpenSpec-generated Skill from GitHub.
- Check every installed Skill or one named Skill for updates.
- Distinguish ordinary folders from Skills whose root is itself a Git worktree.
- Preserve local changes through a three-way merge or a transactional Git fast-forward.
- Permanently disable remote probing for self-authored Skills with `updatePolicy: "local-only"`.
- Keep automation output machine-readable with structured JSON.

## Requirements

- Python 3.10 or newer.
- Git.
- Network access to the GitHub repositories being installed or updated.
- Node.js and npm only when installing or updating an OpenSpec-generated Skill.

The runtime uses only the Python standard library; no `pip install` step is required.

## Install Skills Updater

The recommended layout is a clone under the canonical Skill directory with explicit metadata for the root Git worktree. The following PowerShell commands are for a new installation:

```powershell
$skillDir = Join-Path $HOME ".agents\skills\skills-updater"
New-Item -ItemType Directory -Force (Split-Path $skillDir) | Out-Null
git clone https://github.com/Woif-sha/skills-updater.git $skillDir
Set-Location $skillDir

$sha = git rev-parse HEAD
@'
import json
import sys
from pathlib import Path

metadata = {
    "source": "Woif-sha/skills-updater",
    "sourceType": "git",
    "repoUrl": "https://github.com/Woif-sha/skills-updater",
    "subpath": ".",
    "installedBaseVersion": sys.argv[1],
}
Path(".openskills.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
'@ | python - $sha

python scripts/sync_skills_registry.py --json
```

`.openskills.json` is local control data and is ignored by Git. Do not clone again over an existing installation. Enter the existing directory, verify its metadata and Git upstream, and sync the registry instead.

## Quick Start

The commands can be called from any directory:

```powershell
$updater = Join-Path $HOME ".agents\skills\skills-updater\scripts"

# Rebuild the local registry
python "$updater\sync_skills_registry.py" --json

# Check every Skill or one named Skill
python "$updater\check_updates.py" --json
python "$updater\check_updates.py" --skill zotero-paper-updater --json

# Run the update probe without applying changes
python "$updater\update_agent_skills.py" --check-only --json

# Update every Skill or one named Skill
python "$updater\update_agent_skills.py" --json
python "$updater\update_agent_skills.py" --skill zotero-paper-updater --json
```

Check and update commands accept `--lang zh` or `--lang en` for human-readable output. JSON field names remain stable.

Every entry point treats `~/.agents/skills` as the canonical installation source and maintains `~/.agents/skills/.skills-list.json`. Do not edit the registry by hand; sync it after changing a Skill directory or `.openskills.json`.

### Install From GitHub

```powershell
# The repository root is one Skill
python "$updater\install_agent_skill.py" `
  --repo owner/repo --path . --name my-skill --json

# The Skill is in a repository subdirectory
python "$updater\install_agent_skill.py" `
  --repo owner/repo --path skills/my-skill --name my-skill --json

# The root repository is a Skill Pack containing skills/
python "$updater\install_agent_skill.py" `
  --repo owner/repo --type skill-pack --path . --json

# Generate one OpenSpec workflow Skill
python "$updater\install_agent_skill.py" `
  --repo Fission-AI/OpenSpec --source-type git-generated `
  --workflow-id explore --json
```

An existing destination, a non-GitHub source, an escaping path, or an incomplete source contract fails explicitly. The installer never overwrites the destination or guesses an alternative source.

## Self-Authored Skills: Disable Remote Updates

Declare the policy in the Skill root `.openskills.json`:

```json
{
  "source": "my-skill",
  "sourceType": "local",
  "updatePolicy": "local-only"
}
```

Existing Git provenance may remain; add only this field:

```json
{
  "updatePolicy": "local-only"
}
```

Keep the existing fields in the real file instead of replacing the whole file with the one-field example. The policy is reread inside the lock and before network, backup, and mutation boundaries:

- checks return `status: "local_only"`;
- updates return `action: "skipped_local"` and `applied: false`;
- `remote_version` remains `null`;
- no branch resolution, fetch, staging, backup, or update is attempted.

## Remote-Managed Metadata

A remotely managed entry requires an explicit, internally consistent source contract. An ordinary Git single-skill uses its actual repository-relative path as `subpath`:

```json
{
  "source": "owner/repo",
  "sourceType": "git",
  "repoUrl": "https://github.com/owner/repo",
  "subpath": "skills/my-skill",
  "installedBaseVersion": "the full 40-character Git commit SHA"
}
```

A root Skill Pack must use `sourceType: "git-pack"`:

```json
{
  "source": "owner/repo",
  "sourceType": "git-pack",
  "repoUrl": "https://github.com/owner/repo",
  "subpath": ".",
  "installedBaseVersion": "the full 40-character Git commit SHA"
}
```

An OpenSpec-generated Skill identifies its generator and workflow; its version comes from the generated `SKILL.md`:

```json
{
  "source": "Fission-AI/OpenSpec",
  "sourceType": "git-generated",
  "repoUrl": "https://github.com/Fission-AI/OpenSpec",
  "subpath": ".",
  "generator": "dist/core/shared/skill-generation.js",
  "workflowId": "explore"
}
```

- `installedBaseVersion` is the upstream base incorporated into the installed payload.
- Use `subpath: "."` when the repository root is the single-skill; a root Git worktree must use `.`.
- A root Git worktree gets its current version from local `HEAD` and requires an explicit `origin` upstream for the current branch.
- `sourceCommitSha` is no longer supported and is never used as a compatibility fallback.
- A non-Git folder without provenance stays `unmanaged / unknown_version`; it is never mapped to a guessed remote.

## Update Modes

| Local shape | Update path | Safety contract |
| --- | --- | --- |
| Root contains `.git` | Git worktree transaction | Requires an explicit upstream; clean behind branches may fast-forward; dirty, detached, or diverged states stop |
| Ordinary Skill folder | Snapshot three-way merge | Merges `base + local + remote` from the exact installed base; conflicts never overwrite local payload |
| Root repository contains `skills/` | Skill Pack Git transaction | Registers the repository once instead of splitting its child Skills |
| OpenSpec-generated Skill | Regenerate at an exact revision | Accepts only the configured repository, generator, and `workflowId` |

`.git` and `.openskills.json` are always control data and never enter signatures, merges, backups, copies, or deletion. Payload, Git, and metadata mutations use durable journals. Failures roll back; ambiguous recovery retains evidence and returns `error`.

## JSON Statuses And Exit Codes

Common `status` values:

- `up_to_date`: the installed Skill already incorporates the remote version.
- `update_available`: an applicable update exists.
- `local_only`: remote access is explicitly disabled.
- `unknown_version`: the local directory has insufficient provenance.
- `error`: state is unsafe, provenance conflicts, or an operation failed.

Common `action` values are `none`, `payload_merged`, `fast_forwarded`, `metadata_refreshed`, and `skipped_local`.

- `check_updates.py` exits `0` when no update or error exists, and `1` for an available update, an error, or an empty selection.
- Update, install, and sync commands exit `0` on success and `1` on operational failure.
- Argument errors exit `2`. With `--json`, stdout remains valid JSON and never includes argparse usage or a traceback.

## Project Layout

```text
SKILL.md          # compact entry point
routing.yaml      # canonical task-routing manifest
rules/            # stable invariants
workflows/        # intent-specific procedures
references/       # gotchas and code index
scripts/          # runtime implementation
tests/            # regression contract, never loaded at Skill runtime
```

[SKILL.md](SKILL.md) routes operational detail into `rules/`, `workflows/`, and `references/`. README contains the complete user-facing guide and is not part of the always-read route.

## Development Validation

```powershell
python -m unittest discover -s tests
python -m compileall -q scripts tests
git diff --check
```

`tests/` must remain version-controlled to prevent regressions involving `.git` deletion, partial updates, concurrent metadata loss, ZIP path escape, and local-only network access. Only caches, coverage data, and temporary artifacts are ignored.

## License

MIT

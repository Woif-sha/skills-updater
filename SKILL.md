---
name: skills-updater
description: Manage skills stored in ~/.agents/skills. Check versions from upstream first, update only changed skills, maintain ~/.agents/skills/.skills-list.json, install new skills, and resync the registry when skills are added or deleted manually.
---

# Skills Updater

Manage the unified skill store at `~/.agents/skills`.

This skill does not manage `~/.claude/skills`, `~/.codex/skills`, or `~/.cursor/skills` directly. Those directories may be symlinked by the user, but updater logic treats `~/.agents/skills` as the only source of truth.

## Source of Truth

- Installed skills live in `~/.agents/skills/`
- The registry file is `~/.agents/skills/.skills-list.json`
- Managed single-skill installs keep source metadata in `.openskills.json`
- Manual deletions or additions must be reflected by rescanning the folder and rewriting the registry

## Entry Types

### Single skill

Normal skill folders that contain `SKILL.md`.

Registry fields include:
- `repoUrl`
- `sourceType`
- `subpath`
- `localVersion`
- `remoteVersion`
- `autoUpdate`

### Skill pack

Whole repositories that contain multiple bundled skills.

Current special case:
- `superpowers` is tracked as one `skill-pack`
- Do not register every child skill separately
- When upstream changes, update the whole cloned repo

## Special Handling

### OpenSpec

OpenSpec skills come from `https://github.com/Fission-AI/OpenSpec` and use `sourceType: "git-generated"`.

Update behavior:
1. Read the remote package version first.
2. If the version matches the registry, do nothing.
3. If the version changed, clone OpenSpec, build it, generate the workflow skill, and replace the local skill.

Do not treat OpenSpec skills as static copies.

### skills-updater

This local copy is customized. It must stay registered, but it must not overwrite itself from upstream.

Rules:
- keep `autoUpdate: false` for `skills-updater`
- report that self-update is disabled
- do not remove local custom scripts during batch updates

## Required Workflow

### Default invocation

If the user invokes `skills-updater` without any additional instruction, treat that as:

1. Sync `~/.agents/skills/.skills-list.json`
2. Run `python scripts/check_updates.py`
3. If any managed entry shows `update_available`, run `python scripts/update_agent_skills.py`
4. Report both the check result and any applied updates

This default behavior is operational. It is not a request to explain how the skill works.

### Check updates

Run:

```bash
python scripts/check_updates.py
```

Behavior:
1. Sync `~/.agents/skills/.skills-list.json`
2. Read local versions from the registry and local metadata
3. Fetch remote versions from upstream
4. Mark entries as `up_to_date`, `update_available`, `unknown_version`, or `error`

The updater must compare versions first. It must not download and diff every skill just to decide whether an update exists.

### Update skills

Run:

```bash
python scripts/update_agent_skills.py
python scripts/update_agent_skills.py --skill <name>
```

Behavior:
1. Sync the registry first
2. Fetch the remote version for each managed entry
3. Skip entries whose versions already match
4. Update only entries that are different
5. Create a backup under `~/.agents/skills/.backup-YYYYMMDD-HHMMSS` before replacing a local skill

Update rules by type:
- `single-skill`: stage the remote skill, then replace the local folder only if needed
- `skill-pack`: pull the repo directly when the remote commit differs
- `git-generated` OpenSpec skill: regenerate from upstream when the remote package version differs

### Install a new skill

Run:

```bash
python scripts/install_agent_skill.py --repo <owner/repo-or-url> --path <repo-subpath>
```

Examples:

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
python scripts/install_agent_skill.py --repo https://github.com/Fission-AI/OpenSpec --type single-skill --source-type git-generated --name openspec-explore --workflow-id explore
```

Behavior:
1. Install into `~/.agents/skills/<name>`
2. Write `.openskills.json` for managed single skills
3. Rescan and refresh `.skills-list.json`

### Resync registry

Run:

```bash
python scripts/sync_skills_registry.py
```

Use this when the user manually adds or deletes folders in `~/.agents/skills`.

## User Requests

### "Check all skill updates"

Run `python scripts/check_updates.py` and report version status from `.skills-list.json`.

### "skills-updater" with no extra text

Treat this as the default invocation flow: run `python scripts/check_updates.py`, then run `python scripts/update_agent_skills.py` if updates are available, and report the resulting registry state.

### "Update all skills"

Run `python scripts/update_agent_skills.py`, then report which entries changed and any backup path created.

### "Update superpowers"

Treat `superpowers` as a single `skill-pack`. Compare its repo commit first, then pull the repo if needed.

### "Install a new skill from GitHub"

Use `skills-updater` by default. Run `python scripts/install_agent_skill.py ...` into `~/.agents/skills`, then resync the registry.

### "I deleted a skill manually"

Use `skills-updater` by default. Run `python scripts/sync_skills_registry.py` so `.skills-list.json` matches the filesystem again.

## Scripts

- `scripts/agent_skill_updater.py`: shared staging, comparison, backup, and replacement helpers
- `scripts/skills_registry.py`: registry detection and `.skills-list.json` maintenance
- `scripts/check_updates.py`: remote version probe without forced full downloads
- `scripts/update_agent_skills.py`: selective updates based on version differences
- `scripts/install_agent_skill.py`: install new skills into `~/.agents/skills`
- `scripts/sync_skills_registry.py`: rescan local skill folders and rebuild the registry

## Notes

- Keep context focused on `~/.agents/skills`
- Ignore downstream symlink targets managed by the user
- Do not expand `superpowers` into child entries in the registry
- Preserve the special handling for OpenSpec and the self-update guard for `skills-updater`

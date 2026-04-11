# Skills Updater

Manage updates, installs, and registry sync for all skills stored in `~/.agents/skills`.

[中文](README.md)

## Origin

This project is based on:

`https://github.com/yizhiyanhua-ai/skills-updater`

The original project focused more on Claude Code multi-source skill updates and recommendations. This repository has been heavily reworked for a different operating model:

- manage only `~/.agents/skills`
- keep a single registry file: `.skills-list.json`
- compare versions first, then update only changed skills
- support regular single-skill installs, `skill-pack` repos, and OpenSpec-generated skills
- prevent the local customized `skills-updater` from overwriting itself

## Design Goal

`~/.agents/skills` is the single source of truth.

You can symlink `~/.claude/skills`, `~/.codex/skills`, or `~/.cursor/skills` to that folder if you want, but the updater itself only reads and writes the unified directory.

The registry file is always:

```text
~/.agents/skills/.skills-list.json
```

## Core Features

### 1. Unified scanning and registry rebuild

- scans top-level directories under `~/.agents/skills`
- detects regular single skills and `skill-pack` repos
- refreshes source metadata
- rewrites `.skills-list.json`

### 2. Version-first update checks

- reads `localVersion` from the local registry
- fetches `remoteVersion` from upstream
- updates only when versions differ
- avoids downloading every skill just to compare contents

### 3. Selective updates

- update all installed skills
- update one named skill
- create backups before replacement
- sync the registry again after changes are applied

## Default Request Semantics

- if the user invokes `skills-updater` with no extra text, treat it as an operational request: run `python scripts/check_updates.py`, then run `python scripts/update_agent_skills.py` if updates are available, and leave `.skills-list.json` refreshed
- if the user asks to install a skill, use `skills-updater` by default so the install and registry refresh happen together
- if the user says a skill was deleted manually, use `skills-updater` by default to resync `.skills-list.json` with the filesystem

### 4. New skill installation

- installs directly into `~/.agents/skills/<skill-name>`
- refreshes `.skills-list.json` immediately after install
- writes `.openskills.json` for managed single skills
- preserves full repo layout for `skill-pack` installs

### 5. Sync after manual deletes or additions

If a skill folder is deleted or added manually, the sync step rebuilds `.skills-list.json` from the actual filesystem state. Removed folders disappear from the registry and new folders are added.

### 6. Special handling for `superpowers`

- treated as one `skill-pack`
- only the repo-level entry is recorded in the registry
- child skills are not tracked individually
- updates happen at the whole-repo level

### 7. Special handling for OpenSpec

OpenSpec skills are generated, not copied as static folders:

- upstream repo: `https://github.com/Fission-AI/OpenSpec`
- type: `git-generated`
- update checks compare the upstream package version
- when the version changes, the workflow skill is regenerated

### 8. Self-update protection

This repository contains a locally customized `skills-updater`, so its registry entry is forced to:

```json
{
  "autoUpdate": false
}
```

That prevents batch updates from overwriting the customized local copy.

## Repository Layout

```text
skills-updater/
├─ README.md
├─ README.en.md
├─ SKILL.md
├─ references/
│  └─ marketplaces.md
└─ scripts/
   ├─ agent_skill_updater.py
   ├─ check_updates.py
   ├─ i18n.py
   ├─ install_agent_skill.py
   ├─ recommend_skills.py
   ├─ recommendations.json
   ├─ skills_registry.py
   ├─ stdio_utils.py
   ├─ sync_skills_registry.py
   ├─ test_agent_skill_updater.py
   ├─ test_skills_registry.py
   ├─ update_agent_skills.py
   └─ update_marketplace.py
```

## Script Guide

### `scripts/agent_skill_updater.py`

Low-level update utilities.

Responsibilities:

- download GitHub repo archives
- stage remote skill content
- compute directory signatures
- compare local and staged content
- replace local skill content
- create backups
- handle OpenSpec generation
- support repo clone / pull helpers for `skill-pack` workflows

### `scripts/skills_registry.py`

Registry core.

Responsibilities:

- resolve `~/.agents/skills`
- read and write `.skills-list.json`
- detect regular skills and `skill-pack` repos
- infer known upstream sources
- refresh `localVersion`
- force `autoUpdate: false` for `skills-updater`

### `scripts/check_updates.py`

Read-only update inspection entry point.

Responsibilities:

- sync the registry first
- fetch remote versions
- report `up_to_date`, `update_available`, `unknown_version`, or `error`
- support `--skill` and `--json`

### `scripts/update_agent_skills.py`

Main update entry point.

Responsibilities:

- walk the registry entries
- skip skills with `autoUpdate: false`
- update only changed entries
- pull full repos for `skill-pack`
- regenerate OpenSpec skills when needed
- resync the registry after updates

### `scripts/install_agent_skill.py`

New skill installation entry point.

Responsibilities:

- parse GitHub repo locations
- determine install directory names
- enforce installation into `~/.agents/skills/<name>`
- install single skills or full `skill-pack` repos
- write `.openskills.json`
- refresh `.skills-list.json`

### `scripts/sync_skills_registry.py`

Registry rescan entry point.

Responsibilities:

- rescan `~/.agents/skills`
- rebuild `.skills-list.json`
- remove entries whose folders no longer exist
- add entries for newly discovered skills

### `scripts/stdio_utils.py`

Windows stdio helper.

Responsibilities:

- configure UTF-8 stdout/stderr safely
- avoid repeated wrapper stacking that can break tests or CLI output

### `scripts/i18n.py`

Localization module.

Responsibilities:

- detect locale
- provide English and Chinese UI strings
- centralize CLI text output

### `scripts/recommend_skills.py`

Skill recommendation helper.

Responsibilities:

- fetch trending skills from `skills.sh`
- load local recommendation config
- print recommendation lists

Note: this is preserved as an auxiliary feature, not the center of the unified registry workflow.

### `scripts/update_marketplace.py`

Legacy marketplace compatibility helper.

Responsibilities:

- inspect traditional Claude marketplace repos
- pull marketplace changes
- report affected skills

Note: it remains in the repo, but the main workflow in this customized version is centered on `~/.agents/skills` and `.skills-list.json`.

### `scripts/test_agent_skill_updater.py`

Tests for update and install behavior, including:

- OpenSpec metadata parsing
- directory signature comparison
- staging logic
- self-update protection
- targeted update behavior
- install path and registry rewrite checks

### `scripts/test_skills_registry.py`

Tests for registry behavior, including:

- `superpowers` detection as a `skill-pack`
- known source inference
- registry cleanup after skill deletion
- automatic self-update disable for `skills-updater`

### `scripts/recommendations.json`

Static recommendation data used by the recommendation script.

### `references/marketplaces.md`

Reference material kept for marketplace-related compatibility logic.

## Common Commands

Default empty invocation behavior:

```bash
python scripts/check_updates.py
python scripts/update_agent_skills.py
```

Run the update command only when the check step reports available updates.

Check all skills:

```bash
python scripts/check_updates.py
```

Check one skill:

```bash
python scripts/check_updates.py --skill superpowers --json
```

Update all installed skills:

```bash
python scripts/update_agent_skills.py
```

Update one skill:

```bash
python scripts/update_agent_skills.py --skill openspec-explore
```

Install a regular skill:

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
```

Install a `skill-pack`:

```bash
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
```

Install an OpenSpec-generated skill:

```bash
python scripts/install_agent_skill.py --repo https://github.com/Fission-AI/OpenSpec --type single-skill --source-type git-generated --name openspec-explore --workflow-id explore
```

Rebuild the registry after manual folder changes:

```bash
python scripts/sync_skills_registry.py
```

## Verified Behavior

This repository currently covers and verifies these key behaviors:

- `skills-updater` does not self-update
- updates are driven by `.skills-list.json`
- deleted skills are removed from the registry
- installs always target `~/.agents/skills`
- registry contents are refreshed immediately after install

## License

MIT

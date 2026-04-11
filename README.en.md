# Skills Updater

Manage installs, updates, registry sync, and auxiliary discovery tools for skills stored in `~/.agents/skills`.

[中文](README.md)

## Current Structure

This repository now follows a skill-based-architecture layout:

```text
skills-updater/
|-- SKILL.md
|-- README.md
|-- README.en.md
|-- rules/
|   |-- scope-and-registry.md
|   `-- update-policies.md
|-- workflows/
|   |-- default-invocation.md
|   |-- check-updates.md
|   |-- update-skills.md
|   |-- install-skill.md
|   |-- sync-registry.md
|   |-- recommend-skills.md
|   `-- update-marketplace.md
|-- references/
|   |-- gotchas.md
|   |-- script-map.md
|   `-- marketplaces.md
`-- scripts/
```

## What Each Part Contains

### `SKILL.md`

This is the entry file.

- Defines trigger conditions so the skill activates on requests like `"skills-updater"`, `"check skill updates"`, or `"install a skill from GitHub"`
- Lists always-read files
- Routes common tasks to the right rules and workflows
- Surfaces the highest-value gotchas

It should stay short and act as a router, not a full manual.

### `rules/`

Long-lived constraints.

- [rules/scope-and-registry.md](rules/scope-and-registry.md)
  Covers the source of truth, `.skills-list.json`, `.openskills.json`, and the boundaries for `single-skill` and `skill-pack` entries.
- [rules/update-policies.md](rules/update-policies.md)
  Covers version-first checks, per-type update behavior, backups, OpenSpec generation rules, `superpowers` handling, and the self-update guard for `skills-updater`.

If something is supposed to stay true across tasks, it belongs here.

### `workflows/`

Task-specific operating procedures.

- [workflows/default-invocation.md](workflows/default-invocation.md)
  What to do when the user only says `skills-updater`.
- [workflows/check-updates.md](workflows/check-updates.md)
  How to inspect update status for all skills or one named skill.
- [workflows/update-skills.md](workflows/update-skills.md)
  How to apply updates, including backup and skip behavior.
- [workflows/install-skill.md](workflows/install-skill.md)
  How to install a regular skill, a `skill-pack`, or an OpenSpec-generated skill.
- [workflows/sync-registry.md](workflows/sync-registry.md)
  How to rebuild `.skills-list.json` after manual filesystem changes.
- [workflows/recommend-skills.md](workflows/recommend-skills.md)
  How to handle recommendation and discovery requests.
- [workflows/update-marketplace.md](workflows/update-marketplace.md)
  How to handle explicit marketplace maintenance requests under `~/.claude/plugins/marketplaces`.

If the content answers "what should happen for this request type?", it belongs here.

### `references/`

Supporting material that provides context but does not define policy.

- [references/gotchas.md](references/gotchas.md)
  Captures easy-to-miss edge cases, such as `check_updates.py` using exit code `1` to signal available updates.
- [references/script-map.md](references/script-map.md)
  Maps scripts to responsibilities.
- [references/marketplaces.md](references/marketplaces.md)
  Keeps marketplace compatibility notes and source references.

These files support decisions; they do not replace `rules/` or `workflows/`.

### `scripts/`

Implementation entry points.

- `check_updates.py`: probe remote versions from the registry
- `update_agent_skills.py`: apply updates
- `install_agent_skill.py`: install a new skill
- `sync_skills_registry.py`: rebuild the registry
- `skills_registry.py`: detect local entries and maintain `.skills-list.json`
- `agent_skill_updater.py`: staging, signatures, backups, replacement, and OpenSpec generation helpers
- `recommend_skills.py`: recommendation helper
- `update_marketplace.py`: marketplace compatibility helper

The README explains what each script is for; the behavioral rules live in `rules/` and `workflows/`.

## Core Behavior

- Treat `~/.agents/skills` as the only source of truth
- Maintain one registry in `.skills-list.json`
- Compare versions before deciding to update
- Treat `superpowers` as one `skill-pack`
- Treat OpenSpec skills as `git-generated`
- Keep the local customized `skills-updater` on `autoUpdate: false`

## Common Commands

Check for updates:

```bash
python scripts/check_updates.py
python scripts/check_updates.py --skill <name>
```

Apply updates:

```bash
python scripts/update_agent_skills.py
python scripts/update_agent_skills.py --skill <name>
```

Install a new skill:

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
python scripts/install_agent_skill.py --repo https://github.com/Fission-AI/OpenSpec --type single-skill --source-type git-generated --name openspec-explore --workflow-id explore
```

Sync the registry:

```bash
python scripts/sync_skills_registry.py
```

## Maintenance Rules

- Add stable constraints in `rules/`
- Add task procedures in `workflows/`
- Add script notes, compatibility notes, or pitfalls in `references/`
- Change activation and task routing in `SKILL.md`

Do not turn `SKILL.md` back into a long-form manual.

## Origin

This repository is derived from `https://github.com/yizhiyanhua-ai/skills-updater`, but the current implementation is centered on maintaining a unified local skill store at `~/.agents/skills`.

## License

MIT

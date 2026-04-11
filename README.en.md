# Skills Updater

Manage the unified skill store in `~/.agents/skills`.

[中文](README.md)

## What it does

- Maintains the registry at `~/.agents/skills/.skills-list.json`
- Checks upstream versions before downloading anything
- Updates only the skills that actually changed
- Supports installing new skills into `~/.agents/skills`
- Resyncs the registry when skills are added or removed manually

## Scope

This updater only manages `~/.agents/skills`.

If `~/.claude/skills`, `~/.codex/skills`, or `~/.cursor/skills` are symlinked to that folder, that is fine, but this project still treats `~/.agents/skills` as the only source of truth.

## Special cases

- `superpowers` is tracked as one `skill-pack` repo entry. Its child skills are not expanded into separate registry rows.
- OpenSpec skills come from `https://github.com/Fission-AI/OpenSpec` and are regenerated when the upstream package version changes.
- This local `skills-updater` install is customized and has `autoUpdate: false`, so it will not overwrite itself from upstream.

## Commands

Check all installed skills:

```bash
python scripts/check_updates.py
```

Update everything that changed:

```bash
python scripts/update_agent_skills.py
```

Update one skill:

```bash
python scripts/update_agent_skills.py --skill superpowers
```

Install a new skill:

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
```

Install `superpowers` as a skill pack:

```bash
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
```

Resync the registry after manual folder changes:

```bash
python scripts/sync_skills_registry.py
```

## Files

- `SKILL.md`: runtime instructions for the skill
- `scripts/skills_registry.py`: scans `~/.agents/skills` and rewrites `.skills-list.json`
- `scripts/check_updates.py`: compares local and remote versions
- `scripts/update_agent_skills.py`: applies selective updates and writes backups
- `scripts/install_agent_skill.py`: installs new skills into the managed folder

## License

MIT

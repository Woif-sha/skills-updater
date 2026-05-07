---
name: skills-updater
description: Use when the user says "skills-updater", "check skill updates", "update installed skills", "install a skill from GitHub", or "sync .skills-list.json" for ~/.agents/skills.
---

# Skills Updater

Manage the unified skill store at `~/.agents/skills`.

## Always Read

- `rules/scope-and-registry.md`
- `rules/update-policies.md`

## Common Tasks

| Task | Read This | Then Do This |
| --- | --- | --- |
| User only says `skills-updater` | `rules/scope-and-registry.md`, `rules/update-policies.md`, `workflows/default-invocation.md` | Run the operational default flow, not an explanation |
| "Check skill updates" or "is anything outdated?" | `rules/update-policies.md`, `workflows/check-updates.md`, `references/gotchas.md` | Run `check_updates.py`, interpret statuses and exit codes correctly |
| "Update all skills" or "update <name>" | `rules/update-policies.md`, `workflows/update-skills.md`, `references/gotchas.md` | Run `update_agent_skills.py`, report changed entries and backups |
| "Install a skill from GitHub" | `rules/scope-and-registry.md`, `workflows/install-skill.md`, `references/script-map.md` | Install into `~/.agents/skills/<name>` and confirm registry refresh |
| "I added/deleted a skill manually" | `rules/scope-and-registry.md`, `workflows/sync-registry.md` | Rebuild `.skills-list.json` from the filesystem |
| "Recommend skills" or "what should I install?" | `rules/scope-and-registry.md`, `workflows/recommend-skills.md`, `references/marketplaces.md` | Use the recommender flow; do not confuse it with install/update |
| "Update marketplace <name>" | `rules/scope-and-registry.md`, `workflows/update-marketplace.md`, `references/marketplaces.md` | Treat marketplace maintenance as an explicit side task |

## Known Gotchas

- `check_updates.py` exits with code `1` when updates exist. That is a status signal, not necessarily a script failure.
- `skills-updater` is intentionally registered with `autoUpdate: false`; batch updates must not overwrite this local customized copy.
- Updating a git-backed `single-skill` must preserve local edits. The updater reconstructs the installed base from `sourceCommitSha`, merges local changes with the staged remote update, and blocks the update with conflict files instead of discarding local edits.
- `superpowers` is one `skill-pack`, not dozens of child registry entries.
- OpenSpec skills are generated from upstream versioned templates, not copied as static folders.

See `references/gotchas.md` for the full failure modes.

## Rule Priority

Follow `SKILL.md`, then `rules/`, then `workflows/`, then `references/`.

## Boundaries

- This skill manages `~/.agents/skills` as the source of truth. It does not directly manage `~/.claude/skills`, `~/.codex/skills`, or `~/.cursor/skills`.
- Marketplace utilities are optional adjunct tools. Use them only when the user explicitly asks for marketplace discovery or marketplace maintenance.

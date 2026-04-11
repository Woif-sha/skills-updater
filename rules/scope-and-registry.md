# Scope And Registry Rules

These rules apply to every `skills-updater` task.

## Source Of Truth

- Installed skills live under `~/.agents/skills/`.
- The registry file is `~/.agents/skills/.skills-list.json`.
- Managed single-skill installs keep upstream metadata in `.openskills.json`.
- The updater treats `~/.agents/skills` as canonical even if other tool-specific directories are symlinked elsewhere.

## What Counts As A Managed Entry

- `single-skill`: a folder with `SKILL.md`.
- `skill-pack`: a git repository that also contains a child `skills/` directory.
- `managed: true`: the registry knows the upstream repo and can probe versions.
- `managed: false`: the skill is local-only or missing repo metadata; status should stay `unknown_version`.

## Registry Discipline

- Let the scripts sync the registry when their implementation already does so.
- Use `sync_skills_registry.py` only when the user made manual filesystem changes or explicitly asks for a rescan.
- Do not hand-edit `.skills-list.json` unless the user explicitly asks for manual JSON surgery.
- Report registry-backed status from the current generated registry, not from stale assumptions.

## Detection Rules

- `superpowers` is a single `skill-pack` entry when the repo root contains `.git` and `skills/`.
- Do not expand a `skill-pack` into per-child registry records.
- Known single-skill sources can be inferred from `skills_registry.py` even when `.openskills.json` is incomplete.
- Hidden directories and non-skill folders are ignored during sync.

## Scope Limits

- Do not claim this skill manages downstream symlink targets directly.
- Do not mix marketplace state in `~/.claude/plugins` into the core `~/.agents/skills` registry model.
- Keep explanations and reports centered on the agent skill store unless the user explicitly pivots to marketplaces.

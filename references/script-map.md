# Script Map

This file maps the repository scripts to their responsibilities.

## Core Registry And Update Flow

- `scripts/skills_registry.py`: detects skill folders, infers registry entries, and writes `.skills-list.json`
- `scripts/check_updates.py`: syncs the registry, probes remote versions, stores status fields, and prints human or JSON output
- `scripts/update_agent_skills.py`: applies selective updates based on registry entries and remote version checks; reports merge conflicts without overwriting local skills
- `scripts/install_agent_skill.py`: installs a new single skill or skill-pack into `~/.agents/skills`
- `scripts/sync_skills_registry.py`: rescans the filesystem and rewrites `.skills-list.json`

## Shared Helpers

- `scripts/agent_skill_updater.py`: staging, backup, content comparison, three-way merge for git-backed single skills, metadata refresh, git helpers, and OpenSpec generation support
- `scripts/stdio_utils.py`: Windows UTF-8 console handling
- `scripts/i18n.py`: localized status text

## Discovery And Marketplace Utilities

- `scripts/recommend_skills.py`: trending and personalized recommendations
- `scripts/recommendations.json`: fallback recommendation data
- `scripts/update_marketplace.py`: marketplace repo update plus optional reinstall queueing
- `references/marketplaces.md`: supported marketplace notes and install patterns

## Tests

- `scripts/test_skills_registry.py`: registry detection and special-case coverage
- `scripts/test_agent_skill_updater.py`: updater, install, and self-update guard coverage

## When To Read This File

- Read this reference when you need to map a user request to the right script.
- Read it when a workflow mentions a helper but does not explain the underlying module boundaries.
- Do not replace the task-specific workflows with this file; use it as a script index.

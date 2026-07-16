# Script Map

Read this only when selecting or changing an implementation entry point.

## Commands

- `scripts/check_updates.py` — refresh registry state, probe eligible remotes, emit human or JSON status.
- `scripts/update_agent_skills.py` — check and apply selected updates through snapshot, Git-worktree, or generated transactions.
- `scripts/install_agent_skill.py` — validate, stage, and atomically install a GitHub Skill or pack.
- `scripts/sync_skills_registry.py` — rebuild `.skills-list.json` from installed folders and metadata without remote access.

## Libraries

- `scripts/agent_skill_updater.py` — payload boundary, provenance validation, remote resolution, merge, Git state machine, journals, rollback, and recovery.
- `scripts/skills_registry.py` — local detection, metadata validation, locking, and atomic registry writes.
- `scripts/i18n.py` — strict English/Chinese CLI strings.
- `scripts/stdio_utils.py` — Windows UTF-8 stdio configuration.

## Tests

`tests/` is regression coverage that must be included in the commit and is never imported by Skill runtime. Run:

```powershell
python -m unittest discover -s tests
```

# Install A Skill

Use this workflow when the user wants to add a new skill or skill-pack into `~/.agents/skills`.

## Commands

Typical commands:

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
python scripts/install_agent_skill.py --repo https://github.com/Fission-AI/OpenSpec --type single-skill --source-type git-generated --name openspec-explore --workflow-id explore
```

## Steps

1. Normalize the repo argument to a GitHub URL or `owner/repo` form.
2. Decide the install name:
   - explicit `--name` wins
   - otherwise derive from the repo subpath or repo name
3. Choose the correct install type:
   - `single-skill` for a specific skill path
   - `skill-pack` for a whole bundled repository
   - `git-generated` only for generated OpenSpec installs
4. Run the installer.
5. Confirm the destination path and refreshed registry path in the result.

## Important Behaviors

- The destination directory must not already exist.
- Single-skill installs stage remote content and then write `.openskills.json`.
- Skill-pack installs clone the whole repo directly.
- The installer refreshes `.skills-list.json` at the end.

## Completion Checklist

- The installed path is reported.
- The registry refresh is confirmed.
- The chosen install type matches the requested source.

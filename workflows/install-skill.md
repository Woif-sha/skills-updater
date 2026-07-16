# Install A Skill

Use this route for an explicit GitHub installation request.

## Run

```powershell
python scripts/install_agent_skill.py --repo <owner/repo-or-url> --path <repo/subpath> --name <name>
python scripts/install_agent_skill.py --repo <owner/repo-or-url> --type skill-pack --name <name>
```

For a generated Skill, require `--repo Fission-AI/OpenSpec --path . --source-type git-generated --workflow-id <id>`; the workflow id selects the generated template and defaults the installed name to `openspec-<id>`.

1. Require an explicit GitHub repository; never infer one from the Skill name.
2. Select `single-skill` for one payload path or `skill-pack` for a repository root containing `skills/`.
3. Validate the destination name and require the destination to be absent.
4. Stage and validate the exact remote payload before publishing it.
5. Report the installed path, resolved full commit, entry type, and refreshed registry path.

The installer publishes atomically on the destination volume. An install error must leave no partial destination.

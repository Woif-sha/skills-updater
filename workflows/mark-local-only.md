# Mark A Skill Local-Only

Use this route for a self-authored or intentionally frozen Skill that must never contact an upstream.

1. Resolve exactly one direct child of `~/.agents/skills` and require a regular `SKILL.md`.
2. Read its regular `.openskills.json`, or create a JSON object when the file is absent.
3. Preserve existing provenance and set only:

```json
{
  "updatePolicy": "local-only"
}
```

4. Write valid UTF-8 JSON atomically.
5. Run `python scripts/sync_skills_registry.py --json`.
6. Run `python scripts/check_updates.py --skill <name> --json` and verify `status: "local_only"`, `managed: false`, and `remote_version: null`.

If metadata is a link, non-object JSON, or cannot be written atomically, stop with an error. Do not replace it with a guessed metadata object.

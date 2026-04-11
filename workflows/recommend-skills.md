# Recommend Skills

Use this workflow when the user asks for skill recommendations or discovery help.

## Commands

Run one of:

```bash
python scripts/recommend_skills.py
python scripts/recommend_skills.py --source skills.sh --limit 10
python scripts/recommend_skills.py --json
```

## Steps

1. Choose a source scope:
   - default `all` for general discovery
   - `skills.sh` when the user wants leaderboard-style trending skills
2. Run the recommender.
3. Separate the result into:
   - trending skills from external sources
   - personalized recommendations inferred from installed plugin categories
4. Make it clear that this workflow recommends skills; it does not install them.

## Important Behaviors

- Personalized recommendations are derived from `~/.claude/plugins/installed_plugins.json`, not `~/.agents/skills/.skills-list.json`.
- If scraping `skills.sh` fails, the script falls back to `scripts/recommendations.json`.
- Use `references/marketplaces.md` when the user also needs install source context.

## Completion Checklist

- Recommendation output is scoped to the user's request.
- Discovery results are not presented as already-installed skills.

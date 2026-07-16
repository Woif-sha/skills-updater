# Close A Behavior Change

Use this gate after updater code, output contracts, routing, rules, or workflows change.

1. Recheck the original request and forbidden shortcuts.
2. Run targeted regressions, then all affected test groups with a 60-second process limit.
3. Run compile, structure, link, Skill validation, and `git diff --check` checks.
4. Exercise the changed CLI contract directly; do not infer success from unit tests alone.
5. Review the diff for duplicated state, broad swallowing, hidden fallback, partial mutation, and dead files.
6. Run a short AAR: if a costly, reusable lesson is missing or stale, follow [update-rules.md](update-rules.md); otherwise add nothing.

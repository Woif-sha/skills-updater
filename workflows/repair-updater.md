# Repair Or Review The Updater

Use this route for changes to this repository's implementation.

1. Reproduce or locate the failure before editing.
2. Identify the shared invariant and every caller that must use it; do not add a symptom-specific bypass.
3. Remove obsolete or contradictory paths before adding logic.
4. Add a regression test for the incident, a normal case, and the nearest destructive boundary.
5. Follow [task-closure.md](task-closure.md) before reporting completion.

Transaction catches that roll back and re-raise are integrity mechanisms, not fallbacks. Silent substitution, branch guessing, stale-cache reuse, and fake success paths are forbidden.

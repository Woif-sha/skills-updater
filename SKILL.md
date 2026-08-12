---
name: skills-updater
description: >
  This skill should be used when the user says "skills-updater", "检查 skill 更新",
  "更新已安装的 skill", "从 GitHub 安装 skill", "同步 .skills-list.json",
  "这个 skill 是我自己写的，不要联网更新", "列出或清理 Intervention Record",
  or "修复 skills-updater".
  Activate only for the install, update, and registry lifecycle under
  ~/.agents/skills, updater-owned Intervention Records, or this updater's implementation.
---

# Skills Updater

Manage the canonical Skill store at `~/.agents/skills`.

## Route Every Request

1. Read `routing.yaml`.
2. Apply its `routing_rules` and select the matching route or routes.
3. Read only `always_read`, each selected route's `required_reads`, and its `workflow`.
4. If no route matches, stop and state that the request is outside this Skill; do not guess a command.

Do not reuse the previous route after the user changes tasks.

## Canonical Safety Boundaries

These are the single normative definitions for every route. Operation workflows apply them; repair/review material explains implementation consequences without replacing them.

- **Payload and control data** — `.git` and `.openskills.json` are control data, never Skill payload. Payload signatures, validation, copies, merges, deletion, and rollback exclude them and reject links, traversal, and case-colliding names.
- **Local-only** — `updatePolicy: "local-only"` forbids every remote probe, stage, fetch, and update. Validate only the policy and metadata file safety; provenance fields are inert.
- **Provenance and no fallback** — every remotely managed entry requires explicit, consistent source, repository, subpath, mode, and version identity. Missing or contradictory identity is an error; never infer a repository, source type, branch, origin, installed base, or cached substitute.
- **JSON** — JSON mode emits valid structured JSON for argument errors, operational failures, and success. Classify the structured result instead of treating an exit code as the result.

## Rule Priority

`SKILL.md` → `rules/` → `workflows/` → `references/` → `README.md`.

## Scope

This Skill owns `~/.agents/skills` and updater-created records under `~/.agents/interventions`. It does not manage Claude marketplaces, recommend third-party Skills, or maintain tool-specific mirror directories.

---
name: skills-updater
description: >
  This skill should be used when the user says "skills-updater", "检查 skill 更新",
  "更新已安装的 skill", "从 GitHub 安装 skill", "同步 .skills-list.json",
  "这个 skill 是我自己写的，不要联网更新", or "修复 skills-updater".
  Activate only for the install, update, and registry lifecycle under
  ~/.agents/skills or this updater's own implementation.
---

# Skills Updater

Manage the canonical Skill store at `~/.agents/skills`.

## Route Every Request

1. Read `routing.yaml`.
2. Apply its `routing_rules` and select the matching route or routes.
3. Read only `always_read`, each selected route's `required_reads`, and its `workflow`.
4. If no route matches, stop and state that the request is outside this Skill; do not guess a command.

Do not reuse the previous route after the user changes tasks.

## Non-Negotiable Boundaries

- `.git` and `.openskills.json` are control data, never Skill payload.
- `updatePolicy: "local-only"` forbids every remote probe, stage, fetch, and update.
- Local-only validates the policy and metadata file safety only; every remotely managed entry requires explicit, consistent provenance with no fallback.
- JSON mode must remain valid JSON on both success and failure.

## Rule Priority

`SKILL.md` → `rules/` → `workflows/` → `references/` → `README.md`.

## Scope

This Skill owns `~/.agents/skills` only. It does not manage Claude marketplaces, recommend third-party Skills, or maintain tool-specific mirror directories.

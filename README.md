# Skills Updater

统一管理 `~/.agents/skills` 下的 skill 安装、更新、注册表同步与附属推荐工具。

[English](README.en.md)

## 当前结构

这个仓库现在按 skill-based-architecture 组织：

```text
skills-updater/
|-- SKILL.md
|-- README.md
|-- README.en.md
|-- rules/
|   |-- scope-and-registry.md
|   `-- update-policies.md
|-- workflows/
|   |-- default-invocation.md
|   |-- check-updates.md
|   |-- update-skills.md
|   |-- install-skill.md
|   |-- sync-registry.md
|   |-- recommend-skills.md
|   `-- update-marketplace.md
|-- references/
|   |-- gotchas.md
|   |-- script-map.md
|   `-- marketplaces.md
`-- scripts/
```

## 各部分职责

### `SKILL.md`

入口文件。

- 定义触发条件，确保 agent 在 `"skills-updater"`、`"check skill updates"`、`"install a skill from GitHub"` 这类请求下正确启用
- 规定常读文件
- 提供常见任务路由表
- 提醒几个最关键的 gotchas

它应该保持短小，只负责导航，不承载完整规则手册。

### `rules/`

长期稳定约束。

- [rules/scope-and-registry.md](rules/scope-and-registry.md)
  说明 source of truth 是 `~/.agents/skills`，以及 `.skills-list.json`、`.openskills.json`、`single-skill`、`skill-pack` 的边界和识别规则。
- [rules/update-policies.md](rules/update-policies.md)
  说明版本优先、按类型更新、本地修改三方合并、备份策略、OpenSpec 生成逻辑、`superpowers` 特判，以及 `skills-updater` 自更新保护。

如果某条规则是“总是成立”的，就应该放在这里，而不是写进 workflow。

### `workflows/`

按任务拆分的执行流程。

- [workflows/default-invocation.md](workflows/default-invocation.md)
  用户只说 `skills-updater` 时的默认执行语义。
- [workflows/check-updates.md](workflows/check-updates.md)
  检查全部或指定 skill 的更新状态。
- [workflows/update-skills.md](workflows/update-skills.md)
  执行全量或单项更新，并说明本地修改合并、备份、冲突与跳过逻辑。
- [workflows/install-skill.md](workflows/install-skill.md)
  从 GitHub 安装普通 skill、`skill-pack` 或 OpenSpec 生成型 skill。
- [workflows/sync-registry.md](workflows/sync-registry.md)
  手动增删目录后重建 `.skills-list.json`。
- [workflows/recommend-skills.md](workflows/recommend-skills.md)
  推荐与发现 skill，不混同于安装和更新。
- [workflows/update-marketplace.md](workflows/update-marketplace.md)
  显式处理 `~/.claude/plugins/marketplaces` 下的 marketplace 更新。

如果某条内容描述的是“遇到某类请求时怎么做”，就放在这里。

### `references/`

补充性资料，不直接定义行为。

- [references/gotchas.md](references/gotchas.md)
  记录最容易误判的坑，例如 `check_updates.py` 用退出码 `1` 表示“有更新可用”。
- [references/script-map.md](references/script-map.md)
  对应仓库脚本和职责的索引，方便从用户请求映射到脚本。
- [references/marketplaces.md](references/marketplaces.md)
  marketplace 兼容信息和来源说明。

这些文件用来补上下文，不应替代 `rules/` 或 `workflows/`。

### `scripts/`

实际执行逻辑。

- `check_updates.py`: 读取注册表并探测远端版本
- `update_agent_skills.py`: 应用更新，遇到本地/远端冲突时报告错误而不覆盖本地 skill
- `install_agent_skill.py`: 安装新 skill
- `sync_skills_registry.py`: 重建注册表
- `skills_registry.py`: 识别本地目录并维护 `.skills-list.json`
- `agent_skill_updater.py`: 远端拉取、暂存、目录签名、备份、三方合并与元数据刷新
- `recommend_skills.py`: 推荐技能
- `update_marketplace.py`: marketplace 兼容脚本

README 只说明这些脚本分别做什么；具体执行规则以 `rules/` 和 `workflows/` 为准。

## 核心行为

- 只把 `~/.agents/skills` 当成 source of truth
- 通过 `.skills-list.json` 维护统一注册表
- 先比较版本，再决定是否更新
- git-backed `single-skill` 更新时保留本地修改：用 `sourceCommitSha` 还原安装基准，三方合并本地与远端变更；冲突时阻止覆盖并输出冲突文件
- `superpowers` 视为一个整体 `skill-pack`
- OpenSpec skill 视为 `git-generated`
- 本地定制版 `skills-updater` 保持 `autoUpdate: false`

## 常见命令

检查更新：

```bash
python scripts/check_updates.py
python scripts/check_updates.py --skill <name>
```

应用更新：

```bash
python scripts/update_agent_skills.py
python scripts/update_agent_skills.py --skill <name>
```

安装新 skill：

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
python scripts/install_agent_skill.py --repo https://github.com/Fission-AI/OpenSpec --type single-skill --source-type git-generated --name openspec-explore --workflow-id explore
```

同步注册表：

```bash
python scripts/sync_skills_registry.py
```

## 维护约定

- 想加稳定规则，改 `rules/`
- 想加某类请求的处理流程，改 `workflows/`
- 想补充脚本说明、兼容信息或坑点，改 `references/`
- 想改 skill 触发和任务路由，改 `SKILL.md`

不要把新的长篇说明再塞回 `SKILL.md`。

## 来源

本仓库基于 `https://github.com/yizhiyanhua-ai/skills-updater` 二次改造，当前实现更偏向本地统一 skill 目录 `~/.agents/skills` 的维护，而不是多套目录并行管理。

## License

MIT

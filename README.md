# Skills Updater

统一管理 `~/.agents/skills` 下的技能。

[English](README.en.md)

## 现在的职责

- 维护注册表 `~/.agents/skills/.skills-list.json`
- 先检查远程版本，再决定是否更新
- 只更新真正有变化的技能
- 支持把新技能安装到 `~/.agents/skills`
- 发现手动新增或删除技能后，重新同步注册表

## 管理范围

这个 updater 只管理 `~/.agents/skills`。

如果 `~/.claude/skills`、`~/.codex/skills`、`~/.cursor/skills` 软链接到这里，没有问题，但它们不是 updater 的直接管理对象。

## 特殊规则

- `superpowers` 按一个 `skill-pack` 仓库处理，不展开记录里面每个子 skill。
- OpenSpec 技能来自 `https://github.com/Fission-AI/OpenSpec`，属于 `git-generated`，更新时要重新生成。
- 当前本地 `skills-updater` 是定制版，注册表中 `autoUpdate: false`，不会被它自己从上游覆盖。

## 常用命令

检查全部技能更新：

```bash
python scripts/check_updates.py
```

更新全部有变化的技能：

```bash
python scripts/update_agent_skills.py
```

更新单个技能：

```bash
python scripts/update_agent_skills.py --skill superpowers
```

安装新技能：

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
```

安装 `superpowers`：

```bash
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
```

手动增删目录后重建注册表：

```bash
python scripts/sync_skills_registry.py
```

## 关键文件

- `SKILL.md`：技能运行说明
- `scripts/skills_registry.py`：扫描 `~/.agents/skills` 并重写 `.skills-list.json`
- `scripts/check_updates.py`：比较本地和远程版本
- `scripts/update_agent_skills.py`：按版本差异执行选择性更新
- `scripts/install_agent_skill.py`：安装新技能到统一目录

## License

MIT

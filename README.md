# Skills Updater

为 `~/.agents/skills` 提供 Skill 安装、更新检查、事务化更新和注册表同步。

[English](README.en.md)

## 功能

- 从 GitHub 安装单个 Skill、Skill Pack 或 OpenSpec 生成型 Skill。
- 检查全部或指定 Skill 的远端版本。
- 区分普通目录和根目录本身就是 Git 工作树的 Skill。
- 在保留本地修改的前提下执行三方合并或 Git fast-forward。
- 用 `updatePolicy: "local-only"` 永久禁止自编 Skill 的远端探测和更新。
- 始终以结构化 JSON 报告自动化调用结果。

## 环境要求

- Python 3.10 或更高版本。
- Git。
- 能访问待安装或更新的 GitHub 仓库。
- 只有安装或更新 OpenSpec 生成型 Skill 时才需要 Node.js 和 npm。

运行时代码只使用 Python 标准库，不需要执行 `pip install`。

## 安装 Skills Updater

推荐把仓库直接克隆到统一 Skill 目录，并为这个根 Git 工作树写入完整来源元数据。以下 PowerShell 命令适用于全新安装：

```powershell
$skillDir = Join-Path $HOME ".agents\skills\skills-updater"
New-Item -ItemType Directory -Force (Split-Path $skillDir) | Out-Null
git clone https://github.com/Woif-sha/skills-updater.git $skillDir
Set-Location $skillDir

$sha = git rev-parse HEAD
@'
import json
import sys
from pathlib import Path

metadata = {
    "source": "Woif-sha/skills-updater",
    "sourceType": "git",
    "repoUrl": "https://github.com/Woif-sha/skills-updater",
    "subpath": ".",
    "installedBaseVersion": sys.argv[1],
}
Path(".openskills.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
'@ | python - $sha

python scripts/sync_skills_registry.py --json
```

`.openskills.json` 是本地控制数据，已被 Git 忽略，不会提交到仓库。已有安装不要再次执行 `git clone`；进入原目录，确认元数据和 Git upstream 正确后运行注册表同步即可。

## 快速使用

可以从任意目录调用脚本：

```powershell
$updater = Join-Path $HOME ".agents\skills\skills-updater\scripts"

# 同步本地注册表
python "$updater\sync_skills_registry.py" --json

# 检查全部或单个 Skill
python "$updater\check_updates.py" --json
python "$updater\check_updates.py" --skill zotero-paper-updater --json

# 只检查更新流程，不应用更改
python "$updater\update_agent_skills.py" --check-only --json

# 更新全部或单个 Skill
python "$updater\update_agent_skills.py" --json
python "$updater\update_agent_skills.py" --skill zotero-paper-updater --json
```

检查和更新命令可使用 `--lang zh` 或 `--lang en` 强制指定人类可读输出语言；JSON 字段名保持稳定。

所有运行入口都以 `~/.agents/skills` 为唯一安装源，并维护 `~/.agents/skills/.skills-list.json`。不要手工编辑注册表；修改 Skill 目录或 `.openskills.json` 后重新运行同步命令。

### 从 GitHub 安装

```powershell
# 仓库根目录就是一个 Skill
python "$updater\install_agent_skill.py" `
  --repo owner/repo --path . --name my-skill --json

# Skill 位于仓库子目录
python "$updater\install_agent_skill.py" `
  --repo owner/repo --path skills/my-skill --name my-skill --json

# 根仓库是包含 skills/ 的 Skill Pack
python "$updater\install_agent_skill.py" `
  --repo owner/repo --type skill-pack --path . --json

# 从 OpenSpec 生成指定 workflow Skill
python "$updater\install_agent_skill.py" `
  --repo Fission-AI/OpenSpec --source-type git-generated `
  --workflow-id explore --json
```

安装目标已存在、来源不是 GitHub、路径越界或来源契约不完整时，安装会明确失败，不会覆盖已有目录或猜测替代来源。

## 自编 Skill：永久禁止远端更新

在该 Skill 根目录的 `.openskills.json` 中显式写入：

```json
{
  "source": "my-skill",
  "sourceType": "local",
  "updatePolicy": "local-only"
}
```

已有 Git provenance 可以保留，只需增加：

```json
{
  "updatePolicy": "local-only"
}
```

实际文件应保留原有字段，而不是用上面的单字段示例覆盖整个文件。该策略会在锁内以及网络、备份和变更边界前重新读取：

- 检查结果为 `status: "local_only"`；
- 更新结果为 `action: "skipped_local"`、`applied: false`；
- `remote_version` 始终为 `null`；
- 不解析远端分支，不 fetch，不暂存，不备份，也不尝试更新。

## 远端管理元数据

远端管理项必须使用明确且相互一致的来源契约。普通 Git single-skill 的 `subpath` 应填写它在仓库内的实际相对路径：

```json
{
  "source": "owner/repo",
  "sourceType": "git",
  "repoUrl": "https://github.com/owner/repo",
  "subpath": "skills/my-skill",
  "installedBaseVersion": "完整的 40 位 Git commit SHA"
}
```

根仓库是 Skill Pack 时，`sourceType` 必须为 `git-pack`：

```json
{
  "source": "owner/repo",
  "sourceType": "git-pack",
  "repoUrl": "https://github.com/owner/repo",
  "subpath": ".",
  "installedBaseVersion": "完整的 40 位 Git commit SHA"
}
```

OpenSpec 生成型 Skill 使用生成器和 workflow 标识，版本来自生成后的 `SKILL.md`：

```json
{
  "source": "Fission-AI/OpenSpec",
  "sourceType": "git-generated",
  "repoUrl": "https://github.com/Fission-AI/OpenSpec",
  "subpath": ".",
  "generator": "dist/core/shared/skill-generation.js",
  "workflowId": "explore"
}
```

- `installedBaseVersion` 是上次纳入本地内容的上游基线。
- 仓库根目录就是 single-skill 时，`subpath` 使用 `.`；根 Git 工作树必须使用 `.`。
- 根 Git 工作树的当前版本来自本地 `HEAD`，并要求当前分支有显式 `origin` upstream。
- `sourceCommitSha` 已停止支持；不会把它当作兼容字段兜底。
- 不含 `.git` 且缺少来源信息的目录保持 `unmanaged / unknown_version`，不会被猜测成远端 Skill。

## 更新模式

| 本地形态 | 更新方式 | 安全约束 |
| --- | --- | --- |
| 根目录含 `.git` | Git 工作树事务 | 只允许显式 upstream；干净且 behind 时 fast-forward；dirty、detached 或 diverged 时停止 |
| 普通 Skill 目录 | Snapshot 三方合并 | 使用精确安装基线合并 `base + local + remote`；冲突不覆盖本地内容 |
| 根目录含 `skills/` 的仓库 | Skill Pack Git 事务 | 整个仓库作为一个注册项，不拆分子 Skill |
| OpenSpec 生成型 Skill | 精确 revision 重新生成 | 只接受规定的仓库、generator 和 `workflowId` |

`.git` 和 `.openskills.json` 始终是控制数据，不参与签名、合并、备份、复制或删除。载荷、Git 和元数据变更都有持久化 journal；失败时回滚，无法证明安全时保留恢复数据并返回 `error`。

## JSON 状态和退出码

常见 `status`：

- `up_to_date`：本地已包含远端版本。
- `update_available`：存在可应用更新。
- `local_only`：明确禁止远端访问。
- `unknown_version`：本地目录没有足够 provenance。
- `error`：状态不安全、来源矛盾或操作失败。

常见 `action`：`none`、`payload_merged`、`fast_forwarded`、`metadata_refreshed`、`skipped_local`。

- `check_updates.py`：无更新且无错误时退出 `0`；存在更新、错误或空选择时退出 `1`。
- 更新、安装和同步脚本：成功退出 `0`，操作错误退出 `1`。
- 所有脚本的参数解析错误退出 `2`；使用 `--json` 时 stdout 仍是合法 JSON，不输出 argparse usage 或 traceback。

## 项目结构

```text
SKILL.md          # 轻量入口
routing.yaml      # 唯一任务路由表
rules/            # 稳定不变量
workflows/        # 按用户意图拆分的步骤
references/       # gotchas 与代码索引
scripts/          # 运行时代码
tests/            # 回归规范，不参与 Skill 运行时加载
```

详细执行约束由 [SKILL.md](SKILL.md) 路由到 `rules/`、`workflows/` 和 `references/`。README 负责面向使用者的完整说明，不属于常驻路由。

## 开发验证

```powershell
python -m unittest discover -s tests
python -m compileall -q scripts tests
git diff --check
```

`tests/` 必须纳入版本控制，用来防止 `.git` 误删、部分更新、并发元数据覆盖、ZIP 路径逃逸和 local-only 联网等缺陷再次出现。只有缓存、覆盖率和临时产物会被忽略。

## License

MIT

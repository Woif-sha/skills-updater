# Skills Updater

统一管理 `~/.agents/skills` 下所有 skills 的更新、安装和注册表同步。

[English](README.en.md)

## 项目来源

本项目基于 `https://github.com/yizhiyanhua-ai/skills-updater` 进行二次改造。

原项目的重点更偏向 Claude Code 多来源技能更新与推荐；当前版本已经按本仓库的使用方式做了重构，核心目标变成：

- 只管理 `~/.agents/skills`
- 维护统一注册表 `.skills-list.json`
- 先比对版本，再决定是否下载和替换
- 支持 `skill-pack`、普通单 skill、以及 OpenSpec 生成型 skill
- 保证本地定制版 `skills-updater` 不会被它自己覆盖

## 当前设计

`~/.agents/skills` 是唯一的技能源目录。

你可以把 `~/.claude/skills`、`~/.codex/skills`、`~/.cursor/skills` 软链接到这里，但 updater 本身不会直接管理那些目录。所有检查、安装、更新、删除同步，最终都只围绕这一处展开。

注册表文件固定为：

```text
~/.agents/skills/.skills-list.json
```

## 主要功能

### 1. 统一扫描和注册

- 扫描 `~/.agents/skills` 下的顶层目录
- 自动识别普通单 skill 和 `skill-pack`
- 为每个受管 skill 写入或刷新来源信息
- 重建 `.skills-list.json`

### 2. 基于版本的更新检查

- 先读取本地注册表中的 `localVersion`
- 再向上游仓库查询 `remoteVersion`
- 仅当版本不同才进入实际更新流程
- 避免“全部下载下来再做目录 diff”这种高成本方式

### 3. 选择性更新 skill

- 支持更新全部 skill
- 支持只更新指定 skill
- 更新前自动生成备份目录
- 更新后自动同步注册表

### 4. 安装新 skill

- 直接安装到 `~/.agents/skills/<skill-name>`
- 安装完成后自动刷新 `.skills-list.json`
- 普通 skill 会写入 `.openskills.json`
- `skill-pack` 会保留整仓结构

### 5. 同步手动删除或新增

如果你手动删除了某个 skill 目录，或者手动新增了一个 skill 目录，运行同步脚本后，`.skills-list.json` 会按当前文件系统状态重建，旧条目会被移除，新条目会被纳入。

### 6. 特殊处理 `superpowers`

- `superpowers` 视为一个整体 `skill-pack`
- 注册表只记录整仓信息
- 不展开记录其中每个子 skill
- 上游更新时，直接更新整仓即可

### 7. 特殊处理 OpenSpec

OpenSpec 技能不是静态目录复制，而是生成型 skill：

- 上游仓库：`https://github.com/Fission-AI/OpenSpec`
- 类型：`git-generated`
- 检查更新时读取上游包版本
- 版本变化后，重新拉取并生成对应 workflow skill

### 8. 阻止 `skills-updater` 自更新

这个仓库里的 `skills-updater` 是本地定制版，所以它在注册表里会被标记为：

```json
{
  "autoUpdate": false
}
```

这样在批量更新时，它不会把自己从上游覆盖掉。

## 目录结构

```text
skills-updater/
├─ README.md
├─ README.en.md
├─ SKILL.md
├─ references/
│  └─ marketplaces.md
└─ scripts/
   ├─ agent_skill_updater.py
   ├─ check_updates.py
   ├─ i18n.py
   ├─ install_agent_skill.py
   ├─ recommend_skills.py
   ├─ recommendations.json
   ├─ skills_registry.py
   ├─ stdio_utils.py
   ├─ sync_skills_registry.py
   ├─ test_agent_skill_updater.py
   ├─ test_skills_registry.py
   ├─ update_agent_skills.py
   └─ update_marketplace.py
```

## 脚本说明

### `scripts/agent_skill_updater.py`

底层更新工具模块。

负责：

- 下载 GitHub 仓库归档
- 暂存远端 skill 内容
- 计算目录签名
- 比较本地与暂存内容
- 执行替换
- 创建备份
- 处理 OpenSpec 生成流程
- 执行 `skill-pack` 的 git clone / git pull 相关辅助逻辑

### `scripts/skills_registry.py`

注册表核心模块。

负责：

- 获取 `~/.agents/skills`
- 读取和写入 `.skills-list.json`
- 识别普通单 skill 和 `skill-pack`
- 推断已知 skill 的上游仓库来源
- 刷新 `localVersion`
- 对 `skills-updater` 强制写入 `autoUpdate: false`

### `scripts/check_updates.py`

只做检查，不做替换。

负责：

- 先同步注册表
- 查询每个 skill 的远端版本
- 输出 `up_to_date`、`update_available`、`unknown_version`、`error`
- 支持 `--skill` 和 `--json`

### `scripts/update_agent_skills.py`

真正执行更新的主入口。

负责：

- 按注册表逐项检查更新
- 跳过 `autoUpdate: false` 的 skill
- 对有版本变化的 skill 执行更新
- 对 `skill-pack` 执行整仓拉取
- 对 OpenSpec 执行重新生成
- 更新完成后重写注册表

### `scripts/install_agent_skill.py`

安装新 skill 的入口。

负责：

- 解析 GitHub 仓库地址
- 计算安装目录名
- 确保安装目标是 `~/.agents/skills/<name>`
- 安装普通单 skill 或整仓 `skill-pack`
- 写入 `.openskills.json`
- 安装完成后同步 `.skills-list.json`

### `scripts/sync_skills_registry.py`

纯同步入口。

负责：

- 重新扫描 `~/.agents/skills`
- 重建 `.skills-list.json`
- 删除已经不存在的条目
- 纳入新出现的条目

### `scripts/stdio_utils.py`

Windows 终端输出辅助模块。

负责：

- 安全配置 UTF-8 标准输出和错误输出
- 避免重复包裹 `stdout/stderr` 导致测试或 CLI 异常

### `scripts/i18n.py`

国际化模块。

负责：

- 检测系统语言
- 提供中英文输出文本
- 为 CLI 脚本统一输出文案

### `scripts/recommend_skills.py`

技能推荐脚本。

负责：

- 从 `skills.sh` 获取推荐或热门 skill
- 加载本地推荐配置
- 输出推荐列表

说明：这是保留的辅助能力，不是当前统一技能目录管理流程的核心。

### `scripts/update_marketplace.py`

旧 marketplace 兼容脚本。

负责：

- 检查传统 Claude marketplace 仓库更新
- 拉取 marketplace
- 标记受影响的 skill

说明：这个脚本保留在仓库中，但当前主流程已经转向 `~/.agents/skills` 和 `.skills-list.json`。

### `scripts/test_agent_skill_updater.py`

更新和安装相关测试，覆盖：

- OpenSpec 元数据解析
- 目录签名比较
- 远端暂存逻辑
- `skills-updater` 自更新保护
- 指定条目更新
- 安装路径与注册表回写

### `scripts/test_skills_registry.py`

注册表测试，覆盖：

- `superpowers` 识别为 `skill-pack`
- 已知来源推断
- 删除 skill 后注册表条目清理
- `skills-updater` 自动禁用自更新

### `scripts/recommendations.json`

推荐 skill 的静态配置文件，供推荐脚本读取。

### `references/marketplaces.md`

保留的参考资料文件，主要服务于 marketplace 相关兼容逻辑。

## 常用命令

检查全部 skill 的更新状态：

```bash
python scripts/check_updates.py
```

检查单个 skill：

```bash
python scripts/check_updates.py --skill superpowers --json
```

更新全部已安装 skill：

```bash
python scripts/update_agent_skills.py
```

更新指定 skill：

```bash
python scripts/update_agent_skills.py --skill openspec-explore
```

安装普通 skill：

```bash
python scripts/install_agent_skill.py --repo anthropics/skills --path skills/docx --name docx
```

安装 `skill-pack`：

```bash
python scripts/install_agent_skill.py --repo https://github.com/obra/superpowers --type skill-pack --name superpowers
```

安装 OpenSpec 生成型 skill：

```bash
python scripts/install_agent_skill.py --repo https://github.com/Fission-AI/OpenSpec --type single-skill --source-type git-generated --name openspec-explore --workflow-id explore
```

手动改动目录后重建注册表：

```bash
python scripts/sync_skills_registry.py
```

## 当前验证状态

当前仓库已经覆盖并验证的关键行为包括：

- `skills-updater` 不会自更新
- 更新按 `.skills-list.json` 驱动
- 删除 skill 后注册表会清理对应条目
- 安装 skill 时目标目录固定在 `~/.agents/skills`
- 安装完成后注册表会同步刷新

## License

MIT

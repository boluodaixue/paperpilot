# W0：Memory/Vault 契约与安全基础

## 状态

已完成（2026-08-28）。W0 只建立长期 Memory 与 Vault 的最小契约，没有开始 W1 的多 Memory 研究写入。

## 目标与边界

W0 在不改变 Research AgentGraph、Research Workflow、fork policy、递归上限、checkpointer、Markdown Memory Store、Chat Store 和 N6 可选 Red/Blue 的前提下，定义长期 `memory_id`、Vault 根路径以及 Markdown/frontmatter/WikiLink 的安全边界。

本阶段没有新增服务、Repository、索引、Agent 角色或自动写入行为，也没有移动或改写既有 Memory 文件。

## 已完成

- 新增 frozen `MemoryDescriptor`，且只包含 `memory_id`、`title`、`relative_path`、`created_at`、`updated_at`；
- `memory_id` 使用独立的长期 `M-<stable-id>` 契约，不复用会话级 `session_id` 或执行级 `thread_id`；
- Memory 的规范目录由 ID 确定为 `Memories/M-<stable-id>/`，标题变化不会改变 ID 或目录；
- 重复 `memory_id`、非法 ID、非规范 descriptor 路径和时间倒序均由纯函数确定性拒绝；
- PaperPilot 管理的 Markdown frontmatter 使用平面 YAML Properties，校验稳定 ID、Memory ID、类型、标题、带时区 ISO-8601 时间、来源、状态和字符串 tags；
- Vault 路径只接受正斜杠分隔的相对 `.md` 路径，拒绝绝对路径、盘符/UNC、`.`、`..`、非 Markdown 后缀、控制字符、Windows ADS、设备名和尾随空格/点；
- 路径解析以解析后的 Vault 根为边界，已有 symlink/junction 及其下尚不存在的目标都不能逃逸；
- 新 WikiLink 使用无扩展名的 Vault 根相对完整路径 `Memories/M-.../...`，拒绝裸文件名、歧义路径、链接语法注入和不安全 Windows 路径组件；
- 默认配置收敛为 `research.vault_root`；旧 `research.memory_root` 仍可读取，两者并存时新键优先；默认物理路径仍为项目下的 `memory/`；
- 既有 Vault 根目录 `reports/`、`evidence/`、`sources/` 通过只读识别函数暴露为 legacy 布局；识别会忽略逃逸 Vault 的 symlink，不创建、移动或修改文件；
- 现有 `MarkdownMemoryStore`、`MemoryManifest` 和根目录持久化路径保持不变，因此 N1–N6 调用者不需要迁移。

## 安全契约

### 身份与位置

`memory_id` 是长期身份，`relative_path` 只能是由该 ID 推导出的规范目录。标题和未来文件名可以改变，但不能反向生成或修改 `memory_id`。

### Frontmatter

W0 只定义并校验新 PaperPilot 管理笔记的 frontmatter。既有根目录报告仍按 legacy 只读兼容识别；W0 不在启动时重写旧 frontmatter。

### 路径与链接

路径解析与 WikiLink 校验都是无写入的纯契约。W0 没有创建 Memory/Home、写入多 Memory 子目录或扫描索引。调用方在后续阶段写入前仍必须使用这些校验，不能把 Obsidian URI 或用户输入直接当作文件路径。

## 明确未实现

- W1：新建、列出或选择 Memory，以及把研究写入 `Memories/M-.../`；
- W2：Obsidian URI、按钮、CLI 输出或前端改动；
- W3：Memory 扫描、检索、索引、Research Brief 注入或继续研究；
- W4：Memory 问答、保存为笔记、Home 更新或内容哈希并发控制；
- W5：资料导入、附件或整理提案；
- W6：迁移入口、迁移命令或默认产品闭环；
- 数据库、向量索引、图数据库、第二套存储、新 Agent 角色或自动写入。

## 验收结果

- W0 专项：`80 passed`；
- 原 N1–N6 回归：`160 passed, 1 warning`；
- 包含 W0 的仓库全量回归：`240 passed, 1 warning`；
- warning 仍是既有 `StarletteDeprecationWarning`；
- `compileall`、导入检查和 `git diff --check` 通过。

W0 正式完成。W1 尚未开始。

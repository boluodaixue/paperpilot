# LLM Wiki + Obsidian 目标架构

## 1. 状态与定位

本方案已于 2026-08-28 确认，定义 N0–N6 之后的 LLM Wiki 目标架构。

LLM Wiki 不是 PaperPilot 内置的另一套 Markdown 阅读器。它是现有 Markdown Memory Store 之上的“记忆选择、检索、对话、受控写入和继续研究”能力：

- **Obsidian** 负责阅读、手工编辑、WikiLink、Backlinks 和图谱浏览；
- **PaperPilot** 负责 Research Agent、记忆检索、带引用对话、变更提案、来源追溯和原子写入；
- **Markdown Vault** 是唯一知识真相源；任何索引都必须可从 Markdown 重建。

## 2. 核心决策

### 2.1 一个 Vault，多个 Memory

PaperPilot 默认只管理一个 Obsidian Vault。每个长期研究主题或项目是 Vault 中的一个独立 Memory 目录，而不是一个新 Vault。

这同时满足：

- 每项研究的文件、对话和后续增量相互隔离；
- 同一 Memory 可以容纳多次研究执行和用户笔记；
- 不同 Memory 之间仍可建立 Obsidian 原生 WikiLink 和 Backlink；
- 可以在 Obsidian 中查看单个 Memory 的局部图，也可查看整个 Vault 的全局图。

如果用户显式需要完全物理隔离，可以把某个 Memory 导出为独立 Vault；这是导出能力，不是默认运行模型。

### 2.2 Memory 不等于一次 Agent 执行

三种身份不得混用：

| 字段 | 生命周期 | 用途 |
|---|---|---|
| `memory_id` | 长期 | 一个持久主题、项目或知识空间 |
| `session_id` | 会话级 | 一段 UI 对话与历史记录 |
| `thread_id` | 执行级 | 一次 Research Workflow 或 Agent 运行 |

创建新主题时产生 `memory_id`。在旧 Memory 中继续对话或发起补充研究时，`memory_id` 保持不变，但可以产生新的 `session_id` 和 `thread_id`。

### 2.3 不改变 Research AgentGraph

LLM Wiki 不新增 Wiki Agent、Memory Manager Agent 或专用角色树。

- 普通记忆问答是一次“检索 Markdown → policy 回答”，不进入 Research AgentGraph；
- 需要新资料或深度研究时，才进入现有 Research Workflow；
- 从 Memory 发起研究仍必须先生成 Research Brief 并由用户确认；
- 根、子、孙 Research Agent 继续使用同一个同质 AgentGraph。

## 3. Vault 文件契约

```text
PaperPilotVault/
├── Memories/
│   ├── M-<stable-id>/
│   │   ├── Home.md
│   │   ├── reports/
│   │   ├── evidence/
│   │   ├── sources/
│   │   ├── notes/
│   │   ├── imports/
│   │   └── attachments/
│   └── ...
├── Inbox/
└── .obsidian/          # 由 Obsidian 管理，PaperPilot 不改写插件配置
```

### 3.1 `Home.md`

`Home.md` 是 Memory 的入口和人类可读索引，至少包含：

- Memory 标题与目标；
- 已有报告、笔记和导入资料的 WikiLink；
- 当前已知结论；
- 未解问题；
- 最近一次更新时间。

`Home.md` 不是隐藏 manifest。它本身就是可在 Obsidian 中阅读和编辑的 Markdown 笔记。

### 3.2 笔记类型

| 目录 | 内容 | 写入规则 |
|---|---|---|
| `reports/` | Research Workflow 最终报告 | PaperPilot 生成；用户可在 Obsidian 中编辑 |
| `evidence/` | 带 locator 的证据笔记 | 只能由研究或受控导入生成 |
| `sources/` | 来源元数据与原始引用 | 不由普通对话修改 |
| `notes/` | 用户笔记、对话保存和 LLM 整理结果 | 新建默认需用户确认 |
| `imports/` | 导入资料的结构化提取结果 | 确认后写入 |
| `attachments/` | PDF、图片或其他原始附件 | 保留原始文件与内容哈希 |

### 3.3 Frontmatter

所有 PaperPilot 管理的 Markdown 使用平面 YAML Properties，不使用嵌套对象：

```yaml
---
id: "N-stable-id"
type: "note"
memory_id: "M-stable-id"
title: "Example note"
created_at: "2026-08-28T12:00:00+08:00"
updated_at: "2026-08-28T12:00:00+08:00"
origin: "user | research | import | conversation"
status: "draft | confirmed"
tags:
  - paperpilot
---
```

`id` 和 `memory_id` 一旦写入不得因重命名而改变。用户可编辑标题和正文，PaperPilot 必须把外部修改视为最新事实。

### 3.4 WikiLink

- 新链接使用 Vault 根目录相对路径，例如 `[[Memories/M-abc/evidence/E-123|Evidence]]`；
- 不生成只依赖文件名的歧义链接；
- 可以跨 Memory 链接，但必须显式指向目标 Memory 路径；
- Backlink 不单独持久化，由 Obsidian 或 PaperPilot 从 WikiLink 反向计算。

## 4. 记忆检索

### 4.1 扫描范围

每次对话必须明确一个记忆范围：

- 当前 Memory；
- 用户显式选择的多个 Memory；
- 全部 Memory。

默认只检索当前 Memory，避免无关项目污染上下文。

### 4.2 索引边界

第一版只使用：

- Frontmatter 字段；
- 文件名和标题；
- Markdown 全文关键词；
- WikiLink 出边和反向引用；
- 文件修改时间与内容哈希。

索引可以是内存结构或可删除缓存，但必须可从 Vault 完全重建。本阶段不引入向量数据库、图数据库或第二套知识库。

## 5. 对话和写入语义

### 5.1 回答

Memory 对话使用检索到的 Markdown 片段作为上下文。回答中的知识性结论必须指向具体 WikiLink；找不到支持时必须明确说明。

普通回答不自动写入 Memory。

### 5.2 保存为笔记

用户选择“保存为笔记”后：

1. policy 生成完整 Markdown 提案、目标路径和 WikiLink；
2. PaperPilot 校验目标 Memory、路径、frontmatter 和链接；
3. 前端展示预览；
4. 用户确认后原子新建 `notes/*.md`；
5. 更新 `Home.md` 的链接时必须使用同一个受控变更。

第一版只新建笔记，不让 LLM 自动批量覆盖已有笔记。

### 5.3 外部编辑与并发

- PaperPilot 在生成变更提案时记录目标文件内容哈希；
- 确认写入前重新计算哈希；
- 如果用户已在 Obsidian 修改文件，拒绝覆盖并重新生成提案；
- 新文件使用稳定 ID 和原子写入，不通过文件名猜测同一性。

## 6. 从 Memory 继续研究

用户可以选择当前 Memory 并发起新问题。PaperPilot 先检索相关旧笔记，再由根 Research Agent 产生 Research Brief。

Research Brief 必须告诉用户：

- 本次使用了哪些旧记忆；
- 哪些是已知内容；
- 哪些是需要新研究的空白；
- 新结果将写入哪个 `memory_id`。

用户确认后，现有同质 Research AgentGraph 执行研究。新报告、证据和来源写入同一 Memory，不覆盖旧报告。

## 7. Obsidian 集成

### 7.1 首次配置

- 用户在 Obsidian 中把 `PaperPilotVault/` 打开为 Vault；
- PaperPilot 配置中只保存 Vault 根路径和可选 Vault 名称；
- PaperPilot 不自动安装 Obsidian 插件，不写入 `.obsidian/` 配置。

### 7.2 打开笔记

前端的“在 Obsidian 中打开”使用标准 `obsidian://open` URI，定位到指定 Memory 的 `Home.md` 或具体笔记。

Obsidian URI 只用于打开，不用于绕过 PaperPilot 的校验直接追加内容。PaperPilot 仍直接、原子地写入 Markdown 文件。

## 8. 最小前端

现有研究界面只增加：

- Memory 选择器；
- “新建 Memory”；
- “继续此 Memory 对话”；
- “保存回答为笔记”；
- “基于此 Memory 发起研究”；
- “迁移既有 Memory”；
- “在 Obsidian 中打开”。

前端不实现文件树、Markdown 编辑器、Backlink 面板、图谱或 PDF 阅读器。

## 9. 现有数据兼容

当前 Vault 根目录下的 `reports/`、`evidence/` 和 `sources/` 不自动搬移，避免破坏已存在的 `MemoryManifest` 和 Chat Store 引用。

W6 将其作为虚拟只读 `M-legacy` 暴露：可以在当前根目录范围内检索和问答，不能研究、保存笔记、导入或接受任何 managed 写入。新建 Memory 使用 `Memories/M-<id>/` 契约，CLI/Web 写入入口必须显式选择一个 managed Memory。

W6 的迁移是显式 copy-on-publish：先生成包含目标 Home 和每篇转换后 Markdown 的完整零写预览；确认时复核源快照、在同卷隐藏 staging 中完整生成和校验，再以一次目录 rename 发布新的 `Memories/M-<id>/`。任一失败都清理 staging 且不产生可见目标。legacy 根文件永远不移动、不删除、不改写，因此旧 `MemoryManifest` 和 Chat Store 指针仍可读取；迁移成功后当前会话也不会自动切换到新 Memory。

这不是跨 SQLite 与 Vault 的全局事务，不引入旧指针批量重写、cross-process lock、journal 或 bundle。多个 PaperPilot 进程同时写同一 Vault 不在本主线保证范围。

## 10. 完整闭环

```text
新建或选择 Memory
→ PaperPilot 扫描当前 Memory Markdown
→ 检索相关旧笔记
→ 带 WikiLink 回答
→ 用户选择：
   ├── 不保存
   ├── 确认后新建 Markdown 笔记
   └── 经 Research Brief 确认后发起补充研究
→ 新结果写回同一 Memory
→ Obsidian 自动看到外部文件变化
→ 用户在 Obsidian 阅读、编辑或新建 Markdown
→ 下次对话重新扫描并使用最新文件
```

## 11. 明确不做

- 内置复杂 Markdown/PDF 阅读器；
- 默认每次研究新建一个 Obsidian Vault；
- 嵌套 Vault；
- 图数据库、Evidence Graph 或单独持久化的 backlink 边；
- 第一版引入向量数据库；
- 每次聊天都自动写入 Memory；
- 没有用户确认的 LLM 批量重写；
- 新的 Wiki Agent、Memory Manager Agent 或第二套 AgentGraph；
- 绕过现有 Research Brief 确认直接发起研究。

## 12. Obsidian 能力依据

- [How Obsidian stores data](https://obsidian.md/help/data-storage)
- [Create a vault](https://obsidian.md/help/vault)
- [Internal links](https://obsidian.md/help/links)
- [Backlinks](https://obsidian.md/help/plugins/backlinks)
- [Properties](https://obsidian.md/help/properties)
- [Obsidian URI](https://obsidian.md/help/uri)

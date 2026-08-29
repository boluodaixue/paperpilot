# Memory / Vault 契约

## 设计目标

PaperPilot 使用一个 Markdown Vault 承载多个长期 Memory。契约必须让 PaperPilot、Obsidian、Chat 会话、LangGraph thread 和派生索引引用同一份知识，同时避免标题修改、路径拼接和跨 Memory 链接破坏身份。

## 长期身份

`memory_id` 使用稳定的 `M-<stable-id>`，不复用生命周期更短的 `session_id` 或 `thread_id`。规范目录只能由 ID 推导：

```text
Memories/M-<stable-id>/
```

标题和文件名可以改变，但不能改变 `memory_id`。最小 `MemoryDescriptor` 只包含：

- `memory_id`
- `title`
- `relative_path`
- `created_at`
- `updated_at`

重复或非法 ID、非规范目录和时间倒序都会被确定性拒绝。

## Markdown 与 frontmatter

PaperPilot 管理的 Markdown 使用平面 YAML Properties。稳定 ID、Memory ID、文档类型、标题、带时区时间、状态、来源和字符串 tags 在写入前经过校验。

Markdown 正文是知识内容的唯一真相源。Chat Store 只保存消息和文件指针；SQLite 索引、embedding cache 和 Runtime Registry 都不能替代或反向覆盖 Markdown。

## 路径契约

Vault 路径只接受正斜杠分隔的相对路径，拒绝：

- 绝对路径、盘符和 UNC；
- `.`、`..` 与控制字符；
- Windows ADS、设备名和尾随空格/点；
- 非预期文件后缀；
- symlink、junction、reparse 或解析后逃逸 Vault 的路径。

调用方不能把 Obsidian URI、用户输入或模型文本直接当作文件系统路径。

## WikiLink 契约

PaperPilot 生成的链接使用无扩展名的 Vault 根相对完整路径：

```markdown
[[Memories/M-.../evidence/E-...]]
```

裸文件名、歧义路径、跨 Memory 写入和 WikiLink 语法注入都被拒绝。报告链接证据，证据链接来源；Obsidian backlinks 提供反向关系，不建立独立 Evidence Graph。

## 目录结构

```text
Memories/M-.../
├── Home.md
├── reports/
├── evidence/
├── sources/
├── notes/
├── imports/
└── attachments/
```

创建 Memory、研究持久化、问答笔记和资料导入都复用同一套身份、路径、frontmatter 和 WikiLink 校验。

## 关键不变量

1. `memory_id` 决定规范目录，标题不能反向改变身份；
2. 所有 managed 写入必须留在选定 Memory；
3. 所有引用最终都能解析到真实 Vault 文件；
4. 外部编辑不能被静默覆盖；
5. 派生数据库可以删除，Markdown 仍能独立恢复知识和索引。

# W1：多 Memory 持久化

## 状态

已完成（2026-08-28）。W1 只实现同一 Markdown Vault 内的 Memory 创建、列出、选择与研究持久化，没有开始 W2 的 Obsidian 接入。

## 目标与边界

W1 在 W0 安全契约之上扩展原有 `MarkdownMemoryStore`，让一个 Vault 可以包含多个长期 Memory，并让现有 Research Workflow 把结果写入用户明确选择的 `memory_id`。

本阶段没有新增 Repository、服务、数据库、索引或 Agent 角色。Research AgentGraph、fork policy、递归上限、checkpointer 和 N6 Red/Blue 仍使用原实现。

## 已完成

- `MarkdownMemoryStore` 增加 `create_memory`、`get_memory` 和 `list_memories`，每次直接读取 Vault 中最新的 `Home.md`，不维护第二份索引；
- 新建 Memory 使用同一 `Memories/` 目录下的 staging 目录完整生成 `Home.md` 和 `reports/evidence/sources/notes/imports/attachments`，再原子 rename 为 `Memories/M-<id>/`；
- `Home.md` 使用 W0 平面 frontmatter，并提供目标、报告、笔记、导入、已知结论、未解问题和最近更新时间的初始人类可读结构；
- 标题在 Obsidian 中修改后，下一次 `get/list` 读取最新标题，但 `memory_id` 和规范目录保持不变；
- Research Workflow state、checkpoint 和最终 `ResearchWorkflowResult` 显式携带可选 `memory_id`；缺失或非法 Memory 在任何 policy/tool 调用前被拒绝；
- `ResearchRuntime` 提供创建、列出、读取和选择 Memory 的薄入口，没有增加全局“当前 Memory”状态；
- 指定 Memory 的报告、证据和来源写入 `Memories/M-<id>/` 对应子目录，并使用完整 Vault 根相对 WikiLink；
- 新管理笔记使用 W0 完整 frontmatter；legacy 根目录输出保持原 frontmatter、路径和短 WikiLink，不在 W1 中迁移；
- 同一 Memory 的不同根 `thread_id` 生成不同报告路径，因此多次研究保留历史；证据和来源仍按稳定 ID 在该 Memory 内复用；
- managed note ID 使用独立安全归一化，不改变 legacy 文件名；不安全来源标题不会进入 WikiLink alias；
- N6 `replace_report` 同时支持 legacy `reports/*.md` 与 managed `Memories/M-.../reports/*.md`，完整 WikiLink 可通过原 manifest 保护；
- Web 后端可把已存在的 `memory_id` 传给同一 Runtime；同一待确认任务不能改选 Memory；
- Chat 报告消息只保存 `memory_id`、执行引用和 manifest 指针，不复制报告 Markdown；读取历史时仍从 Vault 最新文件展开；
- `memory_id=None` 是明确的 legacy 兼容入口，旧 N2/N5/N6 调用和覆盖 `persist_research(brief, result, identity)` 的 subclass 不需要修改。

## 并发与失败语义

- 同一 Memory ID 的并发创建只有一个原子 rename 成功，其余调用确定性返回重复错误；
- staging 目录不会被 `list_memories` 暴露，正常失败会清理；
- 多 Store 实例并发向同一 Memory 写入不同研究时，报告路径按根线程隔离，单个 Markdown 文件继续使用原子替换，不产生 `.tmp` 或半文件；
- `Memories/` 或目标 Memory 通过 symlink/junction 逃逸 Vault 时，创建、读取和列出均拒绝；
- W1 只创建 `Home.md` 初始索引，不在研究持久化时自动覆盖用户编辑的 Home。后续受控 Home 变更仍按 W4 的确认与并发保护边界实施。

## 明确未实现

- W2：Obsidian URI、Memory 选择器、新建按钮、CLI 展示或 `.obsidian/` 写入；
- W3：Markdown 全文扫描、关键词/WikiLink 索引、旧 Memory 注入 Research Brief 或继续研究语义；
- W4：Memory 问答、保存回答为笔记、受控更新 Home 或内容哈希冲突处理；
- W5：PDF/网页/文本导入、attachments 或 imports 写入；
- W6：legacy 文件迁移、迁移入口、跨入口默认收口或 tracing 扩展；
- 向量数据库、图数据库、第二套知识存储或新的 AgentGraph。

## 验收结果

- W1 专项：`19 passed, 1 warning`；
- 原 W0 + N1–N6 回归：`240 passed, 1 warning`；
- 包含 W1 的仓库全量回归：`259 passed, 1 warning`；
- warning 仍是既有 `StarletteDeprecationWarning`；
- `compileall`、导入检查、架构范围扫描和 `git diff --check` 通过。

W1 正式完成。W2 尚未开始。

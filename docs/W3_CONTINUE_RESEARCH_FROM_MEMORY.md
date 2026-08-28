# W3：基于旧 Memory 继续研究

## 状态

已完成（2026-08-28）。W3 只让当前 Memory 的旧 Markdown 进入 Research Brief 和后续同质研究上下文，没有开始 W4 的 Memory 问答或保存笔记。

## 目标与边界

W3 在 W1 的多 Memory 路由和 W2 的选择入口之上，让用户对同一长期 Memory 发起增量研究。PaperPilot 在生成根 Research Brief 前确定性筛选旧笔记，向用户说明使用了哪些文件、哪些信息已知、哪些仍需研究；确认后仍由原 Research Workflow 执行并写回同一 Memory。

本阶段没有新增持久化数据库、Repository、服务或 Agent 角色。Research AgentGraph、fork policy、递归上限、checkpointer、Markdown Memory Store、Chat Store 和可选 N6 Red/Blue 均沿用原实现。

## 已完成

- 新增 `MarkdownMemoryIndex` 进程内最小索引；每次 `search` 都从当前 Markdown 重建，索引不落盘且可完全丢弃；
- 索引读取标准 PaperPilot Markdown 和用户在 Obsidian 新建的普通 Markdown，宽容缺失或非标准 frontmatter；
- 每个索引项包含 Vault 相对路径、标题、有界摘要、WikiLink、修改时间和 SHA-256 内容哈希；
- 搜索结合 frontmatter、文件名/H1、全文关键词、WikiLink 出边和反向引用，标题、路径和 frontmatter 权重高于正文；
- 英文检索过滤固定停用词，中文支持连续词与双字词；直接命中摘要围绕实际命中位置，一跳关联笔记使用开头摘要；
- 搜索结果按分数和路径稳定排序，调用上限为 10，Workflow 固定只取最多 5 项；单项摘要最多 320 字符，单项 WikiLink 也有数量与长度上限；
- 扫描范围硬限制为当前 `Memories/M-.../`，跨 Memory WikiLink 不拉入目标文件，symlink/junction 逃逸被拒绝；
- `ResearchBrief` 增加带默认值的 `memory_id`、`memory_paths`、`known_information` 和 `research_gaps`，旧构造与 legacy Brief 保持兼容；
- 对齐 policy 只收到有界的 path/title/summary/WikiLink JSON；`memory_id` 和命中文件由 PaperPilot 固定，忽略模型伪造值；
- 没有命中时 Brief 明确为空，模型不能虚构已使用的旧知识；模型省略新字段时，已知信息从命中摘要、研究空白从原 directions 保守派生；
- Brief 修改使用 checkpoint 中的同一检索快照，不在用户确认期间隐式换用另一批文件；
- CLI 与 Web 的确认界面显示只读 Memory 上下文，用户仍只编辑原有目标、范围、方向、限制和输出；
- 确认后根 ResearchTask 只接收有界命中摘要、已知信息和研究空白；`memory_id` 不进入 AgentGraph identity、任务上下文或 tracing；
- 新结果继续由 W1 外层 Workflow 写入相同 `memory_id`，不同根线程生成不同报告并保留历史；
- managed 报告记录所用 Memory 路径、已知信息和研究空白；legacy 报告与 CLI Brief 格式保持原样。

## 明确未实现

- 不跳过或自动确认 Research Brief；
- 不新建 Wiki Agent、Memory Manager、第二个 AgentGraph 或不同角色的 Research Agent；
- 不把整个 Memory 注入模型上下文；
- 不使用 embedding、向量数据库、图数据库或持久化 backlink；
- 不实现 W4 Memory 问答、无依据拒答、保存回答、笔记提案、Home 更新或哈希冲突写入；
- 不实现 W5 导入或 W6 legacy 迁移、入口收口和 `memory_id` tracing 扩展。

## 验收结果

- W3 检索、Workflow 和 Brief 展示联合专项：`22 passed`；
- 原 N1–N6 + W0–W2 回归：`293 passed, 1 warning`；
- 包含 W3 的仓库全量回归：`315 passed, 1 warning`；
- warning 仍是既有 `StarletteDeprecationWarning`；
- Python 编译、前端脚本语法、架构范围扫描和 `git diff --check` 通过。

W3 正式完成。W4 尚未开始。

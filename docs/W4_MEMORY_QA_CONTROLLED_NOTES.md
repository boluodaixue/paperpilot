# W4：Memory 问答与受控新建笔记

## 状态

已完成（2026-08-28）。W4 只实现基于当前 Memory 的只读问答，以及经用户预览和确认后新建 Markdown 笔记并更新 `Home.md`；没有开始 W5 的资料导入与整理。

## 目标与边界

W4 在 W3 可从 Markdown 重建的当前 Memory 检索之上增加直接问答。回答只使用所选 Memory 的命中笔记，不足时明确说明；用户可以把回答转换为完整 Markdown 提案，但只有确认动作可以触发写入。

Markdown Vault 仍是唯一知识真相源。Research AgentGraph、Research Workflow、fork policy、递归上限、checkpointer、Chat Store 和可选 N6 Red/Blue 均保持原有职责；W4 没有新增 AgentGraph、Repository、数据库或持久化索引。

## 已完成

- 新增最小 transient 契约 `MemoryCitation`、`MemoryAnswer` 和 `MemoryNoteProposal`，分别表达引用、当前 Memory 回答和一次待确认的完整笔记变更；
- Memory 问答复用 W3 `MarkdownMemoryIndex`，每次只搜索显式指定的 `memory_id`，其他 Memory 不进入回答上下文；
- 无命中时直接返回 `insufficient_evidence`，不调用 policy，不自动切换到网络研究；有命中时 policy 无工具调用，只能基于提供的有界 Markdown 上下文生成 claim；
- PaperPilot 只接受实际命中路径，过滤模型伪造的跨 Memory 路径和模型自行生成的 WikiLink，再为采用的引用确定性附加 Vault 根相对路径与完整 WikiLink；
- “保存回答为笔记”先生成完整 Markdown 提案；稳定 `Note-...` ID、规范 `notes/` 目标路径、允许引用路径和固定 frontmatter 由 PaperPilot 约束，policy 不能选择写入位置或扩展来源；
- 提案同时保存 `Home.md` 快照哈希、目标文件预期状态和受控 Home 更新内容；生成与校验提案均不写文件；
- `MarkdownMemoryStore.commit_memory_note` 在同一受控提交中只新建提案目标笔记并替换对应 `Home.md`，其他来源和笔记保持不变；
- 提交前校验目标路径、frontmatter、Memory 身份、来源 WikiLink、Home 路径和内容哈希；最终替换在文件系统原子点保留当时的旧 Home 并复核其哈希，目标已存在、Home 被 Obsidian 修改、重复确认或并发提交会抛出 `MemoryWriteConflictError`，不静默覆盖；
- Home 更新只修改更新时间并向唯一 `## Notes` 列表加入新 WikiLink，保留用户已有的其他 Home 内容；写回失败会移除本次新建笔记和受控临时文件；若外部进程持续写入超过有界恢复次数，PaperPilot 返回冲突并保留最新捕获版本的恢复路径，不删除未知内容；
- 两个 Store 实例基于同一快照并发确认时只有一个成功，失败方不留下笔记、断链或半成品；`notes/` symlink/junction 逃逸在写入前被拒绝；
- `ResearchRuntime` 只提供 `answer_memory`、`propose_memory_note` 和 `commit_memory_note` 三个薄入口，没有改变原研究装配；
- Web 增加“基于此 Memory 研究 / Memory 问答”明确模式；回答经元素与属性白名单安全渲染为 Markdown，并为每个引用提供安全的 Obsidian 打开 URI；
- Web 服务端只用进程内 `_MEMORY_ANSWERS` 和 `_MEMORY_NOTE_PROPOSALS` 暂存问答与提案，不写 Chat Store；Memory、answer、proposal 和 commit 返回必须严格匹配；
- 保存动作先展示完整 Markdown 预览和确认/取消；只有确认端点调用 commit，冲突确定性返回 `409`，成功后移除已消费的 proposal。

## 写入与冲突契约

普通问答和生成提案都是只读操作。提案包含生成时的 `Home.md` 内容哈希，并声明目标笔记此前不存在；确认时任一条件不再成立都会拒绝提交。PaperPilot 不把客户端提交的 Markdown 或 Home 内容当作写入真相，也不使用 Obsidian URI 写文件。

成功确认只产生两项受控变化：新建 `Memories/M-.../notes/Note-....md`，并在同一 Memory 的 `Home.md` 中加入该笔记的完整 WikiLink。失败时已有 Vault 内容保持不变。

## 明确未实现

- 不在引用不足时自动发起 Research Workflow、网络检索或工具调用；
- 不把问答或提案写入 Chat Store、数据库、持久化索引或第二套知识存储；
- 不在用户确认前创建笔记、修改 `Home.md` 或自动保存回答；
- 不提供文件树、Markdown 编辑器、内置阅读器、Backlink 面板或 Obsidian 插件；
- 不使用 `obsidian://new`、`append`、`prepend` 或客户端 Home 写入；
- 不实现 W5 的 PDF、网页、文本导入、`attachments/` / `imports/` 写入、资料去重或整理提案；
- 不实现 W6 的 legacy 迁移、入口收口、持久会话 Memory 绑定或 tracing 扩展；
- 不新增 Wiki Agent、问答 Agent、向量数据库、图数据库或独立问答服务。

## 验收映射

- **引用可打开**：回答 DTO 只暴露已验证的 Vault 相对引用，并为每项引用生成经过 W0 路径校验的 `obsidian_uri`；
- **普通问答零写入**：问答前后 Vault 文件快照一致，Web 不创建 ResearchTask，也不写 Chat Store；
- **未确认提案零写入**：完整 Markdown、目标路径、Home 变更和哈希只存在于 transient proposal，目标文件与 Home 在确认前不变；
- **确认后最小写入**：成功只新建目标 note 并更新对应 Home 的唯一 Notes 链接，来源文件不变；
- **外部修改拒绝覆盖**：Home、目标文件、最终哈希与原子替换之间的修改、恢复期间二次或持续修改、重复确认和并发确认均有冲突、恢复或失效保护测试；
- **路径与 Markdown 安全**：非法路径、frontmatter、跨 Memory WikiLink、畸形链接、非法 tags/时间和 symlink/junction 逃逸均在写入前拒绝；
- **Web 用户确认**：问答、提案预览、取消、确认、严格 ID 匹配、`409` 冲突、安全 Markdown 渲染和前端 JavaScript 语法均有确定性测试。

## 验收结果

- W4 专项：`35 passed, 1 warning`；
- 原 N1–N6 + W0–W3 前序回归：`315 passed, 1 warning`；
- 包含 W4 的仓库全量回归：`350 passed, 1 warning`；
- warning 为既有 `StarletteDeprecationWarning`。
- Python 编译、前端脚本语法、架构范围扫描和 `git diff --check` 通过。

W4 正式完成。W5 尚未开始。

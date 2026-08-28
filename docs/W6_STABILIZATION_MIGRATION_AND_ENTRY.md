# W6：稳定化、迁移与入口收口

## 状态

已完成（2026-08-28）。W6 将 W0–W5 的 Memory 闭环收口到共享 Runtime、CLI 和 Web 入口，增加既有根目录 Memory 的只读问答与显式迁移，并补齐可观测性和固定离线评测。没有改变 Research AgentGraph、Research Workflow、fork policy、递归上限、checkpointer 或 N6 可选 Red/Blue。

## 目标与边界

W6 复用唯一 `ResearchRuntime` 和现有 `MarkdownMemoryStore`。真实 CLI/Web 写入入口必须先显式选择一个可写 managed Memory；根目录 `reports/`、`evidence/`、`sources/` 只作为虚拟 `M-legacy` 被读取和迁移，不再作为默认产品写入目标。历史低层 `memory_id=None` 调用仍由 Workflow/Store 保持兼容，用于 N1–N6 与旧 manifest 的读取和回归，但 W6 用户入口不暴露它作为可写选择。

Markdown Vault 仍是唯一知识真相源。检索索引每次从 Markdown 重建，Chat Store 只保存会话绑定和 manifest 指针；迁移提案只在当前进程内短暂保存。W6 没有新增领域数据模型、Service、Repository、持久化索引、数据库知识副本、Agent 角色或自动写入行为。

## 已完成

- `ResearchRuntime` 成为 CLI/Web 的共同 Memory 装配入口，统一提供 managed Memory 与虚拟 `M-legacy` 的列出、选择、问答、受控写入和迁移薄接口；
- `session_meta` 只新增 nullable `memory_id`。会话第一次显式使用 Memory 时事务绑定；同值重复绑定幂等，之后切换到其他 Memory 返回冲突。Web 直接读取该绑定，不再从最后一份报告推断；Memory 暂时不可读取时仍保留原 ID，不静默切换；
- Web 与交互式 CLI 均支持列出/选择/新建 Memory、当前 Memory 问答、完整笔记预览与确认、file/text/URL 导入预览与确认、基于同一 Memory 的补充研究；单次 CLI 也要求显式 `--memory-id`；
- `M-legacy` 只允许安全扫描和问答；研究、保存笔记、导入和任何 managed 写入都被拒绝。Web/CLI 提供显式迁移入口，迁移完成后当前会话仍保持 `M-legacy`，用户必须主动选择新 Memory；
- legacy 扫描只读取 Vault 根目录 `reports/`、`evidence/`、`sources/` 下的 UTF-8 Markdown，拒绝路径逃逸、symlink、junction 和 reparse point，不读取其他目录或非 Markdown 文件；
- 迁移 preview 是完整、零写入提案：包含目标 `Home.md`、每个源/目标路径、完整转换后 Markdown、frontmatter、WikiLink 和源内容哈希；用户确认前不创建 `Memories/`；
- 确认时重新复核源快照和完整提案，在同一 `Memories/` 文件系统内生成并校验隐藏 staging 目录，再以一次目录 rename 发布完整 managed Memory。发布前规范目标不存在，发布后 Home 与所有文件同时可见；任一 staging、复核或 rename 失败都清理本次 staging；
- 迁移采用 copy-on-publish：legacy 根文件永远不移动、不删除、不改写，旧 ChatStore manifest 迁移前后仍可展开，SQLite 指针不重写。目标 managed Memory 发布后由用户显式切换使用；
- Obsidian 外部编辑会在下一次检索中重新读取；受控笔记、导入和迁移若发现提案后的文件变化会拒绝覆盖并保留外部内容。Obsidian 未安装或未打开不影响 Markdown 持久化；PaperPilot 不检测/启动 Obsidian，也不写 `.obsidian/`；
- Langfuse 上下文补齐 `memory_id`、实际检索文件与分数，以及研究、笔记、导入和迁移的路径/状态结果；自定义 trace 不上传问题、回答、Markdown 正文、摘要或附件字节；
- 新增固定离线 `memory_wiki` 评测，分别验证检索命中、引用完整、无依据拒答、受控写入和继续研究。评测只使用临时 Vault、脚本 policy 和固定工具，不构建真实模型 Runtime，也不访问网络。

## 显式迁移语义

W6 的“原子切换”是新 managed Memory 的单目录发布点，不是跨 Chat Store 与 Vault 的全局事务，也不是把旧目录物理移动到新目录。这样可以同时满足：

- 新目标不会出现半成品；
- 失败时没有可见目标，legacy 源仍完整；
- 旧 manifest 和历史会话永不因迁移失效；
- 用户可以检查完整预览，再决定是否显式切换到新 Memory。

源在成功后仍作为只读历史快照存在。W6 不增加跨进程锁、journal、bundle、SQLite/Vault 两阶段提交或旧指针批量重写；多个 PaperPilot 进程同时写同一 Vault 仍不在保证范围。

## 完整闭环验收

- **选择与固定身份**：Web/CLI 必须先选择 managed Memory；Web session 持久显示同一 `memory_id`，问答、笔记、导入、研究及恢复均拒绝跨 Memory；
- **问答与受控保存**：回答只采用当前 Memory 的实际命中路径并附完整 WikiLink；保存前展示完整 Markdown，取消零写入，确认后只按 W4 契约新建 Note 并更新 Home；
- **外部编辑后继续**：测试在确认笔记后模拟 Obsidian 写入新 token，下一次问答与补充 Research Brief 均重新扫描到该编辑，并把新研究结果写回相同 `memory_id`；
- **legacy 数据安全**：preview、成功、源冲突、staging 失败、发布失败和 staging 期间外部编辑均有字节级快照测试；旧 Chat manifest 与 SQLite 原记录迁移前后不变；
- **无内置阅读器依赖**：W6 没有增加文件树、Markdown/PDF 编辑器、Backlink 面板、图谱或 Obsidian 插件；闭环通过标准 Obsidian URI 和 Vault 文件完成；
- **架构保持**：AgentGraph、fork policy、递归、checkpointer、Workflow 状态、报告审查开关和 Red/Blue 行为均未扩展。

## 明确未实现

- 不移动或删除 legacy 根文件，不自动改写旧 Chat/manifest 指针；
- 不自动把迁移后的 Memory 绑定到当前或历史会话；
- 不建立迁移 Repository、持久提案表、文件监控、同步服务或后台自动迁移；
- 不引入 cross-process lock、journal、bundle 或 crash-recovery 协议；
- 不新增 Memory/Wiki/Migration Agent、第二套 AgentGraph、向量数据库、图数据库或持久检索索引；
- 不实现内置 Markdown/PDF 阅读器、编辑器、文件树、Backlink/图谱面板或 Obsidian 插件；
- 不绕过 Research Brief 确认，不让普通问答或未确认提案自动写入。

## 验收结果

- W6 专项：`39 passed, 1 warning`；
- 原 N1–N6 回归：`160 passed, 1 warning`；
- N1–N6 + W0–W5 前序集合：`411 passed, 1 warning`；
- 包含 W6 的仓库全量回归：`450 passed, 1 warning`；
- 固定离线 `memory_wiki` 评测：`5/5 passed`，`pass_rate = 1.0`；
- warning 为既有 `StarletteDeprecationWarning`。离线评测另有 LangGraph 对未注册 checkpoint 类型的未来兼容警告，不影响本次结果。

W6 正式完成。LLM Wiki + Obsidian 的 W0–W6 主线已按既定计划全部完成；没有开始计划外的新阶段。

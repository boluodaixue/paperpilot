# PaperPilot S 系列生产化与检索升级实施计划

## 1. 状态与边界

本计划于 2026-08-28 确认，是 N0–N6 与 W0–W6 全部完成后的下一条独立主线。S0–S5 尚未开始实现；每个阶段必须在上一阶段完成专项、前序与全量回归并单独提交后才能开始。

W6 的提交 `b8e5e1c` 是本计划的冻结基线。W0–W6 的实施记录继续描述当时已经交付的行为；S 系列只通过新的阶段记录改变未来行为，不回写历史完成结论。

必须持续保留：

- 唯一 `ResearchRuntime`、Research Workflow 与同质 Research AgentGraph；
- 根、子、孙的执行身份、fork policy、递归上限和全部硬预算；
- LangGraph interrupt/`Command(resume=...)` 用户确认语义；
- Markdown Vault 作为唯一长期知识真相源；
- Chat Store 只保存会话、消息、绑定和知识文件指针，不复制 Markdown 正文；
- N6 默认关闭的可选单次 Red/Blue；
- Obsidian 作为外部阅读/编辑器，PaperPilot 不写 `.obsidian/`。

S 系列不新增 Manager、Planner、Summarizer、Wiki Agent、Review Agent 或其他固定 Agent 角色，不恢复旧 Orchestrator/DAG/AgentPool/Evidence Graph，也不把派生索引提升为第二知识真相源。

## 2. 阶段总览

| 阶段 | 目标 | 产品结果 |
|---|---|---|
| S0 | 本地文件读取沙箱 | Research 工具只能读取明确授权的 Vault/上传范围 |
| S1 | 持久化工作流状态与确认 | 重启后可定位并恢复任务，LangGraph State 是唯一工作流状态 |
| S2 | 单一 Vault Writer 与崩溃一致性 | 所有产品写入串行、幂等、可恢复，不出现可见半成品 |
| S3 | Legacy 安全退役 | 迁移后活动 Vault 只保留 managed Memory，历史指针在当前版本仍可解析 |
| S4 | 持久化全文检索 | 从 Markdown 增量构建可丢弃的 FTS5 索引 |
| S5 | 可选语义与混合检索 | 在严格 Memory 范围内融合关键词、语义和 WikiLink 召回 |

## 3. 全局数据职责

S 系列固定以下职责，禁止在后续实现中复制状态：

```text
LangGraph State + AsyncSqliteSaver
  = 单个 thread 的工作流唯一真实状态、interrupt、恢复和结果

PaperPilot Runtime Registry
  = session/task/thread 查找、恢复调度租约和必要事件 outbox

Markdown Vault
  = 报告、证据、来源、笔记、导入和附件的唯一知识真相源

Derived Search Index
  = 可删除、可重建、不得反向覆盖 Markdown 的检索缓存
```

Runtime Registry 不得保存 Research Brief、提案正文、回答正文、工作流当前节点或第二份成功/失败状态。可以持久化的最小字段仅限：

- `task_id`、`thread_id`、`session_id`、`memory_id`、`workflow_type`；
- `created_at`、`expires_at`；
- 恢复调度所需的 `lease_owner`、`lease_until`；
- 必须可靠交付的事件 `event_id`、`thread_id`、`sequence`、`event_type` 和最小 payload。

任务真实阶段必须通过同一 `thread_id` 的 checkpoint 推导。SSE token/进度片段默认不永久进入图状态；断线重连先返回 checkpoint 导出的当前快照，再继续实时流。只有确认、完成和失败等必须可靠交付的事件进入 outbox。

## 4. S0：本地文件读取沙箱

### 目标

消除 `FileReaderTool(allowed_base_dir=None)` 的无限制读取面，并修复字符串路径前缀不能表达目录归属的问题。

### 工作

- 产品入口不再允许 `None` 表示无限制本地读取；没有授权目录时不装配 `file_reader`；
- 每次运行只绑定当前 managed Memory 和本次任务受控上传目录；
- 使用 `Path.resolve()` 与 `Path.relative_to()` 判断真实归属，不使用字符串 `startswith`；
- 拒绝绝对路径注入、`..`、symlink、junction、reparse point 和竞态逃逸；
- 限制允许扩展名、单文件字节数、单次读取量和文本解码；
- 返回 Vault/上传根相对安全路径，不把机器绝对路径提供给模型；
- 工具被禁用或拒绝时保持研究失败语义可解释，不自动扩大授权范围。

### 不做

- 不监视用户文件系统；
- 不允许模型自行选择新的读取根；
- 不改变网页、arXiv、计算器和 Notepad 工具协议；
- 不改变 AgentGraph、fork 或递归。

### 验收

- 正常读取当前 Memory 与本次上传文件；
- Vault 邻接目录前缀欺骗、符号链接/联接逃逸、TOCTOU 替换、超大文件和非法扩展名均被拒绝；
- CLI/Web/评测不会装配无限制 FileReader；
- N1–N6 与 W0–W6 全量回归通过。

## 5. S1：持久化工作流状态与确认

### 目标

使用 `AsyncSqliteSaver` 让所有需要暂停/确认的产品 Workflow 跨服务重启恢复，同时保持 LangGraph State 为唯一工作流状态，避免 Runtime Registry 形成第二状态机。

### 工作

- 增加 `langgraph-checkpoint-sqlite` 运行依赖，在 FastAPI lifespan 中创建并关闭唯一 `AsyncSqliteSaver`；
- CLI/Web/评测继续通过 `ResearchRuntime` 注入同一 checkpointer 契约，生产 Web 不再默认使用 `InMemorySaver`；
- 研究确认继续使用现有 Research Workflow 的 interrupt；
- 将 Memory 保存笔记、受控导入和 legacy 迁移分别包入最小 LangGraph Workflow，提案、来源快照、内容哈希、确认和最终路径只保存在各自 State/checkpoint；
- 用 `Command(resume=...)` 确认或取消，不再依赖 `_MEMORY_ANSWERS`、`_MEMORY_NOTE_PROPOSALS`、`_MEMORY_IMPORT_PROPOSALS`、`_LEGACY_MIGRATION_PROPOSALS` 等进程内提案字典；
- 建立薄 Runtime Registry，只记录 session/task/thread 映射、workflow 类型、过期时间和恢复租约；
- 启动时扫描未终结 registry 记录，通过 checkpoint 判断等待、完成、失败或需要重新调度；
- 提案 TTL 到期时将对应 Workflow 明确终结为 expired；删除 session 时取消其未终结 Workflow 并删除/失效 registry 与 outbox；
- 对确认、完成、失败建立最小事件 outbox；普通 SSE 进度在重连时由 checkpoint 快照替代；
- checkpoint 数据库与现有 Chat Store 可以位于同一 SQLite 文件，但表、迁移和职责必须隔离。

### 明确的数据唯一性

- Workflow State 是任务阶段、提案内容、确认内容和结果的唯一真实来源；
- Runtime Registry 不复制 Workflow State，也不能在 checkpoint 不存在时伪造任务状态；
- Markdown 正文与附件字节不进入 registry/outbox；
- Chat Store 不承担 Workflow 恢复；
- `memory_id`、`session_id`、`thread_id` 继续保持不同生命周期。

### 恢复与幂等边界

- 相同 `thread_id` 从最后成功 super-step 或 interrupt 恢复；
- 已完成的工具节点不得因 Web 重启而重复执行；
- 可能产生外部副作用的节点必须使用稳定幂等键，并在重试前查询已提交结果；
- Web 重启后能够列出任务、恢复等待确认界面并继续同一 thread；
- checkpoint 不等于自动调度，重新调用由 registry 租约驱动。

### 本阶段明确不做

- 不实现多进程 Vault 写入；
- 不增加 journal、bundle 或持久化检索索引；
- 不改变 legacy 文件是否保留；
- 不处理 LangGraph 自定义类型的未来 serializer 警告；该项明确延期，不作为 S1 验收阻塞；
- 不把每个 token/SSE 片段写入 checkpoint。

### 验收

- 在 Research Brief、保存笔记、导入和迁移确认点分别终止进程，重启后仍显示同一提案并可继续；
- 在图节点之间终止进程后从最后成功 checkpoint 继续，已完成工具不重复；
- 重复确认、并发确认、过期确认、跨 session/Memory 确认全部拒绝；
- session 删除后不存在可恢复的孤立提案；
- Runtime Registry 与 checkpoint 故意制造不一致时能够确定性修复或标记失败，不产生第二真相；
- 当前阶段专项、S0、N1–N6、W0–W6 和仓库全量回归通过。

## 6. S2：单一 Vault Writer 与崩溃一致性

### 目标

所有产品级 managed Vault 写入只通过一个持久化队列和单一 Writer 发布，使多 API/Research worker 不直接竞争 Markdown 文件，并使多文件写入在进程崩溃后可恢复。

### 工作

- 建立持久化 `vault_write_jobs` 队列；生产入口不能绕过队列直接写 Vault；
- 单个 Vault Writer 获取带期限的唯一 writer lease，并按 `memory_id` 串行执行；
- 写入命令包含稳定 `job_id`、幂等键、操作类型、目标 Memory、输入/目标哈希和预期 Home 哈希；
- 研究结果、保存笔记、导入、Home 更新、报告审查写回和后续 legacy 提交统一进入 Writer；
- 每组写入先在同卷隐藏 staging 完整生成并校验；
- 使用 journal/commit marker 记录准备、发布和完成状态；
- 以原子 replace/rename 建立清晰线性化点；进程启动时完成可安全重放的提交，或清理未发布 staging；
- 继续复核 Obsidian 外部编辑的内容哈希；数据库租约不能替代文件内容冲突检测；
- CLI 同步等待对应 job 的终态，Web 通过 outbox/SSE 接收终态。

### 不做

- 不允许多个 Vault Writer 同时持有写权限；
- 不以跨进程文件锁单独冒充多文件事务；
- 不自动覆盖 Obsidian 外部修改；
- 不改变 Markdown/frontmatter/WikiLink 契约。

### 验收

- 多个 API worker 并发提交时只有一个 Writer 发布，且同一幂等键只产生一组文件；
- 在 staging、逐文件发布、Home 线性化点和完成标记前后分别强制终止进程，恢复后只能看到完整旧状态或完整新状态；
- Writer lease 过期接管不会重复发布；
- 外部编辑冲突保留用户最新内容并明确失败；
- 当前阶段专项、S0–S1 和全部历史回归通过。

## 7. S3：Legacy 安全退役

### 目标

改变 W6 的长期兼容策略：迁移完成后，当前活动 Vault 不再同时保留根目录 legacy 与 managed 副本；当前版本仍能解析历史聊天中的旧文件指针，但旧版本兼容不再保证。

### 工作

- 迁移预览增加受影响 session、manifest、旧路径到 managed 路径的完整映射和外部归档目标；
- 复用 S2 Writer，在 managed Memory 完整发布并验证后才进入退役步骤；
- 为当前版本持久化 legacy path → managed path 映射，历史 Chat manifest 通过映射解析，不复制 Markdown 正文；
- 将绑定 `M-legacy` 的当前版本 session 显式切换到本次成功迁移的 managed Memory；
- 将根目录 `reports/evidence/sources` 移出活动 Vault，放入用户明确配置且位于 Vault 外的可恢复归档目录；
- 归档完成后重新扫描活动 Vault，必须不存在可识别的 `M-legacy`；
- 永久删除外部归档必须是另一个明确动作，迁移确认本身不执行不可恢复删除；
- 任一步失败必须使用 journal 恢复到完整 legacy 或完整 managed+archive 状态，不留下活动双份或断裂引用。

### 行为变化

- 当前版本迁移后只展示并使用 managed Memory；
- Obsidian 活动 Vault 不再索引两份内容；
- 旧 Chat 内容保留，旧文件指针由当前版本映射到 managed 路径；
- 旧版 PaperPilot 不再保证读取已移出活动 Vault 的 legacy 路径；
- 未显式确认迁移时仍保持 W6 的只读 `M-legacy` 行为。

### 验收

- 完整迁移后活动 Vault 中没有 legacy 根目录 Markdown，Obsidian 搜索只出现 managed 内容；
- 历史 Chat manifest 在当前版本中解析到正确 managed 文件；
- 迁移发起 session 与其他 `M-legacy` session 的绑定结果确定且可测试；
- 未配置安全外部归档根、归档目标冲突、源文件变化或任一恢复失败时不移走 legacy；
- 永久清理需要独立确认并有明确删除清单；
- 当前阶段专项、S0–S2 与全部历史回归通过。

## 8. S4：持久化全文检索

### 目标

用 SQLite FTS5 替代每次查询全量扫描正文的主要成本，同时保持 Markdown 是唯一真相源，索引可随时删除重建。

### 工作

- 为每个文档/分块记录 `memory_id`、Vault 相对路径、`chunk_id`、内容哈希、mtime、标题、frontmatter、正文和 WikiLink；
- 首次启动全量构建，后续按内容哈希增量新增、更新和删除；
- 查询首先强制限定显式 `memory_id`，再进行 FTS5/BM25 召回；
- 保留标题、路径、frontmatter 与 WikiLink 邻居的可解释加权；
- 返回前重新验证文件仍存在、仍属于 Memory 且内容哈希没有失效；
- Obsidian 外部编辑通过查询前增量同步和周期 reconciliation 被发现；
- 索引存放在 PaperPilot 应用数据中，不写 `.obsidian/`，也不放入 Markdown frontmatter。

### 不做

- 不引入 embedding 或向量召回；
- 不允许索引反向写 Markdown；
- 不默认跨 Memory 检索；
- 不把索引当成备份或知识 Repository。

### 验收

- 删除索引后可仅从 Vault 重建同等检索结果；
- 新建、修改、删除、重命名和外部编辑能增量收敛；
- 其他 Memory、失效路径和 symlink/junction 不进入上下文；
- 固定中英文关键词、WikiLink、无命中和大型 Vault 基准通过；
- S0–S3 与全部历史回归通过。

## 9. S5：可选语义与混合检索

### 目标

在 S4 全文检索之上增加可选的多语言语义召回，并融合 WikiLink 图邻居，提高同义表达、自然语言问题和跨笔记关联的召回质量。

### 工作

- embedding 只处理明确选定 Memory 的有界分块，并按内容哈希缓存；
- 本地 embedding 默认可用；任何外部 embedding 服务必须显式配置并提示数据边界；
- 分别执行 FTS、语义和 WikiLink 邻居召回，使用确定性 RRF/可解释权重合并；
- 在召回前和送入模型前两次校验 `memory_id`；
- 按 token 预算去重、重排和截断，引用仍只使用真实 Vault 路径；
- embedding 不可用时明确降级到 S4 FTS，不使用伪随机向量伪装语义结果；
- 跨 Memory 检索只能作为用户显式选择的范围，默认仍为当前 Memory。

### 不做

- 不建立图数据库或独立知识真相源；
- 不让模型自行扩大检索 Memory 范围；
- 不因索引命中自动写笔记、导入或研究结果；
- 不改变普通问答与补充研究的用户确认边界。

### 验收

- 固定中英文同义改写、关键词精确匹配和 WikiLink 关联集上，混合召回不低于 S4 基线并有明确指标；
- embedding 缺失、失败或版本变化时安全降级并可重建；
- 跨 Memory 数据不会在默认查询或回答上下文中泄漏；
- 每条最终引用都能回到仍然有效的 Markdown 文件和分块；
- S0–S4 与全部历史回归通过。

## 10. 每阶段统一测试顺序

1. 新增纯函数、状态机、路径和数据迁移单元测试；
2. 固定离线 Workflow/checkpoint/Writer/检索测试；
3. 进程终止、租约接管、并发、重复请求和失败注入；
4. Markdown、frontmatter、WikiLink、manifest 和历史指针完整性；
5. 当前阶段专项；
6. 已完成 S 阶段回归；
7. 原 N1–N6 回归；
8. 原 W0–W6 回归；
9. 仓库全量回归；
10. 真实模型、网络、Obsidian 和多 worker 只作为额外 smoke test，不替代确定性验收。

## 11. 执行顺序

```text
W6 冻结基线 b8e5e1c
→ S0 文件读取沙箱
→ S1 AsyncSqliteSaver + LangGraph State 唯一工作流状态
→ S2 持久化队列 + 单一 Vault Writer + journal
→ S3 Legacy 安全退役
→ S4 SQLite FTS5 增量索引
→ S5 可选语义与混合检索
```

不得跳过 S0–S3 直接上线多 worker 或开始 S4/S5。每个阶段只实现本节定义的工作；发现必须新增未定义 Agent 角色、知识 Repository、自动写入行为、默认跨 Memory 检索或第二知识真相源时，停止扩展并重新对齐。

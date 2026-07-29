# PaperPilot 实施计划

> 本计划是当前唯一有效的开发顺序和验收边界。目标架构以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准，进度以 [ROADMAP.md](ROADMAP.md) 为准。

## 1. 实施策略

采用“可运行纵切片”迁移，不围绕旧模块做角色映射：

1. 先建立同质 Research AgentGraph 的最小闭环；
2. 再接入用户确认和 Markdown Memory Store；
3. 然后增加同质并行与一层递归 fork；
4. 新路径通过验收后逐步移除旧编排和旧证据图；
5. 最后才增加可选 Red/Blue 和 LLM Wiki。

旧仓库只提供候选能力。每项复用都必须满足新架构接口，不因“已经存在”而保留旧职责。

## 2. 目标代码边界

建议把新主路径集中在一个独立包中，避免继续耦合旧 Orchestrator：

```text
src/research/
├── models.py          # 最小输入、状态、结果和证据契约
├── agent_graph.py     # 所有根/子/孙 Agent 共用的图
├── workflow.py        # 用户对齐、确认、根图启动和完成
├── fork_policy.py     # 三项 fork 条件与硬限制
├── memory.py          # Markdown Memory Store
├── rendering.py       # 报告、证据和来源 Markdown 渲染
└── runtime.py         # CLI/Web/评测共用的生产装配入口
```

目录名可以根据现有包结构微调，但不得重新拆出 Planner、Summarizer、Evidence Store、Fork Controller 或 Manager Service。

## 3. 最小契约

编码前先定义并测试以下最小概念，字段只按真实读写增加。

### Research Task

- `task_id`
- `objective`
- `context`
- `expected_output`
- `constraints`

### Execution Context

- `thread_id`
- `parent_thread_id`
- `root_thread_id`
- `depth`
- 工具调用、线程、时间和 token 硬限制

### Research Result

- `task_id`
- `status`
- `summary`
- `findings`
- `evidence`
- `unresolved`
- `child_result_refs`

### Evidence Item

- 支持的 finding
- 来源类型、标题和稳定标识
- URL、论文标识或文件位置
- 页码、章节、段落等 locator（可取得时）
- quote 或 paraphrase，并明确类型
- 局限或可信度说明

这些是 Agent 交换协议，不代表四套 Repository 或服务。持久化时统一渲染为 Markdown。

## 4. N0：文档与架构收敛

### 状态

已完成（2026-08-28）。

### 工作

- 把同质 Research AgentGraph 确立为唯一执行核心；
- 明确外层 Workflow 只负责用户确认和根任务生命周期；
- 删除重复、冲突的旧开发方案；
- 将阶段 0、0.5、旧阶段 1 和旧 LangGraph Phase 1 标记为历史；
- README、架构、实施计划和路线图使用同一术语；
- 停止继续扩展 Evidence Graph、RCS、Planner DAG、AgentPool 和旧 Orchestrator。

### 验收

- 活跃文档不存在 Manager 与子 Agent 异构、独立 Planner/Summarizer、Evidence Graph 或近期 RCS 路线；
- 文档索引能明确区分现行文档与历史记录；
- 下一次编码从 N1 开始。

## 5. N1：单个同质 Research AgentGraph

### 状态

已完成（2026-08-28）。实施记录见 [N1_HOMOGENEOUS_AGENT_GRAPH.md](N1_HOMOGENEOUS_AGENT_GRAPH.md)。

### 目标

先证明同一个图能完成“思考—行动—总结”，不实现 fork，不接旧 Planner DAG。

### 工作

- 建立最小 graph state，只保存当前任务、执行身份、必要消息、工具结果摘要、证据和完成状态；
- 实现 `think_and_plan`：理解任务、形成当前行动计划、判断缺失信息；
- 实现 `decide_next_action`：在调用工具、继续分析、总结返回之间路由；
- 接入现有搜索、论文、网页和文件工具中真正适合的适配器；
- 工具结果进入当前线程隔离的上下文，不写全局可变 Agent 状态；
- 实现本级 `synthesize`，输出统一 Research Result；
- 接入 checkpointer、Langfuse 和三个线程 ID；
- 根 Agent 与一个模拟子 Agent 都通过同一图构造函数运行，证明没有两套实现。

### 不做

- 用户确认；
- 并行或递归 fork；
- Markdown 持久化；
- 独立 Planner、Summarizer、Evidence Store；
- RCS 或 Red/Blue。

### 验收

- 固定任务可以经过至少一次工具调用得到结构化结果；
- 无工具、工具失败、达到调用上限都能确定性结束；
- 每项可用 evidence 都带来源与定位信息；
- 两个线程的消息、工具状态和 checkpoint 完全隔离；
- 同一个 AgentGraph 可以用 `depth=0` 和 `depth=1` 执行；
- 关闭或破坏 tracing 不影响研究结果。

## 6. N2：用户对齐、确认与单 Agent 纵向闭环

### 状态

已完成（2026-08-28）。实施记录见 [N2_CONFIRMATION_AND_MEMORY.md](N2_CONFIRMATION_AND_MEMORY.md)。

### 目标

实现用户可以修改研究方向、确认后才执行研究，并得到持久化 Markdown 报告的完整单 Agent 产品流程。

### 工作

- 外层 Workflow 接收用户问题，由根 Agent 生成简短研究说明：目标、范围、主要方向、限制和预期输出；
- 使用 LangGraph interrupt 暂停并等待用户确认；
- 用户修改时更新同一研究说明并再次暂停；
- 用户确认后调用 N1 的同质 AgentGraph，根执行使用 `thread_id == root_thread_id`；
- 根 Agent 根据 Research Result 生成最终报告；
- 实现单一 Markdown Memory Store；
- 将报告、采用的证据和来源渲染为 Markdown 并一次性提交；
- 报告使用 `[[Evidence-...]]`，证据使用 `[[Source-...]]`；
- 失败恢复和重复提交必须幂等。

### 不做

- fork；
- Evidence Graph 或边；
- 独立 Evidence Store；
- Web 图形化 Fork Tree；
- RCS。

### 验收

- 未确认前不会调用研究工具；
- 用户可以连续修改研究说明，恢复时保持同一根线程；
- 确认后完整执行并生成报告、证据和来源 Markdown；
- 所有 WikiLink 可解析，Obsidian 能自动显示 backlinks；
- Memory Store 中重复 ID 不产生重复文件；
- 进程在确认点或研究节点中断后可以从 checkpoint 恢复。

## 7. N3：同质并行 Fork

### 状态

已完成（2026-08-28）。实施记录见 [N3_HOMOGENEOUS_PARALLEL_FORK.md](N3_HOMOGENEOUS_PARALLEL_FORK.md)。

### 目标

让任一 Research Agent 根据统一策略 fork 同质子 Agent，先完成根到子的一层并行。

### 工作

- 实现轻量 fork policy，识别三个条件：可并行、需隔离、预计工具链深度至少三层；
- 加入硬门槛：任务必须明确、不重复、预算允许；N3 仅允许 `depth=0` 创建 `depth=1`；
- 父 Agent 只给子 Agent 传递明确任务、必要背景、期望结果和执行身份；
- 使用 LangGraph 动态并行分发，每个子任务调用与父级完全相同的 AgentGraph；
- 子 Agent 独立使用消息、scratchpad、工具和 checkpoint；
- 父 Agent 汇聚成功、失败和部分完成结果，并执行自己的 `synthesize`；
- 建立线程父子身份和 Langfuse trace 层级。

### 不做

- 孙 Agent；
- RCS；
- Fork Tree UI；
- 独立 ForkController、AgentFactory、AgentPool 或 Fork Repository。

### 验收

- 三种条件分别有固定输入测试；
- 无依赖任务并行运行，存在依赖的任务不会错误并行；
- 深链或大上下文任务即使只有一个，也可以为隔离而 fork；
- 父子 Agent 使用同一图定义和输出契约；
- 一个子 Agent 失败不丢失其他成功结果；
- 父 Agent 的最终结果明确包含已采用与未采用的子结果。

## 8. N4：一层递归、硬停止与恢复

### 状态

已完成（2026-08-28）。实施记录见 [N4_RECURSION_LIMITS_AND_RECOVERY.md](N4_RECURSION_LIMITS_AND_RECOVERY.md)。

### 目标

允许子 Agent 再 fork 一次，同时确保递归、资源和恢复行为可预测。

### 工作

- 允许 `depth=1` 的 Agent 使用与根相同的 fork policy 创建 `depth=2` 的孙 Agent；
- 在统一入口强制 `depth <= 2`，而不是只依赖 prompt；
- 孙 Agent 的 fork 请求转换为本地执行或结构化 unresolved 原因；
- 加入最大总线程数、单 Agent 子线程数、工具调用数、时间、token 和失败重试限制；
- 使用确定性任务指纹避免同一范围递归重复；
- 验证多层汇聚、部分失败、取消与 checkpoint 恢复；
- 新 Workflow 产生带层级身份的必要运行事件；实际接入 CLI/Web 与默认入口切换在 N5 一并完成。

### 完成判断

不实现 RCS。任一 Agent 只根据以下信息判断继续或返回：

- 必需任务是否完成；
- 关键 finding 是否有来源；
- 是否存在阻断当前任务的 unresolved；
- 最近行动是否仍产生有效信息；
- 是否达到任何硬限制。

根 Agent 对整个研究做最终判断，子 Agent 只对自己的任务负责。

### 验收

- 根 → 子 → 孙可执行和汇聚；
- 孙 Agent 无法继续创建线程；
- 所有硬限制都有确定性测试；
- 重复任务不会形成递归循环；
- 任一层部分失败后仍能返回可用结果和明确限制；
- 恢复后不会重复执行已经完成的工具调用或重复写入 Memory Store。

## 9. N5：迁移入口与清理旧实现

### 状态

已完成（2026-08-28）。实施记录见 [N5_ENTRY_MIGRATION_AND_LEGACY_CLEANUP.md](N5_ENTRY_MIGRATION_AND_LEGACY_CLEANUP.md)。

### 目标

让 CLI、Web 和评测使用新 Workflow，并清除不再适用的旧架构。

### 工作

- 把 CLI 和 Web 研究入口切换到新 Workflow；
- 保持用户对齐、确认、恢复和最终报告的行为一致；
- 对固定研究输入比较新旧路径的报告完整性、来源可定位率、失败行为、时间和成本；
- 确认新路径覆盖实际仍需要的能力后，删除无调用者的旧实现；
- 删除旧 Orchestrator 状态循环、Planner DAG、AgentPool、独立 Summarizer 和 Gap Analyzer；
- 删除 Evidence Store、Evidence Graph、关系边、图谱 UI 和只服务这些能力的配置；
- 清理旧术语、兼容模型和重复测试；
- 更新启动说明和配置模板。

### 复用审查

每个旧模块只允许三种结论：

- **直接复用**：接口和语义符合新架构；
- **迁移能力**：保留底层算法或工具，删除旧角色包装；
- **删除**：与新目标冲突或没有消费者。

不得以降低删除量为目标。

### 验收

- CLI 与 Web 默认只走新 LangGraph 路径；
- 代码中只有一个 Research AgentGraph；
- 没有独立 Planner/Summarizer/Evidence Graph/RCS 参与主链路；
- 全量测试、端到端测试和恢复测试通过；
- 删除 legacy 后不存在失效导入、配置和文档链接；
- 新路径的质量收益不是单纯依靠更多 token 或更长时间。

## 10. N6：可选 Red/Blue

### 状态

已完成（2026-08-28）。实施与验收结果见 [N6_OPTIONAL_REPORT_REVIEW.md](N6_OPTIONAL_REPORT_REVIEW.md)。

### 目标

在成功持久化的最终报告之上增加默认关闭、最多执行一次的报告审查，不改变研究执行核心。

### 工作

- 先按 N2 契约持久化原始最终报告和 `MemoryManifest`，N6 只在其后运行；
- 使用 `research.report_review.enabled` 开关且默认关闭；关闭时不增加 policy 调用，也不改变返回结果、manifest 或文件；
- Red 复用同一 research policy，且只调用一次；它不获得工具，只从事实性、逻辑一致性和引用质量提出结构化问题；
- Blue 复用同一 policy，且只调用一次；它只允许返回 `ADD / DELETE / MODIFY / VERIFY`，其中 `VERIFY` 不触发外部核验；
- 根工作流按返回顺序确定性重放 Blue 动作，并要求重放结果与 Blue 返回的完整 Markdown 完全一致，拒绝任何未声明修改；
- 两次调用均不进入 AgentGraph、不 fork、不创建新线程或 checkpoint；
- 以原报告快照和原 manifest 做确定性保护：frontmatter 逐字不变，WikiLink target 与次数不变（alias 可调整），URL 的值与次数不变，manifest 与全部路径不变；
- 保护通过后，只原子替换 manifest 已指向的同一报告文件；不修改 evidence/source，不保存 Red/Blue 中间结果；
- 任一 policy、解析、动作、校验或写入异常都降级到原报告；写入异常不得损坏已经持久化的原文件；
- 对关闭、有效修订、三类 Red 问题、四类 Blue 动作、无效动作、动作与完整 Markdown 不一致、链接/URL/frontmatter/manifest 篡改、policy 异常和写回异常增加固定离线测试。

### 不做

- RCS、质量分数、奖励模型或独立 CompletionEvaluator；
- 新的 Red Agent / Blue Agent 图节点、角色、线程、工具循环或 fork；
- claim-evidence 新领域模型、Review Repository、Review Store 或第二套持久化；
- 为完成修订而重新检索、修改原始来源或生成新的 evidence/source；
- Red/Blue 多轮互攻、自动研究回流或修改 AgentGraph/fork policy。

### 验收

- Red/Blue 不创建研究线程、不修改原始来源、不伪造证据；
- 开关不改变 AgentGraph、fork policy 和 Memory Store 契约；
- 关闭时 policy 零额外调用，开启时 Red 和 Blue 各至多一次且无工具；
- Red 只产生 `factual / logical_consistency / citation_quality` 三类结构化问题，Blue 只接受四种动作；
- Blue 动作按顺序重放后与其完整 Markdown 完全一致；
- 修订后 frontmatter、WikiLink target 与次数、URL 的值与次数以及 manifest 与原报告保护快照一致；
- 关闭、无有效问题或任一失败时仍正常交付原始已持久化报告；
- 关键专项与回归 `65 passed, 1 warning`，全量回归 `160 passed, 1 warning`；全量中的 warning 为既有 `StarletteDeprecationWarning`。

## 11. LLM Wiki + Obsidian

### 状态

W0–W3 已完成，W4–W6 尚未开始。实施与验收结果见 [W0_MEMORY_VAULT_CONTRACT.md](W0_MEMORY_VAULT_CONTRACT.md)、[W1_MULTI_MEMORY_PERSISTENCE.md](W1_MULTI_MEMORY_PERSISTENCE.md)、[W2_OBSIDIAN_MINIMAL_INTEGRATION.md](W2_OBSIDIAN_MINIMAL_INTEGRATION.md) 和 [W3_CONTINUE_RESEARCH_FROM_MEMORY.md](W3_CONTINUE_RESEARCH_FROM_MEMORY.md)。

具体执行以 [LLM Wiki + Obsidian 实施计划](LLM_WIKI_OBSIDIAN_IMPLEMENTATION_PLAN.md) 为唯一阶段顺序和验收边界：

- W0 Memory/Vault 契约与安全基础；
- W1 多 Memory 持久化；
- W2 Obsidian 最小接入；
- W3 基于旧 Memory 继续研究；
- W4 Memory 问答与受控新建笔记；
- W5 资料导入与整理；
- W6 稳定化、迁移与入口收口。

该主线不改变 Research AgentGraph，不新增第二套存储，不开发复杂 Markdown 阅读器。

## 12. 测试顺序

每阶段均按以下顺序验收：

1. 契约和路由单元测试；
2. 固定离线工具的图流转测试；
3. 并发、身份隔离和失败测试；
4. checkpoint 暂停与恢复测试；
5. Markdown 快照和链接完整性测试；
6. 当前阶段相关专项测试；
7. 全量回归。

涉及真实模型、网络或 Langfuse 的测试单独标记，不用外部服务可用性替代确定性验收。

## 13. 立即执行顺序

```text
N0 文档收敛
→ N1 单个同质 Research AgentGraph
→ N2 用户确认 + Markdown Memory Store
→ N3 同质并行 fork
→ N4 一层递归 + 硬停止 + 恢复
→ N5 切换入口 + 清理 legacy
→ N6 可选 Red/Blue
→ W0–W6 LLM Wiki + Obsidian
```

N6 与 W0–W3 已完成，W4 尚未开始。不得从该主线隐式扩展新的 Agent 角色、图/向量数据库、复杂阅读器、第二套存储或未经确认的自动写入行为。

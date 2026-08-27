# PaperPilot 目标架构

> 本文是 PaperPilot 当前唯一的架构事实来源。若其他文档、历史记录或现有代码与本文冲突，以本文为准。

## 1. 产品定义

PaperPilot 是一个基于 LangGraph 的递归式深度研究系统。

系统先由根 Research Agent 与用户对齐研究目标。用户可以修改研究范围和方向；只有用户确认后，根 Agent 才开始研究。研究期间，根 Agent 和所有子 Agent 运行同一套 Research AgentGraph。任何 Agent 都能思考、拆解、检索、阅读、分析、比较、提取证据、按条件 fork 同质子 Agent，并汇聚子结果。

研究结束后，根 Agent 生成最终报告，并把报告、采用的证据和来源以 Markdown 写入同一个持久化 Memory Store。报告通过 WikiLink 连接证据笔记，证据笔记再连接来源笔记；Obsidian 等工具可以直接根据这些链接展示关系。

## 2. 核心原则

### 2.1 根 Agent 与子 Agent 同质

根、子、孙 Agent 使用同一个 Research AgentGraph、工具协议和结果协议：

```text
理解任务
→ 思考与拆解
→ 判断自己执行还是 fork
→ 检索 / 阅读 / 分析 / 比较 / 证据提取
→ 汇聚子 Agent 结果
→ 总结本级结果
→ 返回父级或生成最终报告
```

它们的差异只来自：

- 当前任务范围；
- 独立的消息和工作上下文；
- `thread_id`、父线程和根线程身份；
- 当前递归深度；
- 根 Agent 拥有用户交互和最终报告发布权限。

系统不设置独立 Planner Agent、Summarizer Agent、Gap Agent 或固定专家角色。规划和总结都是每个 Research Agent 的内在步骤。

### 2.2 Fork 是上下文与并行机制

任一 Research Agent 在满足以下任一条件时，可以建议 fork：

1. 存在两个或更多无依赖的研究任务，可以并行执行；
2. 某个任务会产生大量中间材料，需要与当前上下文隔离；
3. 某个任务预计需要至少三层连续工具调用，继续留在当前消息历史会明显膨胀上下文。

是否真正 fork 还必须通过硬约束：任务范围清晰、没有重复执行、仍有并发与资源预算、当前深度允许创建子线程。

递归深度固定为：

```text
根 Agent：depth = 0
子 Agent：depth = 1
孙 Agent：depth = 2，禁止继续 fork
```

### 2.3 每个 Agent 都产生可汇聚结果

每个 Agent 返回结构化 Research Result，最少包括：

- 当前任务和完成状态；
- 总结；
- 关键发现；
- 每项发现对应的证据；
- 证据的来源与定位信息；
- 冲突、不确定性和未完成项；
- 子 Agent 结果的引用。

子 Agent 不是只返回原始材料。它必须先总结自己的任务结果；父 Agent 再结合其他结果做更高一层汇总。最终报告只是根 Agent 的本级总结产物。

### 2.4 运行状态与持久记忆分离

LangGraph checkpointer 保存执行中的图状态、暂停点和恢复信息。Memory Store 保存研究完成后可长期使用的知识。

```text
LangGraph checkpointer = 运行时恢复
Markdown Memory Store  = 持久知识
```

不为运行线程另建 Run/Fork Repository，也不把完整消息历史当作长期知识保存。

### 2.5 一个 Memory Store，不设 Evidence Store

系统只有一个持久化 Memory Store。第一版使用 Markdown Vault 实现，内部目录只是文件组织：

```text
memory/
├── reports/
├── evidence/
└── sources/
```

- 报告正文使用 `[[Evidence-...]]` 指向证据笔记；
- 证据笔记记录摘录、分析和定位信息，并使用 `[[Source-...]]` 指向来源；
- 来源笔记记录论文、网页或本地文件的元数据与访问位置；
- WikiLink 是唯一显式关系表达，不维护独立 Evidence Graph、证据边分类器或图数据库。

Evidence 是 Memory Store 中的一类 Markdown 内容，不是独立存储服务。

### 2.6 完成判断保持简单

现阶段不实现 RCS 或独立评分引擎。每个 Agent 根据任务完成情况做局部判断，根 Agent 对整项研究做最终判断。

停止或继续由可解释的硬规则约束：

- 必需任务是否完成；
- 关键结论是否有可定位来源；
- 是否仍有阻断报告的证据缺口或冲突；
- 本轮是否仍产生有效信息；
- 是否达到深度、线程数、工具调用、时间或 token 上限。

以后只有在积累了真实评测数据后，才考虑把 RCS 作为可插拔辅助评估器加入。

## 3. 两层 LangGraph

### 3.1 外层 Research Workflow

外层只处理根任务生命周期：

```text
接收问题
→ 根 Agent 理解并提出研究说明
→ 等待用户确认
   ├── 用户修改：更新说明并再次确认
   └── 用户确认：启动根 Research AgentGraph
→ 接收根 Research Result
→ 生成最终报告
→ 写入 Memory Store
→ 可选 Red/Blue 单次报告后处理
→ 完成
```

等待用户确认使用 LangGraph interrupt 与 checkpointer，恢复后继续同一 `thread_id`。

### 3.2 同质 Research AgentGraph

根、子、孙 Agent 都调用同一个图定义：

```text
think_and_plan
      ↓
decide_next_action
  ├── use_tool ───────────────┐
  ├── fork_children ──并行──┐ │
  ├── gather_children ◄─────┘ │
  └── synthesize ◄────────────┘
      ↓
return_research_result
```

图可以循环调用工具，但必须受预算和停止条件限制。Fork 分支把明确任务和必要上下文传给同一个 AgentGraph；不得复制父 Agent 的全部消息历史。

## 4. 执行身份

每个执行线程只使用以下身份：

- `thread_id`：当前 AgentGraph 执行；
- `parent_thread_id`：创建当前执行的父线程，根线程为 `null`；
- `root_thread_id`：整次研究的根线程；
- `depth`：`0 | 1 | 2`。

约束：

- 根线程：`thread_id == root_thread_id`；
- 子孙线程：`root_thread_id` 始终不变；
- `parent_thread_id` 必须指向直接父线程；
- `depth == 2` 时任何 fork 请求都转换为本地执行或带原因返回未完成项。

每次根执行共享同一组硬限制：最大总线程数、单 Agent 子线程数、单 Agent/全局工具调用数、共享截止时间、全局 token 预算和失败重试数。预算在 fork 时分配给子树，子 Agent 返回后由父 Agent 汇总实际用量，不设置独立 BudgetManager。

token 用量优先采用模型返回的 `usage.total_tokens`；无 usage 时使用确定性字符估算。任何一级达到限制都必须返回明确 `stop_reason`。

## 5. Agent 输入输出边界

父 Agent 给子 Agent 的输入只包含：

- 明确的研究任务；
- 必要背景和已知约束；
- 期望输出；
- 已知来源或需要核验的主张；
- 执行身份和预算。

不传递父 Agent 的完整对话、scratchpad 或无关工具输出。

证据至少包含：

- 支持的发现或主张；
- 来源类型和标题；
- URL、论文标识或本地文件位置；
- 页码、章节、段落或网页定位信息（可取得时）；
- 原文摘录或准确释义，并明确区分两者；
- 局限与可信度说明。

## 6. Memory Store 输出

根 Agent 在研究汇聚完成后一次性提交本次持久化内容，避免多个子线程并发修改同一报告。

建议的 Markdown frontmatter 仅保留稳定字段：

```yaml
---
id: stable-id
type: report | evidence | source
created_at: ISO-8601
root_thread_id: thread-id
---
```

正文保持人类可读，不要求使用者理解内部运行模型。文件名和 WikiLink 必须稳定，重复执行持久化不得产生重复副本。

## 7. 可选 Red/Blue

Red/Blue 是最终报告成功持久化后的单次可选后处理，默认关闭：

```text
Persisted Original Report + MemoryManifest
→ Red：输出事实性 / 逻辑一致性 / 引用质量问题
→ Blue：只执行 ADD / DELETE / MODIFY / VERIFY
→ 确定性保护校验
   ├── 通过：原路径原子写回修订报告
   └── 失败：保留并返回原报告
```

Red 与 Blue 各调用一次本次运行已经装配的同一个 research policy，不获得工具，不进入 Research AgentGraph，不 fork，也不创建新的 `thread_id`、checkpoint 或执行身份。审查数据只在当前后处理调用内传递，不形成新的持久化领域模型。

Red 只允许报告三类结构化问题：

- `factual`：报告陈述与已有材料不一致、过度断言或无法由现有内容支持；
- `logical_consistency`：前提、推理、结论或报告内部表述存在矛盾或跳跃；
- `citation_quality`：引用位置、引用对象或引用对结论的支持关系存在问题。

Blue 只能返回 `ADD`、`DELETE`、`MODIFY`、`VERIFY` 动作。`VERIFY` 只表达核验结论，不触发工具调用或新研究；任何需要新增来源的事项必须保留为未修订问题，不能伪造证据。根工作流按 Blue 声明的顺序确定性重放全部动作，重放结果必须与 Blue 同时返回的完整 Markdown 完全一致；不一致表示存在未声明修改，整次修订失败。

在接受 Blue 结果前，系统以原始已持久化报告为基准进行确定性保护：YAML frontmatter 必须逐字保持，WikiLink 的 target 与出现次数必须保持，URL 的值与出现次数必须保持，`MemoryManifest` 及其报告、证据、来源路径不得改变。WikiLink alias 只是显示文本，允许随正文编辑而调整，但不得重定向链接。只有全部保护通过，才允许在 manifest 指向的同一报告路径原子写回。生成、解析、动作应用、保护校验或写回任一步失败时，原报告仍是最终交付。

Red/Blue 不参与 fork 决策，不替代研究完成判断，不循环互攻，也不是 RCS 或评分引擎。N6 不增加 claim-evidence 新模型、Review Store、第二份报告仓库或其他持久化服务。

当前 `ResearchResult` 不提供 claim 到 evidence 的逐条映射，因此确定性保护只能证明 frontmatter、WikiLink target、URL 和 manifest 等结构没有损坏，不能机械证明每条新增或修改后的表述都语义归因于正确证据。该语义审查由 Red/Blue 基于本次已有 evidence 完成。N6 不为此扩展数据模型；未来若要求可机械证明的强语义归因，必须另行对齐契约与验收边界。

## 8. LLM Wiki + Obsidian

LLM Wiki 的目标架构已确认，详见 [LLM Wiki + Obsidian 目标架构](LLM_WIKI_OBSIDIAN_ARCHITECTURE.md)。

核心决策是：

- Obsidian 负责 Markdown 阅读、手工编辑、WikiLink、Backlinks 和图谱，PaperPilot 不重复建造复杂阅读器；
- 使用一个 PaperPilot Vault 和多个长期 Memory 目录，不默认为每次研究新建独立 Vault；
- `memory_id` 表示长期知识项目，与会话级 `session_id` 和执行级 `thread_id` 分离；
- 用户可在 Obsidian 中直接新增或修改 Markdown，PaperPilot 下次对话必须读取最新文件；
- 普通记忆问答不进入 Research AgentGraph；需要补充研究时仍先进行 Research Brief 用户确认；
- 对话结果不自动持久化，用户确认“保存为笔记”后才能原子写入；
- Markdown Vault 仍是唯一知识真相源，本主线不引入图数据库、向量数据库或第二套存储。

## 9. N5 迁移结果

旧仓库能力已经按接口逐项审查，不因曾经存在而保留旧角色：

优先考虑复用：

- 搜索、论文、网页、文件读取等工具；
- 模型调用和配置适配；
- Langfuse tracing；
- LangGraph checkpointer 与线程身份基础；
- 符合新接口的 Markdown、Web 或会话能力。

默认不作为新架构骨架：

- 旧 Orchestrator 状态循环；
- Planner DAG；
- AgentPool；
- 独立 Summarizer、Gap Analyzer；
- Evidence Store、Evidence Graph 和关系边；
- RCS、Fork Tree 及围绕旧模型建立的控制服务。

N5 已完成生产入口切换。上述旧角色、领域模型、图谱存储、孤立配置和专属测试均已删除；历史提交与历史文档只用于追溯。CLI、Web 和评测共用 `src/research/runtime.py` 装配同一个 Workflow。

## 10. 非目标

- 不创建多种固定人格或专家 Agent；
- 不让根 Agent 成为与子 Agent 不同的专用 Manager 实现；
- 不保留独立 Planner 或 Summarizer 角色；
- 不建设 Evidence Graph 或图数据库；
- 不在当前阶段实现 RCS；
- 不无限递归 fork；
- 不为了兼容旧代码复制领域模型、Repository 或服务；
- 不恢复已删除的旧角色包装或建立长期兼容层。

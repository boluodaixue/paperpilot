# PaperPilot 目标架构

> 本文是 PaperPilot 的架构事实来源（Source of Truth）。项目的路线图、实现计划和代码重构均以本文为准。

## 1. 项目定义

PaperPilot 是一个由 **Research Manager 驱动的同质子 Agent 动态 fork 系统**。

Research Manager 根据研究问题制定计划，将不同研究方向 fork 给多个能力相同、状态隔离的 Research Agent。子 Agent 独立检索、阅读、分析和提取证据，并把结构化研究贡献合并回 Evidence Graph。Manager 再根据证据覆盖、质量、冲突和边际信息增量决定继续 fork，还是停止研究并合成报告。

项目的核心不是“多个固定角色协作”，也不是“把若干任务放入对象池并发执行”，而是：

1. Manager 持有全局研究状态和停止权；
2. Worker 是同一种 Research Agent 的独立 fork；
3. fork 具有身份、父子关系、上下文快照、预算和生命周期；
4. 子 Agent 通过 Evidence Merge 回流，而不是只提交自然语言小报告；
5. Evidence Graph 驱动下一轮 fork；
6. 研究达到完成条件后，报告从已验证证据生成。

## 2. 设计原则

### 2.1 Manager 与 Worker 分离

Research Manager 属于控制面，负责规划、fork、预算、合并和停止判断。Research Agent 属于执行面，只负责一个受限研究范围。

Manager 不亲自完成各领域检索；Worker 不直接修改全局计划，也不能绕过预算无限创建新 Agent。

### 2.2 Worker 同质，任务异质

所有 Research Agent 使用同一能力蓝图：

- 搜索网页和论文；
- 阅读来源；
- 分析和比较；
- 提取可定位证据；
- 识别矛盾和未知项；
- 提议后续研究方向。

`discover`、`compare`、`verify`、`fill_gap`、`challenge` 是任务模式，不是不同 Agent 类型。同一个 Research Agent 可以执行所有模式。

### 2.3 Fork 是执行实体，不是任务别名

三个概念必须分开：

| 概念 | 含义 |
|------|------|
| Plan Node | Manager 规划出的研究意图及依赖关系 |
| Fork | 某个 Research Agent 对一个 Plan Node 的一次独立执行实例 |
| Attempt | 同一 Plan Node 因重试、降级或替代策略产生的新一次执行 |

向 DAG 添加任务不等于完成 Agent fork。真正的 fork 必须形成可追踪的执行记录。

### 2.4 上下文按需继承，局部状态隔离

子 Agent 不复制 Manager 的完整对话，也不共享可变 Policy 状态。fork 上下文只包含：

- 原始研究问题；
- 当前研究计划摘要；
- 本次 fork 的研究范围和成功条件；
- 必要的上游结果引用；
- 与研究范围相关的 Evidence ID；
- 独立的时间、token 和工具调用预算。

子 Agent 的消息、scratchpad、工具状态和临时假设彼此隔离。共享数据通过不可变引用读取，通过 Merge 协议写入。

### 2.5 Evidence-first Merge

子 Agent 的主要产物不是一段总结，而是 `ResearchContribution`：

- 新发现的来源；
- 可在来源中定位的 Evidence Span；
- 由证据支持的原子 Claim；
- 对已有 Claim 的支持、矛盾或扩展候选；
- 未解决问题；
- 建议继续 fork 的研究方向；
- 本次执行的成本、失败和质量信息。

自然语言总结只是辅助字段。只有通过验证的 Evidence 才能进入全局证据图和最终报告。

### 2.6 中央审批的受控递归

子 Agent 可以提出 `ForkProposal`，但只有 Fork Controller 可以批准并创建新 fork。审批至少考虑：

- 最大 fork 深度；
- 全局和单轮 Agent 数量；
- 剩余 token、时间和工具预算；
- 与已执行研究范围的重复度；
- 目标缺口的重要性；
- 预期信息增量。

这使系统具备递归探索能力，同时避免 Agent 爆炸。

## 3. 总体架构

```text
User / Web / CLI
        │
        ▼
Research Manager ─────────────── Run State / Event Log
        │
        ├── Research Planner ─── Plan Graph
        ├── Fork Controller ──── Fork Registry / Budget Manager
        │        │
        │        ├── fork Research Agent A ─┐
        │        ├── fork Research Agent B ─┼── ResearchContribution
        │        └── fork Research Agent C ─┘
        │                                      │
        ├── Contribution Validator ◄───────────┘
        ├── Evidence Merge ────── Source / Evidence / Claim / Relation
        ├── Evidence Graph
        ├── Gap Analyzer
        └── Completion Evaluator (RCS)
                 │
          continue research?
             │          │
            yes         no
             │          ▼
        next forks   Evidence-grounded Synthesizer
                            │
                            ▼
                  Report / Web / Obsidian
```

## 4. 分层职责

### 4.1 交互层

负责研究澄清、会话管理、进度展示、Agent 树、报告、证据和图谱展示。交互层不得承担研究决策。

主要入口：Web、CLI、REPL。

### 4.2 控制面

#### Research Manager

每个研究 Run 唯一。负责：

- 维护研究目标和全局状态；
- 请求 Planner 生成或调整 Plan Graph；
- 请求 Fork Controller 执行计划；
- 接收并合并 ResearchContribution；
- 触发 Gap Analysis 和 RCS；
- 决定继续、降级、取消或合成。

#### Research Planner

输出研究意图，而不是角色分配。每个 Plan Node 包含：

- 研究范围；
- 任务模式；
- 依赖；
- 需要的证据类型；
- 完成条件；
- 优先级和预算建议。

#### Fork Controller

负责 fork 的创建、调度、取消、重试和血缘记录。它把 Plan Node 转换为独立 `ForkSpec`，通过 Agent Factory 创建 Research Agent。

#### Budget Manager

统一管理：

- 最大并发；
- 最大总 fork 数；
- 最大 fork 深度；
- 单 fork 与全局 token 预算；
- 单 fork 与全局时间预算；
- 工具调用预算。

#### Completion Evaluator

计算 Research Completion Score，并返回继续或停止的结构化理由。建议组成：

```text
RCS = Coverage + EvidenceQuality + SourceDiversity
    + ConflictResolution + Saturation - CostPenalty
```

权重应由评测校准，而不是永久硬编码。

### 4.3 同质执行面

Research Agent 是无固定领域角色的通用研究执行器。标准循环为：

```text
理解 ForkSpec
→ 制定局部搜索策略
→ 搜索和阅读来源
→ 提取 Evidence Span
→ 形成原子 Claim
→ 自检来源相关性与证据充分性
→ 提交 ResearchContribution / ForkProposal
```

每个 fork 必须拥有独立 Policy 会话和工具状态。模型客户端可以共享连接池，但调用状态不可共享。

### 4.4 证据面

统一处理论文、网页、用户文件和其他来源：

```text
SourceDocument
    └── EvidenceSpan
            └── Claim
                  └── EvidenceRelation
```

#### SourceDocument

记录来源类型、URL、标题、作者、发布时间、抓取时间、内容哈希和原文定位信息。

#### EvidenceSpan

必须能够回到来源中的具体位置，例如页码、章节、段落、字符区间或原始摘录。无法定位的内容不得标为已验证证据。

#### Claim

原子、可判真假的研究主张。Claim 与 EvidenceSpan 是多对多关系，不应把 LLM 生成的 claim 和原文摘录混成同一个对象。

#### EvidenceRelation

Embedding 只用于召回候选关系；`SUPPORTS`、`CONTRADICTS`、`EXTENDS` 必须由关系分类器验证，并保存判定理由、模型版本和置信度。

### 4.5 合成面

Synthesizer 不直接消费所有 Agent 对话，而是消费经过预算选择的 `EvidencePackage`：

- 研究问题；
- 主题覆盖摘要；
- 已验证 Claim；
- 对应 Evidence Span 和来源；
- 未解决冲突；
- 重要限制和未知项。

报告生成后再执行 Citation Validator，验证：

- 引用 ID 存在；
- 引用证据支持对应句子；
- 关键结论具有引用；
- 报告没有引用 fork 的临时内容。

### 4.6 持久化与可观测性

至少持久化以下实体：

- ResearchRun；
- PlanNode；
- AgentFork；
- ForkEvent；
- ResearchContribution；
- SourceDocument；
- EvidenceSpan；
- Claim；
- EvidenceRelation；
- Report 与 Citation。

Web 端应能展示 fork 树、每个 Agent 的研究范围、状态、成本、新增证据数和停止原因。

项目使用 Langfuse v4 的 OpenTelemetry 上下文作为 tracing 基础。目标 trace 层级为：

```text
research.run                         chain
├── planner.generate_plan           chain
├── fork:<forkid>                   agent
│   ├── llm-call                    generation
│   ├── tool:<toolname>             tool
│   └── contribution.validate       chain
├── evidence.merge                  chain
├── completion.evaluate             evaluator
└── report.synthesize               agent
```

ResearchRun 使用 Langfuse `session_id` 进行会话关联；fork 相关字段通过 metadata 传播，统一使用 `runid`、`forkid`、`parentforkid`、`plannodeid` 和 `attempt`。Tracing 是旁路能力：SDK、网络或配置失败不得改变研究结果和状态机行为。

## 5. 核心数据契约

### 5.1 ForkSpec

```text
fork_id
run_id
parent_fork_id
plan_node_id
fork_depth
research_scope
research_mode
success_criteria
inherited_evidence_ids
dependency_contribution_ids
token_budget
time_budget_seconds
tool_budget
```

### 5.2 AgentFork

```text
fork_id
agent_id
attempt
status: REQUESTED | APPROVED | RUNNING | MERGING |
        COMPLETED | FAILED | CANCELLED
started_at / finished_at
budget_used
failure_reason
```

### 5.3 ResearchContribution

```text
contribution_id
fork_id
findings
source_documents
evidence_spans
claims
relation_candidates
unresolved_questions
fork_proposals
quality_summary
execution_stats
```

## 6. 主流程

1. 用户确认研究问题，系统创建 ResearchRun。
2. Research Manager 请求 Planner 生成初始 Plan Graph。
3. Fork Controller 将可执行 Plan Node 转换为多个 ForkSpec。
4. Agent Factory 创建独立的同质 Research Agent。
5. 子 Agent 并行研究并提交 ResearchContribution。
6. Validator 验证来源、摘录、Claim 和关系候选。
7. Merge Service 以幂等事务写入 Evidence Store 和 Evidence Graph。
8. Manager 更新 Plan Node 和 fork 状态。
9. Gap Analyzer 与 Completion Evaluator 评估当前研究状态。
10. 若未完成，Manager 批准新的 fork 或重试；若完成，构建 EvidencePackage。
11. Synthesizer 生成报告，Citation Validator 做最终校验。
12. 报告与完整研究轨迹持久化，并输出到 Web 和 Obsidian。

## 7. 失败与降级原则

- 单 fork 失败不导致整个 Run 失败；Manager 可重试、换策略或接受缺口。
- 重试必须生成新 attempt，不能覆盖历史 fork。
- 全局超时时用已验证 EvidencePackage 合成降级报告。
- Gap Analyzer 失败时不能默认“研究充分”，应使用确定性规则或标记不确定停止。
- Evidence 验证失败只拒绝对应贡献，不应丢弃整个子 Agent 的其他有效证据。
- 所有停止都必须记录机器可读的 stop reason。

## 8. 当前代码与目标架构的关系

当前代码可复用：

- Planner 的 DAG 基础能力；
- Orchestrator 状态机与分层并发；
- Researcher 的工具循环；
- EvidenceStore、EvidenceGraph 和 Obsidian 导出；
- Gap Analyzer、Summarizer、Web SSE 和 ChatStore。

需要重构：

- AgentPool 从对象复用升级为独立 fork 生命周期；
- SEARCH/ANALYZE/VERIFY 固定类型改为同质 Research Agent + research mode；
- 可变 Policy 和工具状态按 fork 隔离；
- AgentResult 升级为 ResearchContribution；
- Memory 全量复制改为上下文快照与引用；
- 动态追加任务升级为带血缘、深度和预算的 fork；
- Evidence 从“摘要生成 claim”升级为可定位、可验证的统一来源模型；
- Research Loop 从简单计数停止升级为 RCS。

当前实现状态及迁移顺序见 [ROADMAP.md](ROADMAP.md) 和 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

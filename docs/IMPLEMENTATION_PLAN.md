# PaperPilot 动态 Fork 实施计划

> 本计划把现有“DAG 任务并发 + Worker 对象池”迁移为 [目标架构](ARCHITECTURE.md) 定义的“Research Manager 驱动的同质子 Agent 动态 fork 系统”。

## 1. 实施约束

1. 采用渐进式迁移，不推翻现有可运行主链路。
2. 每个阶段结束时主分支必须可运行、可回滚。
3. 先修执行正确性，再引入 fork 抽象，最后升级证据和自主停止。
4. 新旧路径短期通过 feature flag 并存；新路径达到验收标准后删除兼容层。
5. 所有新增状态必须可持久化、可测试、可在 Web 进度中观察。

## 2. 目标目录调整

建议在保持现有目录的基础上新增控制面和领域契约：

```text
src/
├── manager/
│   ├── research_manager.py       # Run 级决策循环
│   ├── fork_controller.py        # fork 创建、调度、取消、重试
│   ├── budget_manager.py         # 全局/单 fork 预算
│   ├── completion.py             # RCS 与停止原因
│   └── merge_service.py          # Contribution 验证与幂等合并
├── domain/
│   ├── research_run.py
│   ├── plan.py
│   ├── fork.py
│   ├── contribution.py
│   └── evidence.py
├── agents/
│   ├── research_agent.py         # 唯一同质 Worker
│   ├── gap_analyzer.py
│   └── synthesizer.py
├── evidence/
│   ├── source_store.py
│   ├── validator.py
│   ├── relation_classifier.py
│   └── citation_validator.py
└── persistence/
    ├── run_repository.py
    ├── fork_repository.py
    └── evidence_repository.py
```

目录可以分阶段建立；首阶段不要求一次移动现有文件。

## 3. Phase 0：修复执行基线

### 目标

在引入 fork 前消除会污染新架构的已知正确性问题。

### 任务

- 修复全局超时进入 `SYNTHESIZING` 后立即退出状态机的问题；
- 让模型 Policy 的 tools、消息和截断状态成为单次调用状态；
- 修复 AgentPool 对 search/analyze/verify 的错误回收类型；
- 修复合成阶段覆盖 Agent 变量导致的对象泄漏；
- 保证需要事实检索的任务至少完成一次有效工具调用；
- 工具错误采用可配置重试/替代工具，不立即判定整个任务失败；
- 修复 HotpotQA 把报告标题当短答案的评测逻辑；
- 为上述问题补回归测试。

### 主要影响文件

- `src/orchestrator/orchestrator.py`
- `src/orchestrator/agent_pool.py`
- `src/agents/researcher.py`
- `src/agents/summarizer.py`
- `src/models/vllm_policy.py`
- `src/models/model_router.py`
- `scripts/run_eval.py`

### 验收标准

- 并发运行三个 Worker 时 tools 和截断状态互不影响；
- 全局超时且已有有效结果时生成明确标注的降级报告；
- AgentPool 活跃数在一次 Run 后归零；
- Search 模式不能在零有效来源时返回成功；
- 原有测试与新增回归测试全部通过。

## 4. Phase 1：建立 Fork 领域模型

### 目标

让 fork 成为一等领域实体，但暂时仍由现有 Orchestrator 驱动。

### 任务

新增数据契约：

- `ResearchRun`
- `PlanNode`
- `ForkSpec`
- `AgentFork`
- `ForkBudget`
- `ForkEvent`
- `ResearchContribution`
- `ForkProposal`

新增 fork 生命周期：

```text
REQUESTED → APPROVED → RUNNING → MERGING → COMPLETED
                         ├──────→ FAILED
                         └──────→ CANCELLED
```

新增 SQLite 表和 Repository：

- `research_runs`
- `plan_nodes`
- `agent_forks`
- `fork_events`
- `research_contributions`

所有表以 `run_id` 隔离；写入支持幂等键。

### 兼容策略

现有 `SubTask` 暂时适配为 `PlanNode`；现有 `AgentResult` 暂时适配为最小版 `ResearchContribution`。不要立即删除旧模型。

### 验收标准

- 每个被执行的 Plan Node 都对应一个持久化 fork；
- fork 可以查询 parent、depth、attempt、预算和状态；
- 重试生成新的 attempt，不覆盖旧记录；
- 服务重启后仍能读取完整 fork 树；
- Repository 具有并发写和幂等测试。

## 5. Phase 2：实现独立同质 Research Agent Fork

### 目标

用 Agent Factory 和 Fork Controller 替代“按任务类型借对象”的核心语义。

### 任务

#### Agent Factory

- 每个 fork 创建独立 Research Agent 会话；
- Policy 调用状态与工具状态隔离；
- 网络连接池、只读模型配置和 Embedder 等无状态资源可以共享；
- Agent 身份中包含 `agent_id`、`fork_id`、`parent_fork_id`。

#### Fork Context Builder

- 根据 ForkSpec 构建不可变上下文快照；
- 只注入相关 Evidence ID 和上游 Contribution；
- 不复制全局 `_memory_store`；
- 记录本次上下文选择原因和 token 数。

#### Fork Controller

- 按 Plan Graph 依赖调度 fork；
- 执行并发限制、超时、取消和重试；
- 发出 `fork_requested/started/completed/failed` 事件；
- 统一结算预算。

#### 同质 Worker

- 合并 SEARCH/ANALYZE/VERIFY Worker 实现；
- 通过 `research_mode` 调整局部策略；
- 所有 Worker 使用相同工具能力和输出协议。

### Feature Flag

增加：

```yaml
orchestrator:
  execution_mode: fork_v1   # legacy_pool | fork_v1
```

### 验收标准

- 初始计划能够 fork 至少三个同质 Agent 并行运行；
- 前端或日志可区分每个 fork 的身份与研究范围；
- Agent 间不存在消息、tools、scratchpad 和截断状态共享；
- 依赖节点只继承声明过的 Contribution/Evidence；
- legacy 与 fork_v1 在固定离线输入上产生等价结构化结果。

## 6. Phase 3：Evidence-first Contribution 与 Merge

### 目标

将子 Agent 回流单位从自然语言 `AgentResult.output` 改为结构化 `ResearchContribution`。

### 任务

#### 统一来源模型

论文、网页和用户文件统一进入 `SourceDocument`，至少记录：

- 稳定 source ID；
- 来源类型和 URL；
- 标题、作者、日期；
- 原始文本或内容哈希；
- 抓取时间和来源质量元数据。

#### Evidence Span 验证

- `evidence_text` 必须可在来源文本中定位；
- 保存页码、章节、段落或字符区间；
- 无法定位时标记为 rejected/unverified；
- Claim 与 EvidenceSpan 分离并建立显式关联。

#### Contribution Validator

分别验证：

- 来源有效性；
- 摘录真实性；
- Claim 原子性；
- 与研究范围的相关性；
- Claim 是否被摘录蕴含；
- 重复和冲突候选。

#### Merge Service

- 以 `contribution_id` 幂等合并；
- 单条无效证据不影响其他有效贡献；
- 保存 rejected item 及原因；
- Merge 后返回新增来源、证据、Claim 和关系数量。

### 验收标准

- 任一 verified Evidence 都能定位回具体 SourceDocument；
- 伪造或改写的“原文摘录”不能进入 verified 状态；
- 网页与论文证据使用同一套数据接口；
- 重复提交同一 Contribution 不产生重复数据；
- Synthesizer 不再依赖 Worker 的完整对话轨迹。

## 7. Phase 4：图驱动动态 Fork 与受控递归

### 目标

让 Evidence Graph 的缺口真正产生新的 Agent fork，而不只是追加匿名 SubTask。

### 任务

#### Gap Contract

Gap Analyzer 输出结构化 `ResearchGap`：

- gap ID；
- 缺失主题或冲突；
- 重要性；
- 当前证据状态；
- 需要的证据类型；
- 推荐研究范围；
- 预计信息增量。

#### Fork Proposal

Gap Analyzer 和子 Agent 都可以提出 ForkProposal。Fork Controller 负责去重和审批。

#### 预算与递归

正式启用并验证：

- `max_total_forks`
- `max_forks_per_round`
- `max_fork_depth`
- `max_attempts_per_plan_node`
- `global_token_budget`
- `per_fork_token_budget`
- `global_timeout_seconds`
- `saturation_no_growth_rounds`

#### Fork 去重

对研究范围、目标证据类型和已有 Plan Node 做语义去重，避免多个 Agent 重复搜索同一问题。

### 验收标准

- Gap 能生成带 `parent_fork_id` 的新 fork；
- 子 Agent 的 ForkProposal 必须经过中央审批；
- 达到深度、数量或成本上限时拒绝 fork 并记录原因；
- 重复研究范围不会被重复执行；
- 可从持久化数据重建完整 Agent fork 树。

## 8. Phase 5：Research Completion Score

### 目标

用可解释的 RCS 替代“达到轮数/证据无增长就停止”的简化规则。

### 指标

初版建议包含：

| 维度 | 含义 |
|------|------|
| Coverage | 计划主题和关键问题的覆盖比例 |
| Evidence Quality | verified Evidence 的质量与强度 |
| Source Diversity | 独立来源、来源类型和时间分布 |
| Conflict Resolution | 重要矛盾是否得到解释或保留不确定性 |
| Saturation | 最近若干轮新增信息量是否下降 |
| Cost Penalty | 继续研究的预期收益是否低于成本 |

Completion Evaluator 返回：

```text
score
dimension_scores
decision: continue | stop | stop_with_uncertainty
stop_reason
recommended_gaps
```

### 验收标准

- 相同 Evidence Graph 输入产生确定性 RCS 基线结果；
- 每次继续或停止都有维度分数与可读理由；
- `saturation_no_growth_rounds` 等配置真实影响行为；
- 在标注小数据集上校准阈值，不以主观常量作为最终标准。

## 9. Phase 6：证据约束合成与引用验证

### 目标

最终报告只基于经过验证且在预算内选出的 EvidencePackage。

### 任务

- 按主题、重要性、来源多样性和冲突状态选择 EvidencePackage；
- 分块合成，避免固定字符硬截断；
- 每个关键结论生成结构化 Citation；
- Citation Validator 检查存在性、覆盖率和支持关系；
- 引用不足时返回 Manager 补研究或生成带限制声明的报告；
- 对抗优化不得删除或伪造结构化 Citation。

### 验收标准

- 报告内所有 Evidence ID 均存在；
- 关键结论引用覆盖达到配置阈值；
- 引用支持率可自动评测；
- 超长研究输入不会静默截掉全部 Evidence；
- 降级报告明确列出未解决缺口和停止原因。

## 10. Phase 7：Web 可观测性与任务恢复

### 目标

让用户可以看到和理解动态多 Agent 研究过程。

### 任务

- 新增 Agent Tree 视图；
- 展示 fork 的父节点、深度、研究范围、状态、耗时和预算；
- 展示每个 fork 新增的来源、Evidence、Claim 和 rejected item；
- SSE 增加 fork 生命周期、Merge 和 RCS 事件；
- 任务状态从进程内 `_TASKS` 迁移到持久化 Run Repository；
- 支持取消、重连和服务重启后的状态恢复；
- Obsidian 增加 Research Run / Agent Fork 索引笔记，可选择是否导出执行轨迹。

### 验收标准

- 用户能从 UI 回答“哪个 Agent 为什么被 fork、研究了什么、产生了哪些证据”；
- 页面刷新和服务重启后仍能恢复 Run 与 fork 树；
- 取消 Run 能终止未开始和正在运行的 fork；
- 图谱关系筛选、节点详情和 Obsidian 双链继续正常工作。

## 11. Phase 8：评测与清理旧路径

### 目标

证明动态 fork 带来的收益，并删除不再使用的兼容设计。

### 评测拆分

- Fork diversity：不同 Agent 是否探索了不同来源和方向；
- Fork utility：新增 fork 带来的有效 Evidence 增量；
- Evidence validity：摘录可定位率、claim 支持率；
- Citation correctness：引用存在率、支持率、覆盖率；
- Research completeness：RCS 与人工判断的一致性；
- Efficiency：token、时间、工具调用和 fork 数；
- Report quality：事实性、完整性、结构和不确定性表达。

### 消融实验

至少比较：

1. 单 Research Agent；
2. 固定并发 Worker；
3. 初始同质 fork，无 Research Loop；
4. 动态 fork + Gap Analysis；
5. 动态 fork + Gap Analysis + RCS；
6. 完整系统 + Citation Validator。

### 清理项

- 删除 `legacy_pool` feature flag；
- 删除固定角色 Worker 路由；
- 删除重复的旧 Schema 和适配器；
- 删除无消费者配置；
- 更新 README、示例配置和评测报告。

### 验收标准

- 动态 fork 相对固定 Worker 在 Evidence Coverage 或 Citation Correctness 上有稳定收益；
- 报告平均质量提升不依赖无限增加 token 或运行时间；
- 全部测试、迁移测试和端到端测试通过；
- 文档、配置、代码和 Web 展示使用同一套术语。

## 12. 推荐执行顺序

最小可交付链路是：

```text
Phase 0 正确性
→ Phase 1 Fork 数据模型
→ Phase 2 独立同质 Agent
→ Phase 3 Evidence-first Merge
→ Phase 4 动态递归 Fork
→ Phase 5 RCS
→ Phase 6 引用验证
→ Phase 7 Web 可观测性
→ Phase 8 评测与清理
```

Phase 1～2 完成后，系统才具备真正的“同质子 Agent fork”；Phase 3～5 完成后，才具备“Evidence Graph 驱动的自主动态 fork”；Phase 6～8 完成后，才能将其称为可评测、可审计的完整 PaperPilot。

## 13. 第一批建议开发任务

下一轮编码建议只领取以下范围，避免一次改动过大：

1. 修复 Phase 0 的四个并发与生命周期 Bug；
2. 新增 ForkSpec、AgentFork、ResearchContribution 数据类；
3. 新增 ForkRepository 与迁移测试；
4. 实现无持久递归的 `ForkController v1`；
5. 用 feature flag 让初始 DAG 通过 ForkController 执行；
6. 增加三 Agent 并发隔离集成测试；
7. Web SSE 暂时只增加 fork started/completed 两类事件。

完成这批任务后，再进入 Evidence-first Contribution 重构。

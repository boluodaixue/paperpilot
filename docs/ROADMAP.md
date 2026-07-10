# PaperPilot Roadmap

> 目标：在现有 Evidence-centric Deep Research 原型上，构建 [Research Manager 驱动的同质子 Agent 动态 fork 系统](ARCHITECTURE.md)。

## 状态定义

- ✅ 已完成：代码已进入当前主链路并有测试覆盖；
- 🟡 部分完成：已有可复用实现，但尚未满足目标架构语义；
- 🔄 下一阶段：已形成具体实施方案，等待编码；
- ⬜ 未开始。

## 当前能力

| 能力 | 状态 | 当前边界 |
|------|------|----------|
| Planner DAG | ✅ | 能生成子任务并按依赖分层 |
| 执行正确性基线 | ✅ | Policy 单次调用状态、Agent 生命周期、超时降级、检索门槛、工具重试和短答案评测均有回归覆盖 |
| 并发 Researcher | 🟡 | 三 Worker 调用状态已隔离，但仍来自对象池，尚无 fork 身份、血缘与上下文快照 |
| 动态补研究 | 🟡 | Gap Analyzer 可以追加 SubTask，但不是持久化、带血缘的 Agent fork |
| Research Manager | 🟡 | Orchestrator 承担部分 Manager 职责，尚未形成独立控制面 |
| Fork Controller | ⬜ | 尚无 fork 审批、生命周期、递归深度和 attempt 模型 |
| Fork Registry | ⬜ | 尚不能持久化或重建 Agent 树 |
| Fork Budget | 🟡 | 有任务数、轮数、超时配置，但没有统一预算结算，部分配置未生效 |
| Shared Memory | ✅ | SQLite + 向量索引 + session 隔离 |
| Evidence 提取/存储 | 🟡 | 已能从论文摘要抽取 Evidence，但尚未严格验证原文定位 |
| Evidence Graph | 🟡 | 已有结构边和语义边；关系语义仍主要依赖相似度启发式 |
| Evidence-first Merge | ⬜ | 当前主要合并自然语言 AgentResult |
| Gap Analysis | 🟡 | 已进入 Research Loop，失败和饱和判定仍较简化 |
| Research Completion Score | ⬜ | 当前只有轮数、任务数和证据增量等停止条件 |
| Evidence-grounded Synthesis | 🟡 | 报告可引用 Evidence ID，但缺少 Citation Validator |
| Web UI + SSE | ✅ | 已支持会话、进度、报告、证据表和证据图 |
| Agent Tree UI | ⬜ | 尚无 fork 树与单 Agent 贡献展示 |
| Obsidian 导出 | ✅ | 自动/手动导出报告、证据、关系和论文聚合笔记 |
| Langfuse 可观测性 | ✅ | v4 SDK、OpenTelemetry 嵌套链路、OpenAI LLM generation、Agent/Tool/Chain/Retriever observation |
| Adversarial Loop | ✅ | 可选报告优化模块 |
| Evolution | 🟡 | 有实验代码和独立脚本，未接入在线研究主流程 |

## 目标里程碑

### M0：执行正确性基线 ✅

- 修复全局超时降级合成，有成功结果时跳过对抗，无结果时明确失败；
- 隔离并发 Policy 的 tools、messages 和截断状态，并由 ModelRouter 缓存 Policy 模板；
- 修复 AgentPool 精确类型回收、重复释放和合成 Agent 生命周期；
- 为 SEARCH 设置有效来源门槛，并支持可配置工具重试与固定 fallback；
- 修复 HotpotQA 短答案提取。

完成标志：专项联合测试 `24 passed`，全量测试 `173 passed`，三 Worker 并发隔离已有覆盖。详见 [阶段 1 执行正确性](STAGE_1_EXECUTION_CORRECTNESS.md)。

### M1：Fork 领域模型 ⬜

- ResearchRun、PlanNode、ForkSpec、AgentFork；
- parent、depth、attempt、budget、status；
- Fork Repository 与事件日志；
- 从 SubTask/AgentResult 到新模型的兼容适配。

完成标志：每次 Worker 执行都可作为独立 fork 持久化和追踪。

### M2：真正的同质 Agent Fork ⬜

- Agent Factory；
- Fork Controller；
- 独立 Policy 会话、工具状态、scratchpad；
- 按需继承的 Fork Context；
- 初始 Plan Graph 通过 Fork Controller 执行。

完成标志：多个同质 Research Agent 具备独立身份和状态，并能安全并行。

### M3：Evidence-first Contribution ⬜

- ResearchContribution 输出协议；
- 论文、网页和文件统一 SourceDocument；
- 可定位 EvidenceSpan；
- Contribution Validator；
- 幂等 Evidence Merge。

完成标志：Manager 合并结构化证据，而不是拼接 Worker 小报告。

### M4：图驱动动态递归 Fork ⬜

- 结构化 ResearchGap；
- 子 Agent ForkProposal；
- 中央审批、范围去重和受控递归；
- 最大深度、总 fork 数、重试、token 和时间预算。

完成标志：Evidence Graph 缺口能创建带父子血缘的新 Agent fork，并受全局预算约束。

### M5：Research Completion Score ⬜

- Coverage；
- Evidence Quality；
- Source Diversity；
- Conflict Resolution；
- Saturation；
- Cost Penalty。

完成标志：每次继续或停止都具有机器可读的分数、维度和原因。

### M6：证据约束报告 ⬜

- EvidencePackage；
- 分块合成和上下文预算；
- Citation Validator；
- 不足时补研究或输出限制声明。

完成标志：关键结论引用可验证，Evidence 不会因固定字符截断而静默丢失。

### M7：Agent Tree 与任务恢复 ⬜

- fork 树、状态、范围、预算和证据贡献展示；
- fork 生命周期 SSE；
- Run 持久化、重连、取消和服务重启恢复；
- 可选导出 Agent 轨迹到 Obsidian。

完成标志：用户能清楚看到“哪个 Agent 为什么被 fork，以及产生了什么价值”。

### M8：评测与旧路径清理 ⬜

- 单 Agent、固定 Worker、初始 fork、动态 fork、RCS 的消融；
- fork diversity / utility；
- Evidence validity；
- Citation correctness；
- 成本与质量联合评估；
- 删除 legacy pool、固定角色路由和无效配置。

完成标志：动态 fork 的质量收益可重复，并且不是单纯依赖更多 token 和运行时间。

## 推荐执行顺序

```text
M0 正确性
→ M1 Fork 模型
→ M2 独立同质 Agent
→ M3 Evidence-first Merge
→ M4 动态递归 Fork
→ M5 RCS
→ M6 引用验证
→ M7 可观测性
→ M8 评测与清理
```

具体文件、任务拆解和验收标准见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

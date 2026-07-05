# PaperPilot 产品方向与设计原则

> 本文件保留原始产品设计入口。详细系统设计已统一到 [ARCHITECTURE.md](ARCHITECTURE.md)，编码阶段和验收标准见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 核心方向

PaperPilot 是一个由 Research Manager 驱动的同质子 Agent 动态 fork 系统。

它不是多个固定角色 Agent 的流水线，也不是简单并发多个搜索任务。Manager 根据研究计划和 Evidence Graph 的当前状态，持续 fork 能力相同、状态隔离的 Research Agent。每个 Agent 只负责一个清晰研究范围，并通过结构化 ResearchContribution 将证据合并回全局研究状态。

## 核心循环

```text
Research Question
→ Research Manager 制定 Plan Graph
→ Fork Controller 创建同质 Research Agent
→ 子 Agent 并行发现来源、阅读、分析、提取证据
→ Contribution Validator
→ Evidence Merge / Evidence Graph
→ Gap Analysis / Research Completion Score
→ 继续：批准下一轮 fork
→ 停止：生成并验证带引用报告
```

## 多 Agent 定义

### Research Manager

唯一的 Run 级控制 Agent，负责全局研究方向、fork 审批、预算、合并和停止决策。

### Research Agent

唯一的 Worker 蓝图。所有 Worker 都能执行搜索、阅读、分析、比较、验证和证据提取。不同 Agent 的差异来自 ForkSpec，而不是固定角色或人格。

### Fork Controller

把 Plan Node 转换为独立执行实例，记录 fork 的身份、父子血缘、深度、attempt、上下文快照、预算和状态。子 Agent 可以提议后续 fork，但不能自行绕过控制器创建 Agent。

### Synthesizer

在 Manager 宣布研究完成后消费 EvidencePackage 生成报告。它不属于同质研究 Worker，也不参与研究方向探索。

## Evidence-first 定义

```text
SourceDocument → EvidenceSpan → Claim → EvidenceRelation
```

- 来源覆盖论文、网页和用户文件；
- EvidenceSpan 必须可定位回原文；
- Claim 必须是原子、可判真的主张；
- EvidenceRelation 需要经过语义关系验证；
- 最终引用必须能从报告句子追溯到 Claim、EvidenceSpan 和 SourceDocument。

## 自主停止

Research Completion Score 不是单独的展示分数，而是 Manager 的停止协议。它至少考虑覆盖、证据质量、来源多样性、矛盾处理、信息饱和和继续研究成本。

系统可以：

- `continue`：继续 fork；
- `stop`：证据充分，进入合成；
- `stop_with_uncertainty`：预算耗尽或关键缺口无法解决，生成带明确限制的报告。

## 产品体验

用户最终应能看到：

- Research Manager 的研究计划；
- 动态增长的 Agent fork 树；
- 每个 Agent 的研究范围、状态、预算和证据贡献；
- Evidence Graph 及支持、矛盾、扩展关系；
- 为什么继续研究或停止；
- 可追溯引用的最终报告；
- 可导入 Obsidian 的报告、证据、论文与关系笔记。

## 项目边界

- 同质 Worker 不等于共享可变模型会话；
- 向 DAG 增加 SubTask 不等于创建了完整 fork；
- Agent 的自然语言总结不等于已验证证据；
- 更多 Agent 不天然等于更好的研究，必须通过预算、去重、信息增量和评测控制；
- Evolution 与 Adversarial 是可选增强能力，不改变 Manager/Fork/Evidence 的核心主干。

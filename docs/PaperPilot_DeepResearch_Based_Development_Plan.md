# PaperPilot 基于现有 DeepResearch 代码的迁移策略

> 本文件说明“如何复用当前代码”，不再作为目标架构定义。目标架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，完整任务见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 1. 当前基础

现有项目已经提供：

- Planner 生成子任务 DAG；
- Orchestrator 状态机和分层并发；
- Researcher 工具调用循环；
- SharedMemoryStore；
- EvidenceStore 与 EvidenceGraph；
- Gap Analysis 和补研究循环；
- Summarizer、Adversarial Loop；
- Web、SSE、会话和 Obsidian 导出。

这些模块构成迁移基础，但当前的 AgentPool 调度不能直接等同于目标中的动态 Agent fork。

## 2. 核心迁移关系

| 当前实现 | 目标实现 | 迁移方式 |
|----------|----------|----------|
| Orchestrator | Research Manager | 先抽取决策职责，再保留状态机作为运行驱动 |
| Planner SubTask | PlanNode | 增加研究范围、证据要求和完成条件 |
| AgentPool | AgentFactory + ForkController | 对象复用降为实现细节，fork 生命周期成为主语义 |
| ResearcherAgent | 同质 ResearchAgent | 合并固定任务类型，通过 research_mode 调整策略 |
| AgentResult | ResearchContribution | 从自然语言结果升级为来源、证据、Claim 和缺口协议 |
| `_memory_store` 全局上下文 | ForkContext Snapshot | 按相关性和依赖显式继承，局部状态隔离 |
| 动态追加 SubTask | 带血缘 AgentFork | 增加 parent、depth、attempt、预算、状态和持久化 |
| 证据数量停止规则 | Completion Evaluator / RCS | 使用覆盖、质量、多样性、冲突、饱和和成本 |
| 全量结果直接合成 | EvidencePackage | 只消费已验证、预算选择后的证据 |
| 进程内任务状态 | ResearchRun Repository | 支持重连、恢复、取消和 Agent Tree |

## 3. 保留、改造与降级

### 直接保留

- 工具接口；
- SQLite 基础设施；
- Evidence Graph 查询与 Web 可视化；
- Obsidian 双链导出；
- SSE 事件通道；
- 现有单元测试中的数据构造和离线桩。

### 渐进改造

- Planner 和 SubTask；
- Orchestrator 状态机；
- Researcher 输出协议；
- Memory Context 构建；
- Evidence Schema 和关系判定；
- Summarizer 输入协议；
- Web 任务管理。

### 可选增强

- Adversarial Loop：保留为报告后处理，但不得破坏结构化引用；
- Evolution：保持独立实验模块，直到 fork 轨迹和评测数据稳定后再决定接入；
- Compressor：迁移为 Fork Context 和 EvidencePackage 的预算组件。

## 4. 兼容迁移

迁移期间使用：

```yaml
orchestrator:
  execution_mode: legacy_pool  # legacy_pool | fork_v1
```

`fork_v1` 达到以下条件后才能成为默认值：

1. 三个同质 Agent 并发时状态完全隔离；
2. 每次执行都有持久化 fork 记录；
3. 失败、取消和重试不会破坏父子树；
4. 新旧路径在离线固定输入上输出兼容；
5. Web 能显示最小 fork 生命周期；
6. 完整测试通过。

随后逐步删除 legacy pool、固定角色路由和旧 Schema 适配层。

## 5. 近期开发范围

第一批编码只完成：

- 当前并发和生命周期 Bug 修复；
- ForkSpec、AgentFork、ResearchContribution；
- Fork Repository；
- ForkController v1；
- 初始 DAG 通过 fork_v1 执行；
- fork started/completed SSE；
- 并发隔离和持久化测试。

Evidence-first Merge、递归 fork、RCS 和 Agent Tree 在后续里程碑实现，避免一次重写整个系统。

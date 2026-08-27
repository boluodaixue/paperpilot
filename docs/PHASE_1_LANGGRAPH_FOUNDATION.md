# Phase 1：LangGraph 最小编排基础与执行身份（历史记录）

> 历史说明：本文记录旧目标架构下已完成的最小 LangGraph 基础和测试结果，不再定义后续产品架构。当前设计与开发边界以 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) 为准。文中关于 Manager、Planner、legacy 等约束只适用于当时阶段。

日期：2026-08-28

## 目标

在不改变现有产品职责、研究协议和 legacy 主链路的前提下，引入一条可对照运行的 LangGraph 单根线程路径，验证最小图状态、执行身份、checkpoint 和 Langfuse 旁路上下文。

本阶段的实现边界以 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) 的 Phase 1 为准。

## 已完成

### 最小单线程图入口

新增 `src/orchestrator/langgraph_runner.py`，提供：

- `build_research_graph()`：构建带 checkpointer 的最小串行图；
- `run_research_graph()`：执行一个根研究线程并返回现有 `ResearchReport`；
- `ResearchGraphState`：只包含当前图节点实际读写的字段。

图流转固定为：

```text
manager_prepare
→ legacy_research
→ manager_complete
```

`legacy_research` 直接复用现有 `Orchestrator.run()`。因此 Research Manager 的逻辑职责、Planner、DAG、AgentPool、Evidence、Gap Analysis、Summarizer 和现有失败语义均保持原样；Planner 不会被图入口重复调用。

### 最小 graph state

| 字段 | 写入方 | 消费方 |
|------|--------|--------|
| `query` | 图调用入口 | 身份准备、legacy 研究节点、最终报告 |
| `thread_id` | 图调用入口 | 根身份校验、checkpoint 配置校验、Langfuse metadata |
| `parent_thread_id` | 图调用入口 | Phase 1 根身份校验、Langfuse metadata |
| `root_thread_id` | 图调用入口 | 根身份校验、Langfuse session 与 metadata |
| `report` | legacy 研究节点 | Manager 完成节点、图调用返回值、checkpoint 快照 |

Orchestrator、Store、聊天历史、Evidence 数据和其他可变业务对象没有进入 graph state。`SubTask`、`AgentResult`、`ResearchReport` 继续作为现有任务、结果和报告协议，没有增加平行领域模型。

### 根线程执行身份

Phase 1 只允许根线程：

- `thread_id == root_thread_id`；
- `parent_thread_id is None`；
- graph state 的 `thread_id` 必须与 LangGraph `configurable.thread_id` 一致。

不满足条件的调用会在业务执行前明确失败。本阶段没有实现子线程、动态 fork 或递归。

### Checkpointer

- 引入 `langgraph>=1.0.0,<2.0.0`；
- 默认使用 `InMemorySaver`，并允许测试或调用方注入 checkpointer；
- 验证完成后可按 `thread_id` 读取终态快照；
- checkpoint 只保存最小图状态，不复制 Evidence Store 或 Chat Store。

内存 checkpointer 只用于 Phase 1 的图基础和测试。持久化恢复、服务重启恢复和取消语义仍属于 Phase 7。

### Langfuse 线程上下文与降级

每个图节点沿用 `src/utils/tracing.py` 的 `trace_context` 和 `trace_block`，传播：

- `thread_id`；
- `parent_thread_id`；
- `root_thread_id`。

根线程使用 `root_thread_id` 作为 Langfuse session。metadata 清洗保留下划线并跳过空的父线程字段。

同时加固了 tracing context 的构造、进入、退出、更新和 flush 降级：Langfuse SDK 异常不会改变业务结果，业务函数不会因 tracing 退出失败而重复执行，原始业务异常仍原样传播。

## 验收结果

Phase 1 图专项测试：

```text
9 passed
```

LangGraph + Langfuse 联合专项测试：

```text
25 passed
```

全量回归：

```text
188 passed
```

验收覆盖：

- 固定离线输入分别通过 legacy 与 LangGraph 路径完成研究；
- 两条路径均返回现有 `ResearchReport`，完整结构化字段等价；
- 三节点严格串行流转，不包含并行分发；
- `InMemorySaver` 可按两个不同 `thread_id` 读取隔离快照；
- graph state 只包含五个有明确消费者的字段；
- 根线程三个身份字段及 checkpoint 身份一致性校验；
- Langfuse 关闭、构造失败、进入失败、退出失败、更新失败或 flush 失败均不改变业务结果；
- legacy Orchestrator、DAG、AgentPool、Evidence、Chat、Web 和导出回归通过。

测试均使用固定离线输入，没有声称真实联网研究、持久化恢复或 Langfuse 云端上报已经验证。

## 明确未做

Phase 1 没有实现：

- 同质 Research Agent 首轮并行；
- 子线程或递归 fork；
- reducer 结果汇聚；
- RCS 或 Fork Tree；
- 持久化 checkpoint 和任务恢复；
- `ResearchManager`、`ForkController`、`AgentFactory`、`BudgetManager`、`CompletionEvaluator`、`MergeService`；
- ResearchRun/Fork/Contribution Repository 或八套 fork 领域模型；
- 删除 Orchestrator、DAG 或 AgentPool。

## 下一阶段

下一阶段是 Phase 2“同质 Research Agent 与首轮并行”。只有 Research Agent 同质；Manager、Planner、Gap Analysis 和合成继续保留各自职责。Phase 2 尚未实现，也未标记为完成。

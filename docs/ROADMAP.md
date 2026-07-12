# PaperPilot Roadmap

> 路线图只记录当前目标架构的进度。详细任务和验收标准见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 当前判断

仓库已有可运行的旧 Deep Research 链路、Langfuse 基础和 LangGraph 单线程入口，但这些代码多数建立在旧的 Manager / Planner / DAG / AgentPool / Evidence Graph 架构上，不能视为新 PaperPilot 架构已经完成。

可选择迁移的基础能力包括工具、模型适配、tracing、checkpointer 和线程身份。是否复用由新接口决定。

## 进度

| 阶段 | 状态 | 完成标志 |
|---|---|---|
| N0 文档与架构收敛 | ✅ | 唯一架构、唯一计划、旧方案退出活跃文档 |
| N1 单个同质 Research AgentGraph | 🔄 下一阶段 | 同一图以不同深度执行并返回带来源的结构化结果 |
| N2 用户确认与单 Agent 纵向闭环 | ⬜ | 可修改、确认、恢复，并写出互链 Markdown |
| N3 同质并行 Fork | ⬜ | 三种 fork 条件、上下文隔离、并行汇聚和部分失败可用 |
| N4 一层递归与硬停止 | ⬜ | 根→子→孙可运行，孙不可再 fork，限制与恢复可靠 |
| N5 入口迁移与旧实现清理 | ⬜ | CLI/Web 只走新路径，旧架构及证据图退出代码库 |
| N6 可选 Red/Blue | ⬜ | 报告可选审查且不破坏证据链接 |
| Future LLM Wiki | ⬜ | 基于同一 Memory Store 的问答、导入和整理 |

## N0 已确定的架构决策

- 根、子、孙 Agent 使用同一个 Research AgentGraph；
- 规划、fork 判断、研究、汇聚和本级总结都是同一个 AgentLoop 的能力；
- 根 Agent 仅额外拥有用户交互和最终报告发布权限；
- 递归深度为根 `0`、子 `1`、孙 `2`，孙禁止 fork；
- fork 条件为可并行、需上下文隔离、预计工具链至少三层；
- LangGraph 管状态、路由、并行、汇聚、暂停和恢复；
- checkpointer 保存运行状态，Markdown Memory Store 保存持久知识；
- 报告、证据、来源全部写入一个 Memory Store；
- 使用 WikiLink 和 Obsidian backlinks，不建设 Evidence Graph；
- 当前不实现 RCS，完成判断由 Agent 加硬规则完成；
- Red/Blue 是最终报告的可选后处理；
- 旧模块按“复用、迁移能力、删除”逐项审查，不强行兼容。

## 下一阶段：N1

只实现单个同质 AgentGraph：

- 最小任务、执行上下文、结果和证据契约；
- 思考、行动路由、工具调用、本级总结；
- 根/子共用同一图定义；
- 上下文与 checkpoint 隔离；
- Langfuse 旁路追踪；
- 固定离线输入、失败和预算停止测试。

N1 不实现用户确认、fork、Markdown 持久化、RCS 或 Red/Blue。

## 历史基础

以下记录描述旧架构演进，只用于了解已经解决过的问题，不再定义目标设计：

- [阶段 0 基线](STAGE_0_BASELINE.md)
- [阶段 0.5 Langfuse](STAGE_0_5_LANGFUSE.md)
- [旧阶段 1 执行正确性](STAGE_1_EXECUTION_CORRECTNESS.md)
- [旧 Phase 1 LangGraph 最小基础](PHASE_1_LANGGRAPH_FOUNDATION.md)

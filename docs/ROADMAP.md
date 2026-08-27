# PaperPilot Roadmap

> 路线图只记录当前目标架构的进度。详细任务和验收标准见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 当前判断

N0–N5 已完成。CLI、Web 和评测统一进入同一个 Research Workflow；旧 Manager / Planner / DAG / AgentPool / Evidence Graph 链路已经删除。当前主线只保留同质 Research AgentGraph、工具、模型适配、tracing、checkpointer、Markdown Memory 与 UI 会话投影。

## 进度

| 阶段 | 状态 | 完成标志 |
|---|---|---|
| N0 文档与架构收敛 | ✅ | 唯一架构、唯一计划、旧方案退出活跃文档 |
| N1 单个同质 Research AgentGraph | ✅ | 同一图以不同深度执行并返回带来源的结构化结果 |
| N2 用户确认与单 Agent 纵向闭环 | ✅ | 可修改、确认、恢复，并写出互链 Markdown |
| N3 同质并行 Fork | ✅ | 三种 fork 条件、上下文隔离、并行汇聚和部分失败可用 |
| N4 一层递归与硬停止 | ✅ | 根→子→孙可运行，孙不可再 fork，限制与恢复可靠 |
| N5 入口迁移与旧实现清理 | ✅ | CLI/Web/评测只走新路径，旧架构及证据图退出代码库 |
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

## N1 已完成

N1 已建立独立于旧 Orchestrator 的同质 AgentGraph：

- 最小任务、执行上下文、结果和证据契约；
- 思考、行动路由、工具调用、本级总结；
- 根/子共用同一图定义；
- 上下文与 checkpoint 隔离；
- Langfuse 旁路追踪；
- 固定离线输入、失败和预算停止测试；
- 现有 `MockWebSearchTool` 协议复用验证。

N1 专项 `13 passed`，全量回归 `201 passed`。详见 [N1 实施记录](N1_HOMOGENEOUS_AGENT_GRAPH.md)。

## N2 已完成

- 根 Agent 生成研究说明并通过 LangGraph interrupt 等待用户；
- 用户可以连续修改，确认前不会调用研究工具；
- 确认后调用 N1 的同质 AgentGraph；
- 根 Agent 的结构化结果渲染为最终 Markdown 报告；
- 报告、采用的证据和来源写入同一个 Markdown Memory Store；
- WikiLink 可解析，重复提交幂等，确认点和持久化失败可以恢复；
- 持久化失败重试不会重复执行已经完成的研究工具。

N2 专项 `8 passed`，N1+N2 联合专项 `21 passed`，全量回归 `209 passed`。详见 [N2 实施记录](N2_CONFIRMATION_AND_MEMORY.md)。

## N3 已完成

- 三种 fork 条件与任务完整性、依赖、去重、深度、子线程预算门槛已实现；
- 根 Agent 可并发运行同质子 Agent，父子消息、policy、工具和执行身份隔离；
- 成功、失败和部分完成结果可汇聚，子 Agent 失败不会丢失其他证据；
- N3 专项 `10 passed`，N1–N3 联合专项 `31 passed`，全量回归 `219 passed`。

详见 [N3 实施记录](N3_HOMOGENEOUS_PARALLEL_FORK.md)。

## N4 已完成

- 根、子、孙使用同一 AgentGraph，孙级 fork 被硬性拒绝；
- 总线程、单 Agent 子线程、工具、时间、token 和重试限制已进入可 checkpoint 图状态；
- 任务指纹与祖先去重防止递归回环；
- 同一 saver 中的独立子线程可取消和恢复，已完成 sibling 不重复；
- N4 专项 `17 passed`，N1–N4 联合专项 `48 passed`，全量回归 `236 passed`。

详见 [N4 实施记录](N4_RECURSION_LIMITS_AND_RECOVERY.md)。

## N5 已完成

- CLI、Web 和评测默认入口已切换到共享 `ResearchRuntime`；
- Web 使用同一 thread 完成说明修改、确认和研究，SSE 支持同进程游标回放；
- 固定离线输入完成 N4 legacy 与 N5 Workflow 对照，新路径增加可定位 evidence 与 WikiLink；
- 旧 Orchestrator、Planner DAG、AgentPool、独立 Summarizer、Evidence Store/Graph、Evolution、旧 Red/Blue 和孤立配置已删除；
- N5 完成后全量回归 `119 passed`。

详见 [N5 实施记录](N5_ENTRY_MIGRATION_AND_LEGACY_CLEANUP.md)。

## 下一阶段：N6（可选 Red/Blue）

只在最终 Markdown 报告之上增加可关闭的审查与修订，不改变 Research AgentGraph、fork policy、Memory Store 或线程模型。N6 尚未开始。

## 历史基础

以下记录描述旧架构演进，只用于了解已经解决过的问题，不再定义目标设计：

- [阶段 0 基线](STAGE_0_BASELINE.md)
- [阶段 0.5 Langfuse](STAGE_0_5_LANGFUSE.md)
- [旧阶段 1 执行正确性](STAGE_1_EXECUTION_CORRECTNESS.md)
- [旧 Phase 1 LangGraph 最小基础](PHASE_1_LANGGRAPH_FOUNDATION.md)

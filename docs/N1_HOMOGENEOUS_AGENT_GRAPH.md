# N1：单个同质 Research AgentGraph 实施记录

日期：2026-08-28

## 目标

建立完全独立于旧 Orchestrator、Planner DAG、AgentPool 和 Summarizer 的最小 Research AgentGraph。根 Agent 和子 Agent 只通过不同的任务、身份和深度输入运行同一个图定义。

## 已完成

- 新增 `src/research` 独立包；
- 定义最小 `ResearchTask`、`ExecutionIdentity`、`AgentLimits`、`EvidenceItem` 和 `ResearchResult`；
- 实现统一图流转：

```text
prepare
→ think_and_plan
   ├── use_tools → think_and_plan
   └── synthesize → END
```

- 同一图接受根 `depth=0`、子 `depth=1`，并保留孙 `depth=2` 的身份合法性；
- 复用现有工具的 `get_openai_tool_schema()` 与 `execute()` 协议，不复用旧 Researcher 角色；
- 验证现有 `MockWebSearchTool` 可以直接接入；
- 网页搜索、论文、网页正文和本地文件结果可归一化为带来源与 locator 的 Evidence；
- 模型必须返回结构化最终 JSON；非结构化结果降级为 typed partial result；
- 工具异常、未知工具、模型异常、无证据、最大迭代数和最大工具调用数均有确定结果；
- 工具返回写入消息前有长度限制，完整结果不进入持久记忆；
- 使用 `thread_id`、`parent_thread_id`、`root_thread_id` 和 `depth` 校验执行身份；
- 接入 LangGraph checkpointer 和 Langfuse 节点/工具上下文；
- tracing 关闭或 SDK 故障不影响业务结果。

## 复用与未复用

直接复用：

- LangGraph 与 checkpointer；
- Langfuse 适配层；
- 模型 callable 协议；
- 现有研究工具 schema/execute 协议。

没有复用为新架构骨架：

- `SubTask`、旧 `AgentResult` 和 `ResearchReport`；
- `ResearcherAgent`、Planner、Summarizer；
- Orchestrator、DAG、AgentPool；
- Evidence Store、Evidence Graph、Gap Analysis、RCS。

## 测试

- N1 专项：`13 passed`；
- N1 + 旧 LangGraph Phase 1 + tracing 联合专项：`38 passed`；
- 全量回归：`201 passed in 54.63s`。

覆盖：

- 固定离线工具调用与结构化结果；
- 来源与 locator；
- 根/子同图；
- checkpoint 与消息隔离；
- 现有工具协议；
- 工具失败、模型失败和非结构化输出；
- 迭代与工具调用硬限制；
- 非法身份和 checkpoint thread mismatch；
- Langfuse SDK 故障降级。

## 明确未实现

- 用户对齐和确认；
- fork 或并行子 Agent；
- Markdown Memory Store；
- CLI/Web 新入口；
- RCS、Red/Blue、LLM Wiki。

下一阶段只进入 N2。

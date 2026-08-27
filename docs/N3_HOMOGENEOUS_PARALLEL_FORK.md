# N3 同质并行 Fork 实施记录

## 结论

N3 已完成根 Agent 到子 Agent 的一级并行 fork。根与子都调用同一个 `Research AgentGraph`，没有新增 Manager、Planner、Summarizer、ForkController、AgentFactory、AgentPool 或 fork Repository。

## 实施范围

- 新增最小 `ForkCandidate` 和三种 `ForkReason`：可并行、上下文隔离、预计工具链至少 3 次；
- 新增纯函数 fork gate，校验任务完整性、依赖、重复、深度和子线程预算；
- 在同一 AgentGraph 内加入 `fork_children` 节点，并发调用同一图构造函数；
- 子 Agent 只获得明确任务、必要背景、期望输出和执行身份，不复制父 Agent 消息历史；
- 每个子 Agent 使用独立 policy/tool 实例、消息状态、`thread_id` 和 checkpoint；
- 父 Agent 汇聚子结果、证据、失败和未解决项，再执行自己的 `synthesize`；
- N3 硬性禁止 `depth=1` 创建孙 Agent。

## 验收结果

- N3 专项：`10 passed`；
- N1–N3 联合专项：`31 passed`；
- 全量回归：`219 passed`；
- 覆盖三种 fork 条件、依赖拒绝、真并发、上下文隔离、实例隔离、重复去除、子线程预算、深度门槛和部分失败汇聚。

## 保留边界

N3 没有实现孙 Agent、跨层恢复、全局线程/token/时间预算、RCS、Fork Tree UI 或新的持久化服务。这些中属于主线的递归与恢复部分留到 N4，其余不进入当前主线。

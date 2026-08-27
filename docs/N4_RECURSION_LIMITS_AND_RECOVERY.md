# N4 一层递归、硬停止与恢复实施记录

## 结论

N4 已完成根 `depth=0` → 子 `depth=1` → 孙 `depth=2` 的有界同质递归。三层都使用同一 `Research AgentGraph`；孙 Agent 的 fork 请求由确定性门槛拒绝，不会创建第四层线程。

## 实施范围

- `AgentLimits` 增加最大总线程、全局工具调用、共享耗时、全局 token、单动作重试和全局重试限制；
- 预算作为 AgentGraph 状态在子树间确定性分配和汇总，没有新增 BudgetManager 或运行 Repository；
- 同一根执行的所有子/孙 Agent 共享 checkpointer，但使用各自的确定性 `thread_id`；
- 父节点取消后，已产生终态 checkpoint 的子任务直接复用，未完成任务从最近节点边界恢复；
- 任务指纹和祖先 objective 共同阻止同级重复与 `A → B → A` 递归回环；
- 父 Agent 汇总子树线程、工具调用、token 估算、重试、证据和部分失败；
- 新图状态输出 `agent_started / tool_finished / fork_finished / agent_finished / agent_failed` 运行事件，并保留三个线程 ID 和 `depth`。

## 验收结果

- N4 专项：`17 passed`；
- N1–N4 联合专项：`48 passed`；
- 全量回归：`236 passed`；
- 覆盖三层递归、孙级禁止 fork、祖先去重、所有硬限制、子树部分失败、根节点恢复和共享子 checkpoint 恢复。

## 恢复边界

LangGraph checkpoint 在节点边界提交。因此，已完成并写入终态 checkpoint 的子任务不会重复；取消瞬间仍在执行的外部工具可能在恢复时重跑。要保证任意外部副作用 exactly-once 需要工具自身的幂等协议，不在 N4 中新增服务解决。

token 限制优先使用模型 usage，无 usage 时使用确定性估算。限制在每次模型调用前预检，并在每次响应后更新。

## 未扩展项

N4 没有修改旧 CLI/Web、Orchestrator、Planner DAG、AgentPool、Evidence Graph 或评测入口。新 Workflow 已产生可映射的运行事件；入口切换和 legacy 清理属于 N5，开始前需先确认迁移/删除清单。

# N2：用户确认与 Markdown Memory Store 实施记录

日期：2026-08-28

## 目标

在 N1 同质 Research AgentGraph 外增加最小根任务生命周期：先与用户对齐研究说明，允许反复修改，确认后才执行研究，最后把报告、证据和来源持久化为互链 Markdown。

## 图流程

```text
draft_brief
→ review_brief（LangGraph interrupt）
   ├── modify → revise_brief → review_brief
   └── confirm → prepare_research
                  → 同一个 N1 Research AgentGraph
                  → persist_result
                  → END
```

对齐和研究复用同一个 policy 适配。外层 Workflow 不是新的 Manager、Clarifier 或 Report Agent。

## 已完成

- 新增结构化 `ResearchBrief`；
- 根 Agent 生成目标、范围、研究方向、限制和预期输出；
- 使用 LangGraph interrupt 暂停并返回可审阅说明；
- 支持用户连续修改，revision 单调增加；
- 用户确认前研究工具调用数始终为零；
- 确认后把 ResearchBrief 转为根 `ResearchTask`，嵌入 N1 的同质 AgentGraph；
- 子图继承外层 checkpointer，重建图后仍可恢复确认点；
- 持久化节点失败后只重试持久化，不重复已完成的研究工具；
- 根 Research Result 确定性渲染为人类可读 Markdown 报告；
- 实现一个文件系统 `MarkdownMemoryStore`；
- 报告、证据和来源分别组织在 `reports/`、`evidence/`、`sources/`，但属于同一个 Store；
- 报告使用 `[[evidence/...]]`，证据使用 `[[sources/...]]`；
- Obsidian backlinks 提供反向关系，不维护 Evidence Graph；
- 稳定文件名、原子替换和重复提交无重复文件；
- 报告按根线程区分，证据与来源可以跨研究复用。

## 持久化边界

```text
LangGraph checkpointer
  保存：运行状态、interrupt、节点恢复

Markdown Memory Store
  保存：最终报告、采用的证据、来源笔记
```

不把完整消息历史、scratchpad 或工具原始轨迹写入长期 Memory。

## 测试

- N2 专项：`8 passed`；
- N1 + N2：`21 passed`；
- N1 + N2 + 旧 LangGraph Phase 1 + tracing：`46 passed`；
- 全量回归：`209 passed in 55.48s`。

覆盖：

- 首次运行在确认点暂停；
- 连续两次修改后确认；
- 确认前工具门禁；
- 报告—证据—来源 WikiLink；
- 重复提交幂等；
- 使用同一 checkpointer 重建图并恢复；
- 持久化失败后恢复且不重复研究；
- 多根线程 checkpoint 和报告隔离；
- 跨研究证据与来源复用；
- 非根身份拒绝。

## 明确未实现

- 同质子 Agent fork；
- 递归 fork；
- CLI/Web 默认入口切换；
- Evidence Graph、RCS、Fork Tree；
- Red/Blue 和 LLM Wiki。

下一阶段只进入 N3。

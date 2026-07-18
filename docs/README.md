# PaperPilot 文档索引

## 现行文档

只有以下三份文档定义当前项目：

1. [ARCHITECTURE.md](ARCHITECTURE.md)：唯一架构事实来源；
2. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)：唯一开发顺序和验收边界；
3. [ROADMAP.md](ROADMAP.md)：当前完成度和下一阶段。

若现有代码、注释、历史记录或提交说明与现行文档冲突，以 `ARCHITECTURE.md` 为准。

## 历史记录

以下文件只描述当时已经完成的工作、测试结果和问题修复，不再定义后续架构：

- [STAGE_0_BASELINE.md](STAGE_0_BASELINE.md)
- [STAGE_0_5_LANGFUSE.md](STAGE_0_5_LANGFUSE.md)
- [STAGE_1_EXECUTION_CORRECTNESS.md](STAGE_1_EXECUTION_CORRECTNESS.md)
- [PHASE_1_LANGGRAPH_FOUNDATION.md](PHASE_1_LANGGRAPH_FOUNDATION.md)

历史记录中出现的 Manager、Planner DAG、AgentPool、Evidence Store、Evidence Graph、Gap Analysis 或 RCS，均不代表当前目标架构仍保留这些设计。

## 当前架构实施记录

- [N1_HOMOGENEOUS_AGENT_GRAPH.md](N1_HOMOGENEOUS_AGENT_GRAPH.md)：单个同质 Research AgentGraph 的实现与验收结果。
- [N2_CONFIRMATION_AND_MEMORY.md](N2_CONFIRMATION_AND_MEMORY.md)：用户确认、单 Agent 闭环与 Markdown Memory Store 的实现与验收结果。
- [N3_HOMOGENEOUS_PARALLEL_FORK.md](N3_HOMOGENEOUS_PARALLEL_FORK.md)：一级同质并行 fork、上下文隔离与部分失败汇聚的实现与验收结果。
- [N4_RECURSION_LIMITS_AND_RECOVERY.md](N4_RECURSION_LIMITS_AND_RECOVERY.md)：一层递归、全局硬限制、取消与多线程 checkpoint 恢复的实现与验收结果。
- [N5_ENTRY_MIGRATION_AND_LEGACY_CLEANUP.md](N5_ENTRY_MIGRATION_AND_LEGACY_CLEANUP.md)：生产入口迁移、固定输入对照与 legacy 清理结果。

## 已清理文档

以下重复方案已删除，其内容不再维护：

- `PaperPilot_Development_Plan.md`
- `PaperPilot_DeepResearch_Based_Development_Plan.md`

产品目标已合并到 `ARCHITECTURE.md`，迁移步骤已合并到 `IMPLEMENTATION_PLAN.md`。

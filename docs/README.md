# PaperPilot 文档索引

## 现行文档

以下文档共同定义当前项目和已确认的下一主线：

1. [ARCHITECTURE.md](ARCHITECTURE.md)：唯一架构事实来源；
2. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)：唯一开发顺序和验收边界；
3. [ROADMAP.md](ROADMAP.md)：当前完成度和下一阶段；
4. [LLM_WIKI_OBSIDIAN_ARCHITECTURE.md](LLM_WIKI_OBSIDIAN_ARCHITECTURE.md)：LLM Wiki + Obsidian 主线的目标架构附录；
5. [LLM_WIKI_OBSIDIAN_IMPLEMENTATION_PLAN.md](LLM_WIKI_OBSIDIAN_IMPLEMENTATION_PLAN.md)：W0–W6 的具体实施与验收附录。
6. [S_PRODUCTION_HARDENING_AND_RETRIEVAL_PLAN.md](S_PRODUCTION_HARDENING_AND_RETRIEVAL_PLAN.md)：W6 之后的 S0–S5 生产化与检索升级计划；S0–S1 已完成，S2–S5 尚未开始。

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
- [N6_OPTIONAL_REPORT_REVIEW.md](N6_OPTIONAL_REPORT_REVIEW.md)：默认关闭的单次 Red/Blue 报告后处理实现与验收结果。
- [W0_MEMORY_VAULT_CONTRACT.md](W0_MEMORY_VAULT_CONTRACT.md)：长期 Memory/Vault、frontmatter、WikiLink 与路径安全契约的实现与验收结果。
- [W1_MULTI_MEMORY_PERSISTENCE.md](W1_MULTI_MEMORY_PERSISTENCE.md)：多 Memory 创建、选择、隔离持久化、并发与 legacy 兼容的实现与验收结果。
- [W2_OBSIDIAN_MINIMAL_INTEGRATION.md](W2_OBSIDIAN_MINIMAL_INTEGRATION.md)：安全 Obsidian URI、Web Memory 控件与 CLI 位置输出的实现与验收结果。
- [W3_CONTINUE_RESEARCH_FROM_MEMORY.md](W3_CONTINUE_RESEARCH_FROM_MEMORY.md)：可重建 Markdown 检索、Research Brief 记忆上下文与同 Memory 继续研究的实现和验收结果。
- [W4_MEMORY_QA_CONTROLLED_NOTES.md](W4_MEMORY_QA_CONTROLLED_NOTES.md)：当前 Memory 问答、带 WikiLink 引用、受控笔记提案与并发冲突保护的实现和验收结果。
- [W5_CONTROLLED_IMPORTS.md](W5_CONTROLLED_IMPORTS.md)：PDF/文本/显式 URL 导入、可定位提取、受控整理提案与确认后成组写入的实现和验收结果。
- [W6_STABILIZATION_MIGRATION_AND_ENTRY.md](W6_STABILIZATION_MIGRATION_AND_ENTRY.md)：CLI/Web 固定 Memory、legacy 只读迁移、可观测性与固定离线评测的实现和验收结果。
- [S0_FILE_READER_SANDBOX.md](S0_FILE_READER_SANDBOX.md)：默认拒绝、每次运行虚拟根授权、路径/链接/竞态和内容边界的实现与验收结果。
- [S1_PERSISTENT_WORKFLOW_STATE.md](S1_PERSISTENT_WORKFLOW_STATE.md)：AsyncSqliteSaver、State 唯一真相、薄 Runtime Registry、TTL/outbox 与重启恢复的实现和验收结果。

LLM Wiki + Obsidian 主线的 W0–W6 已全部完成，S0 文件读取沙箱与 S1 持久化工作流状态也已完成；S2–S5 尚未开始，具体边界见 [S 系列实施计划](S_PRODUCTION_HARDENING_AND_RETRIEVAL_PLAN.md)。

## 已清理文档

以下重复方案已删除，其内容不再维护：

- `PaperPilot_Development_Plan.md`
- `PaperPilot_DeepResearch_Based_Development_Plan.md`

产品目标已合并到 `ARCHITECTURE.md`，迁移步骤已合并到 `IMPLEMENTATION_PLAN.md`。

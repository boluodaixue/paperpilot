# PaperPilot 文档

新版文档只保留当前架构事实、近期计划和最有代表性的工程设计。完整的阶段性 N/W/S 实施历史保存在 `archive/full-history-s0-s5` 分支，不再作为作品集版的阅读主线。

## 当前文档

1. [CURRENT_STATUS.md](CURRENT_STATUS.md)：当前分支、已实现能力、真实验证、迁移边界与已知问题；
2. [ARCHITECTURE.md](ARCHITECTURE.md)：系统边界、核心组件、状态归属和数据流；
3. [UNIFIED_CONVERSATION_ARCHITECTURE.md](UNIFIED_CONVERSATION_ARCHITECTURE.md)：统一对话与 Headless Core 的目标边界和分阶段迁移状态；
4. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)：当前实现的验收方法与近期作品集交付计划；
5. [ROADMAP.md](ROADMAP.md)：现状、真实演示和后续优先级；
6. [W0_MEMORY_VAULT_CONTRACT.md](W0_MEMORY_VAULT_CONTRACT.md)：稳定 `memory_id`、Markdown/frontmatter/WikiLink 与路径契约；
7. [S1_PERSISTENT_WORKFLOW_STATE.md](S1_PERSISTENT_WORKFLOW_STATE.md)：AsyncSqliteSaver、LangGraph State 和恢复边界；
8. [S2_SINGLE_VAULT_WRITER.md](S2_SINGLE_VAULT_WRITER.md)：持久队列、单一 Writer、journal 和崩溃一致性；
9. [S5_OPTIONAL_SEMANTIC_HYBRID_RETRIEVAL.md](S5_OPTIONAL_SEMANTIC_HYBRID_RETRIEVAL.md)：FTS、语义与 WikiLink 的可选混合检索。

## 待实施架构

1. [RESEARCH_AGENT_V2_DESIGN.md](RESEARCH_AGENT_V2_DESIGN.md)：已确认的 Supervisor、Blue Worker、Red Reviewer、Lead Draft 与 Citation Audit 整体设计；
2. [RESEARCH_AGENT_V2_IMPLEMENTATION_PLAN.md](RESEARCH_AGENT_V2_IMPLEMENTATION_PLAN.md)：基于当前基础设施、LangChain Deep Research From Scratch 和 GPT Researcher 固定版本制定的分阶段实施计划。

若设计文档与当前实现状态冲突，先看 [CURRENT_STATUS.md](CURRENT_STATUS.md)，再以代码和测试为准。

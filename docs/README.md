# PaperPilot 文档

新版文档只保留当前架构事实、近期计划和最有代表性的工程设计。完整的阶段性 N/W/S 实施历史保存在 `archive/full-history-s0-s5` 分支，不再作为作品集版的阅读主线。

## 当前文档

1. [ARCHITECTURE.md](ARCHITECTURE.md)：系统边界、核心组件、状态归属和数据流；
2. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)：当前实现的验收方法与近期作品集交付计划；
3. [ROADMAP.md](ROADMAP.md)：现状、真实演示和后续优先级；
4. [W0_MEMORY_VAULT_CONTRACT.md](W0_MEMORY_VAULT_CONTRACT.md)：稳定 `memory_id`、Markdown/frontmatter/WikiLink 与路径契约；
5. [S1_PERSISTENT_WORKFLOW_STATE.md](S1_PERSISTENT_WORKFLOW_STATE.md)：AsyncSqliteSaver、LangGraph State 和恢复边界；
6. [S2_SINGLE_VAULT_WRITER.md](S2_SINGLE_VAULT_WRITER.md)：持久队列、单一 Writer、journal 和崩溃一致性；
7. [S5_OPTIONAL_SEMANTIC_HYBRID_RETRIEVAL.md](S5_OPTIONAL_SEMANTIC_HYBRID_RETRIEVAL.md)：FTS、语义与 WikiLink 的可选混合检索。

若代码、注释和文档冲突，以 [ARCHITECTURE.md](ARCHITECTURE.md) 描述的当前实现为准。

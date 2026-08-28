# S4：持久化全文检索实施记录

## 状态

已完成（2026-08-29）。S4 只替换当前 Memory 的派生检索实现；Markdown Vault 仍是唯一知识真相源。Research AgentGraph、Research Workflow、fork/递归/预算、LangGraph State/checkpointer、S2 Writer、S3 legacy 退役和 N6 可选 Red/Blue 均未改变。S5 embedding/混合检索未在本阶段实现。

## 已实现

- `MarkdownMemoryIndex` 使用 Vault 外 SQLite FTS5，不再在每次查询前读取全部 Markdown 正文。产品路径由 `runtime.retrieval_db_path` 配置，默认 `data/retrieval.db`；`runtime.retrieval_reconciliation_seconds` 默认 300 秒。
- 普通表按 Vault scope、`memory_id`、Vault 相对路径保存内容哈希、mtime、字节数、标题、frontmatter 派生文本、正文和 WikiLink；FTS5 表保存有界 `chunk_id`、对应文档哈希及可解释检索字段。
- 首次使用从当前安全 Vault 全量构建；随后每次查询先扫描文件元数据，仅在新建、mtime/大小变化、删除或重命名时读取正文并更新相应文档/分块。周期 reconciliation 和显式 `force_hash` 会重新计算全部内容哈希。
- 查询 SQL 在 FTS/BM25 召回前同时限定 Vault scope 和显式 `memory_id`。最终排序保留标题、路径、frontmatter、正文和一跳 WikiLink/Backlink 的确定性权重。
- 返回前逐条重新扫描当前 Memory 的安全路径并读取真实 Markdown 计算 SHA-256；失效、删除、跨 Memory、symlink/junction 或哈希变化会触发强制同步并重查，绝不把陈旧索引正文直接送入上下文。
- 索引库可直接删除；下一次查询只从 Vault 重建，不需要迁移或恢复索引。索引不写 `.obsidian/`、frontmatter 或 Markdown，也没有任何反向写能力。
- 中文查询保留确定性词组/双字项，英文使用 Unicode FTS5；空查询仍同步但不返回无关文档。

## 数据职责

- Markdown 是唯一持久知识与引用目标；SQLite 只保存可丢弃的派生检索副本。
- Chat Store 不复制索引正文；Runtime Registry、LangGraph State 与 Vault Writer 不承担检索索引职责。
- 查询默认且强制限定一个明确 `memory_id`；S4 没有跨 Memory API。

## 验收结果

- S4 专项：`5 passed`；
- W3 原检索契约与 W6 检索可观测性：通过；
- 仓库全量（含 S0–S3、N1–N6、W0–W6）：`691 passed, 2 skipped`；
- 固定集合覆盖中英文关键词、WikiLink 邻居、250+ 文档 Vault、无命中、外部编辑、创建/修改/删除/重命名、索引删除重建与跨 Memory 隔离。

## 明确未实现

- 未引入 embedding、向量库、图数据库、语义召回或 RRF；
- 未允许模型扩大 Memory 范围，未实现默认或隐式跨 Memory 检索；
- 未将索引作为备份、Repository 或第二知识真相源；
- 未因检索命中触发笔记、导入、研究或任何自动写入；
- 未处理已延期的 LangGraph serializer 未来兼容警告。

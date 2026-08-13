# S5：可选语义与混合检索实施记录

## 状态

已完成（2026-08-29）。S5 只扩展 S4 的可删除派生检索索引；Markdown Vault 仍是唯一知识真相源。Research AgentGraph、Research Workflow、fork policy、递归/预算、LangGraph State/checkpointer、单一 Vault Writer、确认边界与 N6 可选 Red/Blue 均未改变。

## 已实现

- 语义检索由 `runtime.semantic_retrieval_enabled` 显式开启，默认关闭；关闭时保持 S4 FTS5 行为和性能。
- 默认适配本地 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，并默认 `local_files_only: true`。PaperPilot 不配置或调用外部 embedding API；只有操作者显式关闭 `semantic_local_files_only` 时，底层模型库才可能获取模型文件。
- 只读取调用方明确传入的一个 `memory_id`，只为该 Memory 的 S4 有界分块生成 embedding。不存在默认跨 Memory 查询接口。
- 向量以 `Vault scope + memory_id + path + chunk_id + content_hash + model_id` 缓存在 Vault 外的 `memory_embedding_chunks` 表；Markdown 修改、删除、索引重建或模型版本切换会使对应缓存删除或重建。
- FTS、语义和一跳 WikiLink/backlink 分别召回，再以固定权重 `2.0 / 1.0 / 0.5` 和固定 `k=60` 的 RRF 合并；路径作为稳定次级排序键，结果可重复。
- 查询结果继续受既有 `limit <= 10`、有界摘要和 S4 分块上限约束。送入问答或 Research Brief 前仍由现有入口再次校验 `memory_id`；引用只接受本次有效命中的真实 Vault 相对路径。
- 模型缺失、加载失败、向量数量/维度错误、NaN/无穷或零向量均不会生成伪语义结果，而是整次查询明确降级到 S4。trace 只记录模式、模型标识和异常类型，不记录问题或正文。
- 模型推理不持有 SQLite 写事务；仅在发布已计算的派生缓存时短暂获取写锁。

## 配置

```yaml
runtime:
  retrieval_db_path: "data/retrieval.db"
  retrieval_reconciliation_seconds: 300
  semantic_retrieval_enabled: false
  semantic_embedding_model: "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
  semantic_local_files_only: true
```

生产默认因此不会下载模型，也不会把 Markdown 发送到外部服务。启用语义检索但本机没有所选模型时，查询仍由 S4 FTS5 完成。

## 安全与一致性

- embedding 输入来自已通过 S4 路径、symlink/junction、UTF-8 与 Memory 边界检查的当前分块。
- 缓存是可丢弃派生数据，不能反向写入 Markdown，也不是备份、Repository 或第二知识真相源。
- 混合结果返回前重新验证文件仍存在、仍属于当前 Memory 且 SHA-256 与命中版本一致；外部编辑竞态会触发强制 reconciliation 和重新排序。
- 模型版本变化只有在新模型成功产出合法向量后才发布新缓存；失败时保留 S4 可用性。

## 验收

- S5 + S4 + W3 + W6 检索/可观测性专项：`28 passed`；
- 仓库全量：`697 passed, 2 skipped`；
- 固定离线用例覆盖中英文同义表达、精确关键词不退化、WikiLink 融合、模型失败/非法向量降级、模型版本切换重建、严格 Memory 隔离和最终内容哈希有效性。

两个 skip 均为既有平台/可选能力条件，不是 S5 失败。全量验收未使用网络模型下载或伪造随机向量。

## 明确未做

- 未建立向量数据库、图数据库、知识 Repository 或第二知识真相源；
- 未提供默认或模型自行决定的跨 Memory 检索；
- 未增加 Agent 角色、自动写入、自动导入或绕过确认的行为；
- 未改变 S4 关闭语义功能时的默认检索链路。

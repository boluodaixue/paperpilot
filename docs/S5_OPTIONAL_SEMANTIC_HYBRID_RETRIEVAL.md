# 可选语义与混合检索

## 目标

在 Markdown 仍是唯一知识真相源的前提下，为当前 Memory 提供比关键词更好的中英文同义表达和跨笔记关系召回，同时保证本地模型不可用时全文检索仍然可用。

## 全文检索基线

Vault 外 SQLite FTS5 保存按 `vault_scope + memory_id` 隔离的文档和有界分块，包括路径、`chunk_id`、内容哈希、mtime、标题、frontmatter、正文和 WikiLink。

查询前按 mtime/大小增量同步并周期性全哈希 reconciliation；返回前重新验证文件仍存在、仍属于当前 Memory 且 SHA-256 与索引一致。索引可以删除后只从 Markdown 重建。

## 可选本地 embedding

语义检索由 `runtime.semantic_retrieval_enabled` 显式开启，默认关闭。默认模型是：

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

只为选定 `memory_id` 的有界分块生成 embedding。向量以以下身份缓存在同一 Vault 外 SQLite 数据库：

```text
vault_scope + memory_id + path + chunk_id + content_hash + model_id
```

Markdown 修改/删除、索引重建或模型版本变化会删除或重建对应缓存。

## 确定性融合

系统分别执行：

1. FTS5 精确/全文召回；
2. 当前 Memory 语义召回；
3. 一跳 WikiLink 和 backlink 邻居召回。

三路结果使用固定 `k=60` 的 RRF，权重分别为 `2.0 / 1.0 / 0.5`，再以 Vault 路径作为稳定次级排序键。精确关键词因此保持较高优先级，结果顺序可重复。

## 模型准备与状态

默认 `semantic_local_files_only: true`，PaperPilot 不在查询时偷偷下载模型。检查本地缓存：

```bash
python scripts/prepare_semantic_model.py --check
```

首次显式下载：

```bash
python scripts/prepare_semantic_model.py --download
```

脚本会实际加载模型并验证一个有界向量，输出维度和当前配置是否启用语义检索。Runtime 启动日志显示 `FTS5` 或 `hybrid` 配置；查询降级时记录异常类型和目标 `memory_id`，但不记录问题或 Markdown 正文。

## 失败降级

模型缺失、加载失败、向量数量/维度错误、NaN/无穷和零向量都会使本次查询明确降级到 SQLite FTS5。系统不生成伪随机向量，也不会因语义功能失败阻断 Memory 问答或继续研究。

模型推理期间不持有 SQLite 写事务；只有发布已计算的派生缓存时短暂取得写锁。

## 安全与一致性

- 不存在默认跨 Memory 查询 API；
- embedding 输入已经通过路径、UTF-8 和 Memory 边界检查；
- 最终命中必须再次通过真实 Markdown 路径和哈希复核；
- 缓存不能反向写 Markdown，也不是备份或知识 Repository；
- 送入模型前由问答/Workflow 入口再次校验 `memory_id`；
- 引用只接受本次有效命中的 Vault 相对路径。

## 当前验收

固定离线用例覆盖中英文同义表达、精确关键词、WikiLink 融合、内容哈希缓存、模型版本切换、模型失败/非法向量降级、严格 Memory 隔离和最终引用有效性。真实模型效果将在作品集 smoke test 中单独记录。

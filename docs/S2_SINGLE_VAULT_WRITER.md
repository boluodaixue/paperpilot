# 单一 Vault Writer 与崩溃一致性

## 问题

一个报告可能同时写入 report、evidence、source 和 Home。互斥锁只能避免两个线程同时写，不能保证进程在一组文件写到一半时重启后看到完整旧状态或完整新状态，也不能协调多个 Web/Research worker。

## 架构

```text
Workflow / API workers
        ↓
SQLite durable write queue
        ↓
single generation-fenced Vault Writer
        ↓
same-volume staging + journal
        ↓
Markdown Vault
```

队列是物理写入控制面，不是知识库，也不复制 LangGraph 工作流状态。终态 job 清除正文 command blob，只保留稳定身份、哈希、短 locator、状态和错误码。

## Writer lease 与幂等

每个规范 Vault 根映射到独立 `vault_scope`。全局 Writer lease 使用 owner、generation、过期时间和 fencing；同一 scope 只有一个 Writer 能发布。心跳同时续租 Writer 和 running job，失去任一租约的执行器必须停止。

写入命令包含稳定 `job_id`、幂等键、操作类型、`memory_id`、来源 thread、输入哈希、目标旧/新哈希和最小成功 receipt。完全相同的重试复用结果，不同输入的幂等键碰撞明确失败。

## Staging 与 journal

每个 job 在 Vault 同卷私有目录准备完整 stage、manifest 和 marker。私有路径同样逐段拒绝 symlink/junction/reparse。

目录创建在完整树校验后通过原子 no-replace rename 发布。多文件 bundle 先发布叶文件，最后发布 Home 或 report anchor；`PREPARED`、`LINEARIZED`、`COMPLETED` marker 定义恢复阶段。

replace 在原子交换前持久化 intent。addition 回滚先写 durable remove intent，再把实际 canonical 文件移入私有 quarantine 后校验真实字节，不使用脆弱的“检查哈希后直接删除”。

## 崩溃恢复

启动时 Writer 只处理入场时冻结的有限 job 集合。根据 journal 和真实文件哈希，一个 job 最终收敛到：

- 完整旧状态；
- 完整新状态；
- 保留外部内容的明确冲突；
- 底层 I/O 仍不可恢复的 nonterminal 状态。

anchor 发布后、数据库 success 前还会复核整个 manifest。外部修改或删除 leaf 不会被自动补回、覆盖或误报成功。

## 覆盖的写入

- 创建 managed Memory；
- 研究报告、证据和来源 bundle；
- 可选 Red/Blue 报告替换；
- 保存 Memory 问答笔记；
- PDF/text/URL 导入 bundle；
- legacy 迁移和安全归档。
- Research Agent 原始工具 artifact；以 thread scope 和内容哈希幂等发布，receipt 复核成功后才允许裁剪 Working Context。

## 可见性边界

file bundle 的线性化点是 Home/report anchor。进程在 anchor 前被强杀时，Obsidian 全局搜索可能短暂看到尚未被 anchor 引用的叶文件；启动恢复会回滚。完全消除恢复前窗口需要目录级间接发布，会破坏当前直观 Markdown 路径，因此没有采用。

无法证明可安全删除的 foreign artifact 会保留在私有 quarantine，等待人工核对。个人本地项目不额外建设 dead-letter 运维平台。

## 关键不变量

1. 只有持有当前 generation lease 的 Writer 可以发布；
2. stage 与 canonical 目标必须同卷且不能经过链接逃逸；
3. 外部编辑优先保留，不能静默覆盖；
4. Writer 成功前必须复核整个 manifest；
5. 恢复只重放物理 apply，不重复模型调用或 Red/Blue 审查。

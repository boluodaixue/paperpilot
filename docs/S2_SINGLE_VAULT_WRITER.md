# S2 单一 Vault Writer 与崩溃一致性实施记录

## 状态

已完成（2026-08-29）。S2 将所有产品级 managed Vault 写入统一接入持久化队列和单一 Writer，并完成多进程租约、幂等、同卷 staging、journal、原子发布、崩溃恢复和外部编辑保护。S2 没有开始 S3 Legacy 退役、S4 持久全文检索或 S5 语义检索。

## 数据职责

```text
LangGraph State + AsyncSqliteSaver
  = 工作流阶段、interrupt、审查结果和最终工作流结果

Runtime Registry + outbox
  = task/thread/session/Memory 定位、工作流租约和可靠终态事件

vault_write_jobs + vault_writer_lease
  = Vault 物理写入命令、幂等身份、Writer/job 租约和发布终态

Markdown Vault
  = 报告、证据、来源、笔记、导入和附件的唯一长期知识真相源
```

Vault Writer 队列是运行控制面，不是新的知识库，也不复制 LangGraph 工作流状态。终态 job 会清除正文 `command_blob`，只保留稳定身份、请求/结果哈希、短 locator、状态和错误码；Memory 标题等描述仍从 `Home.md` 重读。

## 已实现

- 在产品使用的 SQLite 数据库中增加按规范 Vault 根哈希隔离的 `vault_write_jobs` 和 `vault_writer_lease`；不同 Vault 不共享写入领导权。
- 全局 Writer lease 使用 owner、generation、过期时间和 fencing；同一时刻只有一个 Writer 可发布。心跳同时续全局 Writer 和唯一 running job，任一必要续租失败即停止本地执行器，过期后由其他进程接管。
- 写入命令包含稳定 `job_id`、幂等键、操作类型、`memory_id`、来源 thread、输入哈希、目标旧/新哈希、预期 Home 哈希和最小成功 receipt。时间戳不参与 create/research/report-review 的请求指纹，精确重试与不同输入碰撞可区分。
- 创建 Memory、managed research bundle、N6 报告审查、保存笔记、资料导入和 legacy copy 全部通过同一 `VaultWriteService`。`memory_id=None` 的历史根目录研究写入仅作为低层兼容 seam；产品入口仍要求 managed Memory，`M-legacy` 继续只读。
- 每个 job 在 `Memories/.paperpilot-writer/jobs/<job_id>/` 生成同卷私有 stage、manifest 和 marker。路径逐段拒绝 symlink/junction/reparse，验证仍在 Vault 内且与 canonical 目标同卷；不具备安全 no-replace 原语的平台 fail closed。
- directory create/legacy copy 先完整构建并验证私有树，写入 `TREE_READY` 后以原子 no-replace rename 发布整个 Memory 目录。
- file bundle 先发布受控叶文件，最后发布 Home 或 report anchor；`PREPARED`、`LINEARIZED`、`COMPLETED` marker 定义恢复阶段。启动时只处理入场时冻结的有限 job 集合，新请求不会使启动或普通等待无限延长。
- replace 在原子 exchange/ReplaceFile 前持久化 intent；若交换点出现外部内容，恢复会把最新用户内容还原到 canonical，或在无法无损合并时同时保留 canonical 与 private quarantine 并返回明确冲突。
- addition 回滚不使用 `hash + unlink`。Writer 先写 durable remove intent，再把实际 canonical 文件原子 no-replace 移入同卷 private quarantine，检查被移动的真实字节；foreign 内容无覆盖恢复，canonical 又出现新版本时两份都保留。
- anchor 发布后和 DB success 前复核整个 file bundle 或 directory manifest。外部修改或删除 leaf 不会被自动补回、覆盖或误报成功。
- CLI 同步等待目标 job；等待只驱动入场时冻结的有限 FIFO 前缀，能够接管崩溃遗留的 running 前序 job，同时不会顺带执行后入队任务。
- Web worker 可在本地缓存缺失时从 Runtime Registry + authoritative checkpoint 重建研究任务；SSE 以 Registry/checkpoint 为入口，能重放 research、note、import 和 legacy workflow 的 durable outbox 终态。同步 Memory 创建移到工作线程，不阻塞事件循环。
- N6 将既有 Red/Blue 审查与 Writer apply 分成两个普通 Workflow 节点：审查结果先 checkpoint，写入成功但节点尚未 checkpoint 时只重放 apply，不再次调用 Red/Blue。若 Obsidian 随后修改报告，Workflow 从 Vault 重读最新正文并记录明确 fallback，State 与 Markdown 不分叉。

## 崩溃与可见性边界

- 进程恢复完成后，一个 job 只收敛为完整旧状态、完整新状态或保留外部内容的明确冲突；不会以半发布状态标记成功。
- file bundle 的线性化点是 Home/report anchor。进程在 anchor 前被强杀时，Obsidian 全局文件搜索可能短暂看到尚未被 Home/report 引用的叶文件；启动恢复会回滚它们。消除此恢复前窗口需要改变 W0 路径/引用契约或目录级间接发布，不属于 S2。
- `conflict`/`failed` 的私有 journal 采用保守保留策略。特别是无法证明可安全删除的 foreign artifact 会以 `vault_conflict_quarantined` 留在隐藏 Writer 目录，等待人工核对；S2 不自动删除可能属于用户的字节，也没有新增重试/dead-letter 运维模型。
- 未知 I/O/runtime 错误会保留 nonterminal command 和 journal，供租约接管或下次启动重试。确定性磁盘错误可能阻塞后续 FIFO，需要运维修复底层故障后重启；自动退避/隔离策略未在 S2 计划中定义。

## 保持不变

- 同一个 Research Runtime、Research Workflow 和同质 Research AgentGraph；
- 根、子、孙执行身份、fork policy、递归上限、工具/时间/token/重试硬限制；
- `AsyncSqliteSaver` checkpointer、LangGraph interrupt/恢复语义和 Markdown/Chat Store 职责；
- W0 frontmatter、WikiLink、Vault/Memory 路径与旧根目录只读识别；
- N6 默认关闭、每次研究最多一次 Red/Blue 的约束；
- W6 legacy 迁移仍只复制，不移动或删除旧根目录。Legacy 退役只属于 S3。

## 验收

- S2 专项：`116 passed, 1 skipped`。
- S0–S1 回归：`115 passed, 1 skipped`。
- N1–N6 回归：`105 passed`。
- W0–W6 回归：`290 passed`。
- 仓库全量：`681 passed, 2 skipped`。
- 独立只读审计：通过。

专项覆盖两个真实进程共享 SQLite/Vault 的唯一 Writer 与同幂等发布、global/job 双租约心跳和过期接管、staging/leaf/anchor/marker/DB success 强制终止、replace/remove intent 的 syscall 后崩溃、directory tree 重建、外部编辑 CAS、private path 逃逸、有限队列等待、Web cache-miss/SSE 和 N6 checkpoint-window 精确重放。

两个 skip 均因当前 Windows 测试账户无创建特定 directory/file symlink 的权限；同环境下 junction/reparse 和 Writer private root/jobs/job-root 三层外链逃逸测试均实际通过。

## 下一阶段

下一阶段是 S3“Legacy 安全退役”，当前尚未开始。S2 不移动、不删除根目录 `reports/evidence/sources`，不新增 legacy path 映射或 Vault 外归档，也未实现 S4 FTS5 或 S5 embedding/混合检索。

# S1 持久化工作流状态与确认实施记录

## 状态

已完成（2026-08-28）。S1 只把产品工作流的状态、暂停点、确认与恢复迁移到持久化 LangGraph checkpoint，并增加薄 Runtime Registry；没有开始 S2 的 Vault 写入队列、单一 Writer、staging、journal 或多文件崩溃发布。

## 已实现

- 产品 Web 与 CLI 使用 `AsyncSqliteSaver`，由 FastAPI lifespan 或 CLI 异步上下文统一创建、注入和关闭；测试、离线评测与 benchmark 显式使用 `InMemorySaver`。
- 研究确认继续复用既有 Research Workflow/interrupt；State 新增 workflow 类型、阶段、session/Memory 身份、TTL 与有界失败码。普通 checkpoint 恢复不会被误当作确认恢复。
- Memory 回答/保存笔记、file/text/url 导入和 legacy 迁移分别进入三个最小 LangGraph Workflow。回答、提案、来源快照、内容哈希、确认决定与结果只以 checkpoint State 为准。
- Notepad 快照进入 Research Agent State，root/child/thread 通过 `ContextVar` 隔离；服务关闭并重新打开 SQLite 后仍能恢复，已完成 write 不会重复执行。
- 新增薄 SQLite Runtime Registry，只保存 task/thread/session/Memory/workflow 映射、创建/过期时间和恢复租约；Registry 不保存状态、Brief、回答、提案、结果、Markdown 或附件正文。
- 新增最小 outbox，只可靠保存 confirmed/completed/failed/cancelled/expired 事件及白名单短码；Web 与 CLI 复用同一 checkpoint→outbox 纯派生规则，普通 SSE 进度仍为进程内实时流，重连先读取 checkpoint 快照，再回放 outbox。
- 启动时以 Registry 定位 checkpoint，并由 checkpoint 的 interrupt、next 与 State 终态决定等待、恢复、完成、失败、取消或过期。空 checkpoint 只删除 orphan locator；身份不一致时同样只隔离错误 locator，绝不由 Registry 反向删除权威 checkpoint。
- Registry 租约使用 SQLite `BEGIN IMMEDIATE` 原子领取、后台续租、释放和到期接管；Web 与 CLI 执行器都在状态变更前后 fencing，CLI 等待终端输入时不阻塞心跳；本地执行器丢失租约即停止，sweeper 会在旧租约到期后重新调度 running Workflow，拒绝重复或并发确认。
- 提案 TTL 由后台 sweep 和启动 reconciliation 收口为 expired，终态保留期从终态 checkpoint 时间计算；session 删除先取得该会话全部 Workflow 的有效租约，再以同一 SQLite 事务删除 Chat、locator 与 outbox，之后删除已确定身份的 checkpoint，并保留 Vault 知识文件。
- Web 可从 checkpoint 恢复 Research Brief、Memory Answer、Note/Import/Migration 确认卡；Chat proposal 只保存 task/thread/Memory 指针，不复制 Brief。
- 前端切换会话只隐藏本地确认卡，不会静默取消服务端 Workflow；SSE 重连统一消费 checkpoint snapshot 与 outbox 终态，服务关闭前先停止全部后台执行器再关闭 saver。
- 研究报告、笔记、导入和迁移提交使用稳定身份，并在节点重试前识别完全一致的既有提交；既有内容或身份不一致明确冲突并保留 Vault 现状，不在 S1 做 S2 修复。

## 数据职责

```text
LangGraph State + AsyncSqliteSaver
  = 工作流阶段、interrupt、回答/提案、确认决定、结果

Runtime Registry + Outbox
  = session/task/thread 定位、TTL、恢复租约、必要可靠事件

Chat Store
  = 会话、消息、Memory 绑定、Markdown manifest 指针

Markdown Vault
  = 长期知识正文与附件的唯一真相源
```

checkpoint 与 Chat/Registry 表可以位于同一个 `chat.db`，但由各自独立表和职责管理。`memory_id`、`session_id` 与 `thread_id` 仍保持不同生命周期；恢复前会逐项校验。

## 兼容与边界

- `build_research_runtime()` 仍允许显式注入 saver，且无注入时保留 `InMemorySaver`，供 embedding 与历史测试使用；产品入口必须走持久化生命周期。
- Web 不再定义或读取旧进程内 answer/proposal 字典；历史 Web 测试也通过真实 checkpoint Workflow adapter 验证行为。
- LangGraph 自定义类型 serializer 的未来兼容警告按计划延期，不是 S1 阻塞项。
- 现有同质 Research AgentGraph、Research Workflow、fork policy、根→子→孙递归上限、硬预算、Markdown/Chat、FileReader 沙箱与 N6 可选 Red/Blue 均保持。
- S1 不保证多个进程直接并发写同一 Vault，也不提供多文件原子发布；这属于 S2。
- Legacy 文件仍按 W6 行为保留；退役属于 S3。
- 未增加持久检索索引、FTS、embedding、Repository、新 Agent 或自动知识写入。

## 验收

- S1 专项覆盖 FastAPI lifespan 与 SQLite saver 关闭/重开、四类确认点、普通 checkpoint 续跑、Notepad 恢复、Registry 租约心跳/到期接管/fencing/outbox、CLI 阻塞输入续租与失租停止、TTL、身份冲突隔离、重复/并发确认、session 原子删除和精确提交重放：`70 passed`。
- S0 回归：`44 passed, 1 skipped`。
- N1–N6 全量回归：`105 passed`。
- W0–W6 全量回归：`290 passed`。
- 仓库全量回归：`564 passed, 1 skipped`；内联 Web JavaScript 语法检查通过。

## 下一阶段

下一阶段是 S2“单一 Vault Writer 与崩溃一致性”，当前尚未开始。S2 才会引入持久写入队列、单 Writer lease、staging、journal、内容哈希复核和崩溃恢复。

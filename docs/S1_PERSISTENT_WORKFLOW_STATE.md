# LangGraph 持久化工作流状态

## 问题

研究、保存笔记、资料导入和迁移都可能停在“等待用户确认”。如果只把状态放在进程内字典，服务重启后即使 Markdown 还在，进行中的任务和待确认提案也会丢失。

## 方案

产品 Web 与 CLI 使用 `AsyncSqliteSaver` 持久化 LangGraph State。State 是任务阶段、interrupt、Research Brief、提案、确认决定、模型结果和最终结果的唯一工作流状态。

```text
LangGraph State + AsyncSqliteSaver
  = workflow phase / interrupt / proposal / decision / result

Runtime Registry + Outbox
  = session / task / thread locator / lease / durable event

Chat Store
  = sessions / messages / Memory binding / Markdown pointers

Markdown Vault
  = durable knowledge content and attachments
```

这些表可以共用一个 SQLite 文件，但职责不能互相复制。

## Runtime Registry 为什么仍然存在

LangGraph State 适合回答“某个 thread 运行到哪里”，但 Web 还需要从 `session_id` 或 `task_id` 找到 thread、协调哪个 worker 恢复任务，并为 SSE 重连保留必要终态事件。

Registry 因此只保存：

- task/thread/session/Memory/workflow 映射；
- 创建、过期和终态保留时间；
- 恢复调度 lease 与 fencing；
- confirmed/completed/failed/cancelled/expired 等必要 outbox 事件。

它不保存 Brief、回答、提案 Markdown、附件正文或第二份确认状态。

## 恢复流程

1. 启动时从 Registry 定位候选 thread；
2. 从 SQLite checkpointer 读取权威 State、interrupt 和 `next`；
3. 校验 session、thread 和 `memory_id` 身份；
4. 由 checkpoint 判断等待、继续、完成、失败、取消或过期；
5. worker 取得带期限 lease 后才执行恢复；
6. SSE 先发送 checkpoint 快照，再回放 durable outbox 终态。

空 checkpoint 或身份不一致只会隔离错误 locator，不会让 Registry 反向删除权威 checkpoint。

## 覆盖的工作流

- 研究计划修改、确认和继续执行；
- Memory 回答与“保存为笔记”确认；
- PDF/text/URL 导入预览与确认；
- legacy 迁移预览与确认；
- Notepad 和根/子 thread 执行上下文恢复。

## 生命周期

提案 TTL、终态保留时间和恢复 lease 都由 Runtime 配置控制。后台 sweeper 将过期提案收敛到 checkpoint 终态，并清理 locator/outbox；删除会话时先取得该会话相关 Workflow 的有效 lease，再删除 Chat、Registry 和已确认身份的 checkpoint，Vault 知识文件保持不变。

## 关键不变量

1. State 是工作流唯一真相，Registry 不能形成第二状态机；
2. 普通 checkpoint 续跑和用户确认恢复必须区分；
3. 同一个确认只能由一个持租约 worker 消费；
4. 已完成的副作用使用稳定身份，恢复时不能重复执行；
5. 服务关闭先停止后台执行器，再关闭 saver。

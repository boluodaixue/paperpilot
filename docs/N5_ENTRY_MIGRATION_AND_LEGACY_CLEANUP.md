# N5：生产入口迁移与 Legacy 清理

## 状态

已完成（2026-08-28）。N4 基线提交为 `82a4fa7`。

## 完成内容

- 新增 `src/research/runtime.py`，集中装配唯一 Research Workflow、模型、工具、`AgentLimits`、内存 checkpointer 和 Markdown Memory Store；
- CLI 默认展示 Research Brief，支持同一 root thread 上的修改与确认；只有显式 `--yes` 才自动确认；
- REPL 每个问题创建独立 root thread，session 只作为界面分组，不共享 Agent 上下文；
- Web 使用同一 graph/checkpointer 完成 alignment、修改、确认和执行，增加 `waiting_confirmation` 状态；
- Web 将 N4 结构化执行事件投射为带 sequence 的 append-only SSE，支持同进程游标回放与去重；
- ChatStore 只保存 UI 会话、消息和 Markdown manifest 引用，checkpoint 与知识均不以 ChatStore 为准；
- 评测与 benchmark 显式自动确认 Research Brief，并记录 status、evidence、source、thread、tool、token、retry、unresolved 和 manifest；
- HotpotQA 提取跳过 YAML frontmatter，引用覆盖率识别 `[[evidence/...]]`；
- Judge 与 Embedder 移入 `evaluation/`，不再属于产品执行核心；
- 默认配置只保留 research、外部 judge、Markdown memory、chat 和有效工具配置。

本阶段未新增持久化 checkpointer 依赖。确认、恢复和 SSE 回放只承诺同一进程生命周期；跨进程恢复需要后续单独对齐。

## 固定输入对照

N4 legacy 从提交 `82a4fa7` 独立导出并运行原固定离线测试，验证旧报告、串行节点和线程隔离行为。N5 使用相同固定问题运行新 Workflow。

| 项目 | N4 legacy | N5 Workflow |
|---|---|---|
| 最终交付 | `ResearchReport` Markdown | `ResearchWorkflowResult` + Markdown manifest |
| 来源 | 固定样例无来源 | 结构化 evidence，带稳定 `source_ref` |
| 关系表达 | 无 | 报告到 evidence、evidence 到 source 的 WikiLink |
| 执行身份 | 根 thread 快照 | root/parent/thread 身份贯穿 Workflow 与事件 |
| 成本字段 | 最终报告不暴露 token 计数 | 结构化 tool/thread/token/retry 计数 |
| 固定离线单测观测 | 关键调用约 `0.03s` | 关键调用约 `0.05s` |

两条路径都没有真实模型或付费网络调用。新路径的收益来自来源可定位性、结构化状态和恢复契约，不是通过增加真实 token 或等待时间取得。

## 删除内容

- `src/orchestrator/`、`src/planner/`、`src/agents/`；
- `src/evidence/`、`src/compressor/`；
- 旧 SQLite Shared/Long/Short Memory；
- 旧 `src/adversarial/` 与 `src/evolution/`；N6 将基于新报告契约重新实现可选 Red/Blue；
- 旧 core runner、ablation、baseline、批量实验总控、evidence backfill/graph 脚本；
- 只服务旧角色、图谱和实验体系的配置与测试；
- Web 的独立 clarifier、关系图 API、Graph UI 和二次 Obsidian 导出。

旧 SQLite 数据文件不由 N5 自动删除。会话删除也不会删除可能被多个报告引用的 evidence/source Markdown。

## 验收结果

- N5 runtime、CLI、Web、评测与静态架构专项：`58 passed`；
- N1–N5 全量回归：`119 passed`；
- N4 legacy 临时导出对照：`3 passed`；
- `compileall`、CLI help、Web import、wheel 构建、失效 import 扫描和 `git diff --check` 通过；
- 生产代码不存在旧 Orchestrator、Planner、AgentPool、Evidence Graph 或旧 Memory import。

## 下一阶段边界

N6 仅实现可关闭的 Red/Blue 报告后处理。它不得创建研究线程、替换完成判断、修改来源事实或引入第二套存储；N6 当前未开始。

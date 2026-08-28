# S3：Legacy 安全退役实施记录

## 状态

已完成（2026-08-29）。S3 只改变 W6 的长期 legacy 兼容策略；Research AgentGraph、Research Workflow、fork policy、递归与预算、LangGraph checkpointer、Markdown 知识真相源、Chat 正文职责和 N6 可选 Red/Blue 均未改变。S4/S5 未在本阶段实现。

## 已实现

- `research.legacy_archive_root` 是产品迁移的显式前置条件，默认配置指向活动 Vault 外的 `legacy-archives/`；缺失、不可读取、链接/reparse、位于 Vault 内或目标冲突都会在移动旧目录前拒绝。
- 迁移 checkpoint proposal 在原 W6 完整 Markdown 预览之外，固定保存受影响 session、历史 report manifest、完整 legacy path → managed path 映射、外部归档目标、全树内容清单和依赖快照哈希。
- S2 的持久队列、唯一 Writer lease、job lease 与 journal 被继续复用。Writer 先在外部目录生成并逐字节校验可恢复归档，再把 `reports/evidence/sources` 原子移入本 job 的私有保留区；只有归档与 managed staging 都完整时才进入最终切换。
- 最终切换在 SQLite fenced transaction 中写入 `legacy_path_mappings`、把全部当前 `M-legacy` session 改绑到目标 Memory，并发布 managed Memory 目录。Chat message/manifest 原文不改，也不复制 Markdown 正文；`ResearchRuntime.read_memory()` 仅在读取历史路径时应用持久映射。
- 若确认前源内容、session 或 manifest 快照改变，或归档目标冲突，旧目录保持在活动 Vault。切换前中断可从私有保留区恢复完整 legacy；切换后中断通过已发布 managed tree、外部归档和映射幂等收敛。
- 迁移完成后重新安全扫描活动 Vault，必须没有可识别 `M-legacy` Markdown；当前版本的发起会话及其他 legacy 会话均确定性改绑。
- 永久清理不是迁移的一部分。`prepare_legacy_archive_cleanup()` 返回归档内完整删除清单、逐文件哈希和一次性确认令牌；`delete_legacy_archive()` 只接受未变化的相同令牌。
- 无显式归档配置的低层、内存 checkpointer 嵌入测试仍可调用 W6 copy-only seam，用于冻结历史回归；产品 `open_research_runtime()` 使用持久数据库且不允许该兼容 seam。

## 数据职责

- Markdown Vault 仍是唯一知识真相源；外部归档只是用户可恢复的迁移前快照，不参与检索和回答。
- `legacy_path_mappings` 只保存旧指针定位信息、目标 Memory 和归档定位，不保存 Markdown 正文。
- session 改绑与 path mapping 与现有 Chat/Runtime SQLite 同库提交，不新增 Repository、Agent、索引或第二套知识存储。

## 验收覆盖

- 完整迁移后活动 Vault 无 legacy Markdown、归档完整、managed Memory 完整；
- 多个 legacy session 同步改绑，历史 Chat 原始 JSON 字节保持不变且旧 report path 可读取；
- 缺失归档根、源变化与归档目标冲突均不移动 legacy；
- 最终切换后强制失败可由相同持久 job 接管并完成；
- 永久清理必须经过独立清单与内容哈希令牌确认；
- S3 专项、S0–S2、N1–N6、W0–W6 与仓库全量回归通过。

验收结果：

- S3 专项：`5 passed`；
- 仓库全量（含 S0–S2、N1–N6、W0–W6）：`686 passed, 2 skipped`；
- `skipped` 为既有可选环境用例，本阶段未新增跳过项。

## 明确未实现

- 未实现 S4 FTS5、S5 embedding/混合检索、默认跨 Memory 检索或文件监控；
- 未改写历史 Chat manifest，未将外部归档加入 Obsidian 或检索；
- 未新增 Agent 角色、知识 Repository、后台自动迁移或自动永久删除；
- 未处理已延期的 LangGraph 自定义类型 serializer 未来兼容警告。

# LLM Wiki + Obsidian 实施计划

## 1. 状态与唯一边界

本计划已于 2026-08-28 确认，是 [LLM Wiki + Obsidian 目标架构](LLM_WIKI_OBSIDIAN_ARCHITECTURE.md) 的唯一实施顺序和验收边界。

实施不得为追求“Wiki 完整度”而新建复杂阅读器、图数据库、向量数据库、Wiki Agent 或第二套存储。任何超出本计划的新服务边界、持久化模型或产品行为都必须先与用户对齐。

## 2. 阶段总览

| 阶段 | 目标 | 产品结果 |
|---|---|---|
| W0 | 契约与安全基础 | 稳定 `memory_id`、Vault 路径和 Markdown 契约 |
| W1 | 多 Memory 持久化 | 新建/列出/选择 Memory，研究写入指定 Memory |
| W2 | Obsidian 最小接入 | 在 Obsidian 中直接打开 Memory，无自建阅读器 |
| W3 | 基于旧 Memory 继续研究 | 旧笔记进入 Research Brief，新结果回写同一 Memory |
| W4 | Memory 问答与受控新建笔记 | 带 WikiLink 回答，用户确认后新建 Markdown |
| W5 | 资料导入与整理 | PDF/网页/文本进入同一 Memory，变更仍受控 |
| W6 | 稳定化与入口收口 | CLI/Web/评测、迁移、恢复和文档统一 |

W0–W6 是一条主线。每阶段必须在上一阶段验收后再开始，不并行创建替代契约。

## 3. W0：Memory/Vault 契约与安全基础

### 状态

已完成（2026-08-28）。实施与验收结果见 [W0_MEMORY_VAULT_CONTRACT.md](W0_MEMORY_VAULT_CONTRACT.md)。

### 目标

在不改变 Research AgentGraph 的前提下，定义长期 `memory_id` 和可安全操作的 Vault 路径。

### 工作

- 新增最小 `MemoryDescriptor`：`memory_id`、`title`、`relative_path`、`created_at`、`updated_at`；
- 将 `memory_id` 与 `session_id` / `thread_id` 明确分离；
- 配置中将现有 `research.memory_root` 收敛为 Vault 根路径，保持旧配置可读；
- 实现 Memory 路径、稳定 ID、frontmatter 和 WikiLink 的纯函数校验；
- 实现 Vault 内路径解析，拒绝绝对路径、`..`、symlink 逃逸和非 Markdown 目标；
- 为既有根目录 `reports/evidence/sources` 建立只读兼容视图，不自动搬移。

### 不做

- 不新增问答；
- 不改前端；
- 不写入 `.obsidian/`；
- 不迁移用户文件；
- 不引入数据库索引。

### 验收

- 一个 Memory 可以稳定定位到 `Memories/M-<id>/`；
- `memory_id` 不因标题和文件名改变；
- 路径逃逸、重复 ID 和非法 frontmatter 有确定性测试；
- 既有 MemoryManifest 和 N1–N6 回归不被破坏。

## 4. W1：多 Memory 持久化

### 状态

已完成（2026-08-28）。实施与验收结果见 [W1_MULTI_MEMORY_PERSISTENCE.md](W1_MULTI_MEMORY_PERSISTENCE.md)。

### 目标

让一个 Vault 包含多个可新建、列出、选择和持续追加的 Memory。

### 工作

- 在现有 Markdown Memory Store 中加入 Memory 目录能力，不新建另一个 Repository 层；
- 新建 Memory 时原子生成 `Home.md` 和必要子目录；
- Research Workflow 入口接收 `memory_id`；
- 报告、证据和来源写入当前 Memory 的子目录；
- WikiLink 改为 Vault 根目录相对完整路径；
- Chat Store 仅保存 `memory_id` 和 manifest 指针，不复制 Markdown 知识；
- 同一 Memory 的多次研究产生多份报告，不覆盖历史报告。

### 验收

- 两个 Memory 的报告、证据、来源和 Home 文件彻底隔离；
- 同一 Memory 两次研究的历史都存在；
- 不同 Memory 可使用无歧义完整路径建立 WikiLink；
- 旧 N2/N5/N6 的单 Memory 调用可以按明确兼容入口运行；
- 并发新建和写入不产生重复或半成品目录。

## 5. W2：Obsidian 最小接入

### 状态

已完成（2026-08-28）。实施与验收结果见 [W2_OBSIDIAN_MINIMAL_INTEGRATION.md](W2_OBSIDIAN_MINIMAL_INTEGRATION.md)。

### 目标

不建阅读器，让用户从 PaperPilot 一键打开指定 Memory。

### 工作

- 从 Vault 名称/路径和目标笔记生成编码正确的 `obsidian://open` URI；
- Web 增加 Memory 选择器、新建 Memory 和“在 Obsidian 中打开”；
- CLI 在研究完成后输出 Vault 路径、`Home.md` 路径和 Obsidian URI；
- 提供简短首次配置说明：用户手工将 Vault 根目录打开为 Obsidian Vault。

### 不做

- 不检测或强制安装 Obsidian；
- 不写 `.obsidian/`；
- 不调用 Obsidian URI 直接写入笔记；
- 不开发 Obsidian 插件。

### 验收

- 中文、空格和嵌套路径均能产生正确 URI；
- 按钮打开指定 Memory `Home.md`；
- Obsidian 编辑后 PaperPilot 下次扫描读取最新文件；
- 未安装 Obsidian 不影响 PaperPilot 研究与持久化。

## 6. W3：基于旧 Memory 继续研究

### 状态

已完成（2026-08-28）。实施与验收结果见 [W3_CONTINUE_RESEARCH_FROM_MEMORY.md](W3_CONTINUE_RESEARCH_FROM_MEMORY.md)。

### 目标

使同一 Memory 成为可持续增长的研究上下文，而不是一次性报告容器。

### 工作

- 扫描当前 Memory 的 frontmatter、标题、全文和 WikiLink；
- 建立进程内、可从 Markdown 重建的最小索引；
- 使用确定性关键词/标题/WikiLink 筛选相关笔记；
- 将命中笔记的路径和摘要注入根 Research Agent 的对齐上下文；
- Research Brief 展示已使用记忆、已知信息和新研究空白；
- 确认后仍调用现有 Research Workflow，结果写入同一 Memory。

### 不做

- 不跳过 Research Brief；
- 不为 Wiki 重建一个 AgentGraph；
- 不把整个 Memory 全部塞入上下文；
- 不在本阶段引入 embedding 或向量数据库。

### 验收

- 旧笔记能影响新 Research Brief，且可向用户说明具体使用了哪些文件；
- 无关 Memory 默认不进入上下文；
- 新报告不覆盖旧报告；
- 继续研究不改变 fork policy、递归上限和执行身份。

## 7. W4：Memory 问答与受控新建笔记

### 状态

已完成（2026-08-28）。实施与验收结果见 [W4_MEMORY_QA_CONTROLLED_NOTES.md](W4_MEMORY_QA_CONTROLLED_NOTES.md)。

### 目标

用户可以基于旧 Memory 直接问答，并选择性地把结果写为新 Markdown。

### 工作

- 增加 Memory 问答入口，默认仅使用当前 Memory；
- 回答必须返回所用笔记的 Vault 根目录相对路径和 WikiLink；
- 引用不足时明确标注，不自动切换到网络研究；
- 增加“保存为笔记”动作；
- policy 输出完整 Markdown 提案，根据稳定 note ID 生成新路径；
- 前端展示提案，用户确认后原子新建笔记并受控更新 `Home.md`；
- 使用内容哈希阻止覆盖 Obsidian 中的并发修改。

### 验收

- 回答中的引用能在 Obsidian 中直接打开；
- 普通问答不产生新文件；
- 未确认的提案不写入文件；
- 确认后只新建笔记和更新对应 Home 链接；
- 目标文件被 Obsidian 修改后，旧提案必须拒绝写入；
- 路径、frontmatter、WikiLink 和原子写入测试通过。

## 8. W5：资料导入与整理

### 状态

已完成（2026-08-28）。实施与验收结果见 [W5_CONTROLLED_IMPORTS.md](W5_CONTROLLED_IMPORTS.md)。

### 目标

让用户把自有论文、网页或文本放入指定 Memory，同时保留来源和用户确认。

### 工作

- 支持本地 PDF/文本文件和显式 URL 导入；
- 原始文件放入 `attachments/`，结构化提取结果放入 `imports/`；
- 使用来源引用、内容哈希和 locator 去重；
- LLM 可提议新笔记、WikiLink、支持/冲突/空白，但不直接写入；
- 用户确认后再原子完成一组文件变更；
- 导入失败不改变已有 Memory。

### 不做

- 不监视用户整个文件系统；
- 不自动下载未确认的大文件；
- 不批量重写旧笔记；
- 不引入 Review Repository 或 Import Repository。

### 验收

- 同一资料重复导入不产生重复附件或笔记；
- 所有提取内容可定位到原始文件或 URL；
- 未确认提案不改变 Memory；
- 确认变更不产生断链、路径逃逸或半成品文件；
- Obsidian 能直接阅读生成笔记和附件链接。

### 实施结果

- Web 支持用户明确选择的 PDF/UTF-8 文本、粘贴文本和显式 URL；原始内容进入 content-addressed `attachments/`，按页/行/网页段定位的提取结果进入 `imports/`；
- `source_ref + locator + content_hash` 精确三元组在准备和提交内均去重，内容相同但来源不同时只共享 attachment，不丢失来源证明；
- policy 只能根据有界提取片段与当前 Memory 命中提议标题、摘要、支持、冲突和空白，PaperPilot 过滤伪造 locator/路径/WikiLink/HTML 并确定性生成一篇 Import 和一篇 Note；
- 未确认时 Vault 不变；确认后在单 PaperPilot 进程共享的 Vault `RLock` 内成组发布 attachment/import/note，用经内容哈希复核的 Home 原子替换作为线性化点，失败回滚保留已被其他完成 Import 引用的共享 attachment；
- URL 只在显式预览时读取，并限制 scheme/端口/凭据、每跳公网 DNS/IP、重定向、identity encoding、流式大小和总超时；attachment 路径、Markdown、frontmatter、WikiLink、跨 Memory 与 symlink/junction 均经安全校验；
- W5 专项 `61 passed, 1 warning`，原 N1–N6 + W0–W4 前序回归 `350 passed, 1 warning`，包含 W5 的仓库全量回归 `411 passed, 1 warning`；warning 为既有 `StarletteDeprecationWarning`。

W5 没有实现 CLI 导入收口、legacy 迁移、文件树、内置阅读器、监控、Repository、持久化索引或新 Agent。W5 也不引入 journal、bundle 或 cross-process lock；保证范围是共享同一 Vault `RLock` 的单 PaperPilot 进程，多 PaperPilot 进程同时写同一 Vault 不在 W5 支持面。

## 9. W6：稳定化、迁移与入口收口

### 状态

已完成（2026-08-28）。实施与验收结果见 [W6_STABILIZATION_MIGRATION_AND_ENTRY.md](W6_STABILIZATION_MIGRATION_AND_ENTRY.md)。W6 没有增加 cross-process lock、journal 或 bundle。

### 目标

将闭环变成 CLI/Web 默认可用能力，并明确处理既有 Memory 数据。

### 工作

- CLI 和 Web 共用同一个 Memory 选择、检索和写入装配入口；
- Web 会话固定显示当前 `memory_id`，不隐式切换；
- 完成既有根目录 Memory 的只读兼容与显式迁移命令/界面；
- 迁移前生成预览，迁移中原子切换，迁移失败可回退；
- 补齐 Langfuse 中的 `memory_id`、检索文件和写入结果上下文；
- 增加固定离线评测：检索命中、引用完整、无依据拒答、受控写入和继续研究；
- 更新 README、架构、实施记录和路线图。

### 验收

- 用户可完成“选择 Memory → 问答 → 保存笔记 → Obsidian 编辑 → 再问答 → 补充研究 → 回写同一 Memory”；
- 全程不依赖内置 Markdown 阅读器；
- Markdown Vault 是唯一知识真相源；
- Obsidian 未安装、未打开或外部编辑冲突时，PaperPilot 不丢数据也不静默覆盖；
- N1–N6 研究、递归、恢复、报告和可选 Red/Blue 全量回归通过。

### 实施结果

- CLI/Web 真实写入入口统一通过 `ResearchRuntime` 要求显式 managed Memory；Web session 持久化 nullable `memory_id` 并拒绝后续切换；
- 虚拟 `M-legacy` 只读检索/问答，研究、笔记、导入和 managed 写入均拒绝；CLI/Web 提供完整预览后的显式迁移；
- 迁移用二次源快照、完整 staging 校验和单次同卷目录 rename 发布新 managed 副本；legacy 根文件、旧 Chat manifest 与当前 session 绑定不移动、不改写、不自动切换；
- Langfuse 补齐 `memory_id`、检索路径/分数和写入路径/状态，不上传问题、Markdown 正文或附件字节；
- 固定离线 `memory_wiki` 五项评测 `5/5 passed`；W6 专项 `39 passed, 1 warning`，原 N1–N6 回归 `160 passed, 1 warning`，前序集合 `411 passed, 1 warning`，仓库全量 `450 passed, 1 warning`；pytest warning 为既有 `StarletteDeprecationWarning`；
- 未新增领域模型、Service、Repository、持久化索引、Agent 角色、自动写入、第二套存储或内置阅读器。

## 10. 每阶段测试顺序

1. 纯函数契约和路径安全测试；
2. Markdown 快照、frontmatter 和 WikiLink 完整性；
3. 固定离线检索/对话/导入测试；
4. 多 Memory、会话和执行身份隔离；
5. 并发修改、失败回退和 checkpoint 恢复；
6. CLI/Web 入口测试；
7. 当前阶段专项；
8. 全量回归。

真实 Obsidian 启动与真实模型/网络作为单独手工验收，不代替确定性测试。

## 11. 立即执行顺序

```text
W0 Memory/Vault 契约与安全基础
→ W1 多 Memory 持久化
→ W2 Obsidian 最小接入
→ W3 基于旧 Memory 继续研究
→ W4 Memory 问答与受控新建笔记
→ W5 资料导入与整理
→ W6 稳定化、迁移与入口收口
```

W0–W6 已按顺序全部完成。该主线没有隐含的下一阶段；任何新增数据模型、服务、索引、Agent 角色、自动写入、跨进程事务或阅读器能力都必须另行定义和对齐。

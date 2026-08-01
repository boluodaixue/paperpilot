# W5：资料导入与受控整理

## 状态

已完成（2026-08-28）。W5 只把用户明确提供的 PDF、UTF-8 文本、粘贴文本或显式 URL 整理为当前 Memory 内的受控导入提案；只有用户确认才写入 `attachments/`、`imports/`、`notes/` 和对应 `Home.md`。W6 稳定化、迁移与入口收口尚未开始。

## 目标与边界

W5 复用既有 `MarkdownMemoryStore`、W3 当前 Memory 检索和同一 policy，实现“选择来源 → 提取与整理 → 预览完整提案 → 确认写入”。准备提案不写 Vault，policy 不暴露工具、不选择写入路径，PaperPilot 确定性生成路径、frontmatter、WikiLink 和最终 Markdown。

Markdown Vault 仍是唯一知识真相源。Research AgentGraph、Research Workflow、fork policy、递归上限、checkpointer、Chat Store 和可选 N6 Red/Blue 均保持原有职责。W5 没有新增 Agent 角色、Repository、数据库、持久化索引或第二套存储。

## 已完成

- 支持用户明确选择的本地 `.pdf`、`.txt`、`.md`、`.markdown` 文件、直接粘贴的文本，以及显式输入的 HTTP/HTTPS URL；Markdown 原文按普通 UTF-8 文本导入，不被误认为 PaperPilot 管理的 Markdown 笔记；
- 导入原始内容使用完整 SHA-256 寻址，规范路径为 `Memories/M-.../attachments/Asset-<64hex>.(pdf|txt|html)`；结构化提取结果写入 `imports/Import-<id>.md`，policy 整理结果写入一篇 `notes/Note-import-<id>.md`；
- PDF 按页保留 `page:<n>` locator，文本按行段保留 `lines:<start>-<end>` locator，网页按可见内容段保留 `section:<n>` locator；Import Markdown 保留来源引用、最终 URL/文档定位、内容哈希和原始附件 WikiLink；
- 使用 `source_ref + locator + content_hash` 精确三元组去重；完全相同的资料直接返回 duplicate，不再提取、调用 policy 或写文件；不同来源引用若内容相同，可共用同一个内容寻址附件，仍保留各自来源记录；
- policy 只获得有界的提取片段和当前 Memory 命中，只能提议标题、摘要、支持、冲突和空白；PaperPilot 过滤伪造 locator、越界 Memory 路径、模型 WikiLink 和原始 HTML，再确定性渲染 Import 与 Note Markdown；
- 原始内容、提取内容和模型文本均按 Markdown 字面内容进行转义，不允许导入内容伪造 frontmatter、WikiLink、HTML 或写入路径；
- 原始资料与提取结果均有硬上限：原始输入最大 10 MiB，PDF 最多 200 页，提取文本最多 200,000 字符，policy 上下文最多 48,000 字符和 64 个 locator；
- URL 只在用户点击“生成导入预览”后读取，不跟随页面内链接；仅允许无凭据、默认端口的 HTTP/HTTPS，每次 DNS 解析和最多 3 次重定向均重新校验并固定公网 IP，拒绝 loopback、private、link-local、multicast、reserved 和元数据地址；使用 identity encoding、流式大小上限和 15 秒总超时；
- 生成提案和取消提案不改变 Vault；Web 只在当前进程内临时保存待确认提案，响应不暴露附件字节或 base64，用户可完整预览 Import/Note Markdown、目标路径和 Obsidian URI；
- 确认时在当前 PaperPilot 进程的 Vault 共享 `RLock` 内进行一组受控提交：用不覆盖的硬链接发布 attachment/import/note，最后以经内容哈希复核的 `Home.md` 原子替换作为线性化点；Home 同时且各一次增加 Import 和 Note 链接；
- 目标已存在、Home 被 Obsidian 外部修改、路径越界、symlink/junction 逃逸或完整性校验失败时拒绝写入；提交中失败会按文件身份回滚本次新建文件，不删除同名外部文件；
- 已修复“共享 attachment 被失败事务回滚删除”的并发边界：回滚前扫描已经 Home 线性化的受管 Import，已被完成导入引用的内容寻址附件必须保留；
- `ResearchRuntime` 只增加准备 file/text/URL 导入和确认提交的薄入口，没有改变 Research Workflow 或 Chat Store；Web 增加当前 Memory 的“导入资料”卡片、预览、确认和取消端点。

## 去重、提案与写入契约

导入身份由规范 `source_ref`、稳定 locator 和原始内容 SHA-256 的精确三元组确定。三项全部相同才是同一导入；只有内容相同时仅共享附件，不合并不同来源证明。提交前会在 Store 内重新执行去重，因此同一资料的并发确认中，后到者返回 duplicate，不留下第二组文件。

成功确认会产生或复用一个 content-addressed attachment，新建一篇 Import、一篇整理 Note，并在对应 `Home.md` 的唯一 Imports/Notes 区段加入完整 WikiLink。`Home.md` 的内容哈希和最终原子替换是该组变更对外成功的判定点；确认失败时，本次未线性化的新文件会回滚，已有 Memory 内容保持不变。

W5 不引入 journal、bundle 布局或 cross-process lock。支持面是共享同一 Vault 锁的单 PaperPilot 进程；多个 PaperPilot 进程同时写同一 Vault 不在 W5 保证范围。W6 也不因此文档自动承诺跨进程锁或崩溃日志，如需扩展必须另行对齐。

## 明确未实现

- 不实现 W6 的 CLI 导入收口、legacy 迁移命令/界面、迁移预览或入口统一；
- 不监视 Vault 或用户文件系统，不扫描目录、自动发现文件或跟随网页内链接；
- 不自动下载未经用户显式预览的 URL 或大文件，不批量导入、批量改写旧笔记或自动修复链接；
- 不提供文件树、PDF/内置 Markdown 阅读器、编辑器、Backlink 面板或 Obsidian 插件；
- 不引入 Import Repository、Review Repository、持久化索引、向量数据库、图数据库或第二套知识存储；
- 不新增 Import Agent、Wiki Agent、Review Agent 或新 AgentGraph，policy 不使用工具且不直接写入；
- 不修改 Research AgentGraph、Research Workflow、fork policy、递归上限、checkpointer、Chat Store 或 N6 Red/Blue；
- 不保证多 PaperPilot 进程对同一 Vault 的并发写入，不引入 journal 或 bundle 交易布局。

## 验收映射

- **重复导入不增殖**：精确三元组在准备与提交内均复核；顺序重复、同时确认和内容相同但来源不同的共享附件都有确定性测试；
- **提取可定位**：Import frontmatter 与正文保留 `source_ref`、locator、media type、byte size、content hash 和原始附件 WikiLink，PDF/文本/HTML 片段都有稳定 locator；
- **未确认零写入**：file/text/URL 准备、policy 整理、Web 预览和取消前后的 Vault 快照不变；
- **确认时路径与成组安全**：规范 attachment/import/note/Home 路径、frontmatter、WikiLink、附件哈希、Memory 身份、symlink/junction、Home 冲突、半成品回滚和共享附件保留均有测试；
- **Obsidian 可读**：Import、Note、Home 和附件链接均使用经校验的 Vault 相对路径，Web 返回对应的安全 `obsidian://open` URI；
- **URL/Markdown 安全**：实现检查与针对性测试覆盖非公网地址、DNS rebinding/重定向逃逸、非默认端口、超时、超大或非 identity encoding 响应、不支持的 media type、恶意 Markdown/HTML 与伪造模型引用；
- **Web 用户确认**：三种来源请求、base64/声明大小、容量错误 `413`、预览无原始字节、duplicate、取消、严格 Memory/proposal 匹配、确认冲突 `409` 和前端脚本语法均有确定性测试。

## 验收结果

- W5 专项：`61 passed, 1 warning`；
- 原 N1–N6 + W0–W4 前序回归：`350 passed, 1 warning`；
- 包含 W5 的仓库全量回归：`411 passed, 1 warning`；
- warning 为既有 `StarletteDeprecationWarning`。

W5 正式完成，W6 尚未开始。

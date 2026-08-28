# W2：Obsidian 最小接入

## 状态

已完成（2026-08-28）。W2 只让 PaperPilot 定位并打开指定 Memory，不建设 Markdown 阅读器，也没有开始 W3 的 Memory 检索或继续研究。

## 目标与边界

W2 在 W0 路径安全契约和 W1 多 Memory 持久化之上增加标准 `obsidian://open` 链接、Web Memory 控件和 CLI 位置输出。Markdown Vault 仍是唯一知识真相源，Obsidian 只负责阅读和人工编辑。

本阶段没有新增数据模型、Repository、服务、索引、Agent 角色或持久化存储。Research AgentGraph、Research Workflow、fork policy、递归上限、checkpointer、Markdown Memory Store、Chat Store 和可选 N6 Red/Blue 均沿用原实现。

## 已完成

- 新增纯函数 `build_obsidian_open_uri`，复用 W0 `resolve_vault_markdown_path` 校验目标；
- 显式配置 `research.vault_name` 时生成 `vault` + `file` URI；未配置时生成绝对 `path` URI，不从目录名猜测 Vault 名称；
- 查询参数统一使用 UTF-8 percent-encoding，空格编码为 `%20`，路径分隔符编码为 `%2F`；Windows 绝对路径稳定转换为正斜杠；
- 拒绝空白 Vault 名称、绝对/遍历路径、非 `.md` 目标和 symlink/junction 逃逸；
- Web 增加 `GET /api/memories` 与 `POST /api/memories`，直接复用 W1 Runtime 创建和列出 Memory，不维护第二份目录索引；
- Web 顶部增加 Memory 选择器、新建按钮和普通 `obsidian://` 链接；待确认或运行中的任务锁定原 `memory_id`，不允许中途切换；
- Web 新研究和方案修改继续通过原 `/api/alignment`，只增加已选 `memory_id`，没有新的研究入口；
- CLI 单次与 REPL 增加可选 `--memory-id`，managed 研究完成后输出 Vault、`Home.md`、Obsidian URI 和报告绝对路径；
- `memory_id=None` 的 legacy CLI 调用继续兼容，并明确显示 Home/URI 不可用，不伪造或移动既有根目录文件；
- Memory API 每次调用 W1 的 `list_memories` 重新读取 `Home.md`，Obsidian 外部标题编辑会在下一次读取中生效；
- 默认配置保留 `research.vault_root`，并说明可选 `research.vault_name`；README 提供首次手工打开 Vault 的步骤。

## 明确未实现

- 不检测、安装或服务端启动 Obsidian；
- 不写 `.obsidian/`，不开发 Obsidian 插件；
- 不使用 `obsidian://new`、`append`、`prepend` 或 URI 写入笔记；
- 不实现文件树、Markdown 编辑器、Backlink 面板、图谱或 PDF 阅读器；
- 不实现 W3 扫描/检索、旧 Memory 注入 Research Brief 或继续研究语义；
- 不实现 W4 Memory 问答、保存回答、受控更新 Home 或内容哈希冲突处理；
- 不实现 W5 导入或 W6 legacy 迁移入口。

## 验收结果

- W2 URI、CLI、Web 联合专项：`34 passed, 1 warning`；
- 原 N1–N6 + W0–W1 回归：`259 passed, 1 warning`；
- 包含 W2 的仓库全量回归：`293 passed, 1 warning`；
- warning 仍是既有 `StarletteDeprecationWarning`；
- Python 编译、前端脚本语法、架构范围扫描和 `git diff --check` 通过。

W2 正式完成。W3 尚未开始。

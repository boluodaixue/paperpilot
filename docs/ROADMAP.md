# PaperPilot Roadmap

> 路线图只记录当前目标架构的进度。详细任务和验收标准见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

## 当前判断

N0–N6 与 LLM Wiki + Obsidian 的 W0–W6 已完成并提交。CLI、Web 和评测继续进入同一个 Research Workflow；旧 Manager / Planner / DAG / AgentPool / Evidence Graph 链路已经删除。W6 只收口 Memory 入口、会话绑定、legacy 只读迁移、可观测性和固定离线评测，没有改变 Research AgentGraph、Research Workflow、fork、递归、checkpointer 或 N6 Red/Blue，也没有引入第二套知识存储。

独立主线 S0–S5 已进入实施：S0 文件读取沙箱已完成，CLI/Web/评测不再装配无限制 FileReader，研究运行只授权当前 managed Memory；S1 尚未开始。后续才会用 `AsyncSqliteSaver` 持久化 LangGraph State，并依次引入薄 Runtime Registry、单一 Vault Writer、legacy 退役和可重建检索索引。详细边界见 [S 系列实施计划](S_PRODUCTION_HARDENING_AND_RETRIEVAL_PLAN.md)。

## 进度

| 阶段 | 状态 | 完成标志 |
|---|---|---|
| N0 文档与架构收敛 | ✅ | 唯一架构、唯一计划、旧方案退出活跃文档 |
| N1 单个同质 Research AgentGraph | ✅ | 同一图以不同深度执行并返回带来源的结构化结果 |
| N2 用户确认与单 Agent 纵向闭环 | ✅ | 可修改、确认、恢复，并写出互链 Markdown |
| N3 同质并行 Fork | ✅ | 三种 fork 条件、上下文隔离、并行汇聚和部分失败可用 |
| N4 一层递归与硬停止 | ✅ | 根→子→孙可运行，孙不可再 fork，限制与恢复可靠 |
| N5 入口迁移与旧实现清理 | ✅ | CLI/Web/评测只走新路径，旧架构及证据图退出代码库 |
| N6 可选 Red/Blue | ✅ | 默认关闭的单次报告后处理，且不破坏 frontmatter、WikiLink、URL 或 manifest |
| W0 Memory/Vault 契约 | ✅ | 稳定 `memory_id`、Vault 路径、frontmatter/WikiLink 安全与 legacy 只读识别 |
| W1 多 Memory 持久化 | ✅ | 原子创建/列出/选择 Memory，研究隔离写入并保留历史 |
| W2 Obsidian 最小接入 | ✅ | Web/CLI 可定位并打开指定 Memory `Home.md`，不自建阅读器 |
| W3 基于旧 Memory 继续研究 | ✅ | 旧笔记进入 Research Brief，新报告回写同一 Memory且保留历史 |
| W4 Memory 问答与受控新建笔记 | ✅ | 带可打开引用的当前 Memory 回答，确认后才新建笔记并更新 Home |
| W5 资料导入与整理 | ✅ | file/PDF/text/显式 URL 生成受控提案，确认后写入 attachment/import/note 并线性化 Home |
| W6 稳定化、迁移与入口收口 | ✅ | CLI/Web 固定 Memory、legacy 显式原子发布、Memory trace 与固定离线评测完成 |
| S0 本地文件读取沙箱 | ✅ | 默认拒绝，只读取当前 Memory/受控上传范围，路径与内容有界 |
| S1 持久化工作流状态与确认 | ⬜ | 未开始；AsyncSqliteSaver、State 唯一真相、薄 Runtime Registry |
| S2 单一 Vault Writer | ⬜ | 未开始；持久队列、幂等、journal 与崩溃恢复 |
| S3 Legacy 安全退役 | ⬜ | 未开始；活动 Vault 只留 managed Memory，历史指针可解析 |
| S4 持久化全文检索 | ⬜ | 未开始；可重建的 SQLite FTS5 增量索引 |
| S5 可选语义与混合检索 | ⬜ | 未开始；严格 Memory 范围的关键词/语义/WikiLink 融合 |

## N0 已确定的架构决策

- 根、子、孙 Agent 使用同一个 Research AgentGraph；
- 规划、fork 判断、研究、汇聚和本级总结都是同一个 AgentLoop 的能力；
- 根 Agent 仅额外拥有用户交互和最终报告发布权限；
- 递归深度为根 `0`、子 `1`、孙 `2`，孙禁止 fork；
- fork 条件为可并行、需上下文隔离、预计工具链至少三层；
- LangGraph 管状态、路由、并行、汇聚、暂停和恢复；
- checkpointer 保存运行状态，Markdown Memory Store 保存持久知识；
- 报告、证据、来源全部写入一个 Memory Store；
- 使用 WikiLink 和 Obsidian backlinks，不建设 Evidence Graph；
- 当前不实现 RCS，完成判断由 Agent 加硬规则完成；
- Red/Blue 是最终报告的可选后处理；
- 旧模块按“复用、迁移能力、删除”逐项审查，不强行兼容。

## N1 已完成

N1 已建立独立于旧 Orchestrator 的同质 AgentGraph：

- 最小任务、执行上下文、结果和证据契约；
- 思考、行动路由、工具调用、本级总结；
- 根/子共用同一图定义；
- 上下文与 checkpoint 隔离；
- Langfuse 旁路追踪；
- 固定离线输入、失败和预算停止测试；
- 现有 `MockWebSearchTool` 协议复用验证。

N1 专项 `13 passed`，全量回归 `201 passed`。详见 [N1 实施记录](N1_HOMOGENEOUS_AGENT_GRAPH.md)。

## N2 已完成

- 根 Agent 生成研究说明并通过 LangGraph interrupt 等待用户；
- 用户可以连续修改，确认前不会调用研究工具；
- 确认后调用 N1 的同质 AgentGraph；
- 根 Agent 的结构化结果渲染为最终 Markdown 报告；
- 报告、采用的证据和来源写入同一个 Markdown Memory Store；
- WikiLink 可解析，重复提交幂等，确认点和持久化失败可以恢复；
- 持久化失败重试不会重复执行已经完成的研究工具。

N2 专项 `8 passed`，N1+N2 联合专项 `21 passed`，全量回归 `209 passed`。详见 [N2 实施记录](N2_CONFIRMATION_AND_MEMORY.md)。

## N3 已完成

- 三种 fork 条件与任务完整性、依赖、去重、深度、子线程预算门槛已实现；
- 根 Agent 可并发运行同质子 Agent，父子消息、policy、工具和执行身份隔离；
- 成功、失败和部分完成结果可汇聚，子 Agent 失败不会丢失其他证据；
- N3 专项 `10 passed`，N1–N3 联合专项 `31 passed`，全量回归 `219 passed`。

详见 [N3 实施记录](N3_HOMOGENEOUS_PARALLEL_FORK.md)。

## N4 已完成

- 根、子、孙使用同一 AgentGraph，孙级 fork 被硬性拒绝；
- 总线程、单 Agent 子线程、工具、时间、token 和重试限制已进入可 checkpoint 图状态；
- 任务指纹与祖先去重防止递归回环；
- 同一 saver 中的独立子线程可取消和恢复，已完成 sibling 不重复；
- N4 专项 `17 passed`，N1–N4 联合专项 `48 passed`，全量回归 `236 passed`。

详见 [N4 实施记录](N4_RECURSION_LIMITS_AND_RECOVERY.md)。

## N5 已完成

- CLI、Web 和评测默认入口已切换到共享 `ResearchRuntime`；
- Web 使用同一 thread 完成说明修改、确认和研究，SSE 支持同进程游标回放；
- 固定离线输入完成 N4 legacy 与 N5 Workflow 对照，新路径增加可定位 evidence 与 WikiLink；
- 旧 Orchestrator、Planner DAG、AgentPool、独立 Summarizer、Evidence Store/Graph、Evolution、旧 Red/Blue 和孤立配置已删除；
- N5 完成后全量回归 `119 passed`。

详见 [N5 实施记录](N5_ENTRY_MIGRATION_AND_LEGACY_CLEANUP.md)。

## N6 已完成

N6 只在最终 Markdown 报告成功持久化后增加默认关闭的单次审查与修订，不改变 Research AgentGraph、fork policy、Memory Store 或线程模型：

- Red 与 Blue 复用同一 policy，各至多调用一次，均无工具、无 fork、无新线程；
- Red 只报告 `factual / logical_consistency / citation_quality` 三类结构化问题；
- Blue 只使用 `ADD / DELETE / MODIFY / VERIFY`；
- 根工作流确定性顺序重放 Blue 动作，并拒绝与完整 Markdown 不一致的未声明修改；
- frontmatter、WikiLink target 与次数、URL 的值与次数以及 manifest 由确定性规则保护；WikiLink alias 可调整；
- 任一失败都保留原始已持久化报告；
- 不实现 RCS、评分引擎、claim-evidence 新模型或新存储。

N6 关键专项与回归为 `65 passed, 1 warning`；N1–N6 全量回归为 `160 passed, 1 warning`，其中 warning 为既有 `StarletteDeprecationWarning`。详见 [N6 实施记录](N6_OPTIONAL_REPORT_REVIEW.md)。

## W0 已完成

W0 在不改变 N1–N6 主链的前提下完成了长期 Memory/Vault 基础：

- 最小 `MemoryDescriptor` 与稳定 `memory_id`；
- `Memories/M-<id>/` 规范路径、重复 ID 和带时区时间校验；
- 平面 frontmatter、完整 WikiLink 和 Windows/Vault 路径安全；
- `research.vault_root` 与旧 `research.memory_root` 读取兼容；
- 既有根目录 `reports/evidence/sources` 的无写入识别；
- W0 专项 `80 passed`，原 N1–N6 回归 `160 passed, 1 warning`，仓库全量 `240 passed, 1 warning`。

详见 [W0 实施记录](W0_MEMORY_VAULT_CONTRACT.md)。

## W1 已完成

- 一个 Vault 可原子创建、列出和选择多个 `Memories/M-<id>/`；
- `Home.md` 与六个必要子目录完整生成，外部标题编辑会在下次读取时生效；
- Workflow/checkpoint/Runtime 显式保持所选 `memory_id`，缺失 Memory 在研究调用前失败；
- 报告、证据、来源按 Memory 隔离并使用完整 Vault 相对 WikiLink；
- 同一 Memory 多次研究保留多份报告，legacy 根目录调用保持不变；
- Chat 只保存 `memory_id` 和 manifest 指针，报告正文继续以 Markdown 为准；
- 并发创建、并发写入和 symlink/junction 逃逸均有确定性测试；
- W1 专项 `19 passed, 1 warning`，原 W0 + N1–N6 回归 `240 passed, 1 warning`，仓库全量 `259 passed, 1 warning`。

详见 [W1 实施记录](W1_MULTI_MEMORY_PERSISTENCE.md)。

## W2 已完成

- 使用标准 `obsidian://open` URI，可按显式 Vault 名称和文件路径打开，也可按绝对路径打开；
- 中文、空格、嵌套路径和 URI 保留字符均按 UTF-8 percent-encoding；
- URI 目标复用 W0 Vault 路径安全检查，不接受路径逃逸、非 Markdown 或 symlink/junction 逃逸；
- Web 增加 Memory 选择器、新建 Memory 和“在 Obsidian 中打开”，新研究携带明确选择的 `memory_id`；
- CLI 可用 `--memory-id` 选择已有 Memory，并在完成后输出 Vault、Home、URI 和报告路径；
- 每次列出 Memory 都重新读取 `Home.md`，因此 Obsidian 外部标题编辑会在下一次请求中生效；
- 不检测或启动 Obsidian，不写 `.obsidian/`，不实现插件、阅读器或笔记写入；
- W2 专项 `34 passed, 1 warning`，原 N1–N6 + W0–W1 回归 `259 passed, 1 warning`，仓库全量 `293 passed, 1 warning`。

详见 [W2 实施记录](W2_OBSIDIAN_MINIMAL_INTEGRATION.md)。

## W3 已完成

- 当前 Memory 的 Markdown 在每次搜索时重建进程内索引，不写缓存或第二套知识库；
- 索引读取 frontmatter、文件名/H1、全文、WikiLink 出边/反向引用、mtime 和内容哈希；
- 中英文关键词按标题、路径、frontmatter、正文和一跳链接确定性排序，结果最多 5 项且摘要有硬上限；
- 其他 Memory、无关文件与 symlink/junction 逃逸不会进入上下文；
- Research Brief 固定展示目标 `memory_id`、命中文件、已知信息和新研究空白，模型不能改写检索确定的路径；
- 同一检索快照贯穿 Brief 修改与确认，确认前仍不调用研究工具；
- 根 Research Agent 只收到有界摘要、已知信息和空白，不接收 `memory_id`，AgentGraph、fork、identity 与 tracing 保持不变；
- 新报告继续通过 W1 路由写回同一 Memory，不覆盖旧报告；
- W3 专项 `22 passed`，原 N1–N6 + W0–W2 回归 `293 passed, 1 warning`，仓库全量 `315 passed, 1 warning`。

详见 [W3 实施记录](W3_CONTINUE_RESEARCH_FROM_MEMORY.md)。

## W4 已完成

- 问答只检索用户明确选择的当前 Memory，其他 Memory 不进入上下文；
- 无命中时明确返回证据不足且不调用 policy，有命中时回答只采用实际命中路径并由 PaperPilot 附加完整 WikiLink；
- 普通问答不发起 Research Workflow、不使用工具、不写文件或 Chat Store；
- 保存回答先生成带稳定 note ID、固定路径、完整 frontmatter 和受限来源的 Markdown 提案，未确认时 Vault 不变；
- 用户确认后只新建对应 `notes/` 笔记并受控更新同一 Memory 的 `Home.md` Notes 链接；
- Home 内容哈希、目标不存在约束以及在原子替换点保留并复核旧 Home，阻止 Obsidian 外部编辑、重复确认与并发提交被静默覆盖；
- 写回失败会回滚本次笔记和受控临时文件；恢复期间持续外部写入时返回冲突并保留最新捕获版本，不删除未知内容；非法路径、frontmatter、跨 Memory WikiLink 与 symlink/junction 逃逸不会写入；
- Web 明确区分“基于此 Memory 研究”和“Memory 问答”，回答使用白名单 Markdown 安全渲染，引用可在 Obsidian 中打开，保存前展示完整 Markdown 并要求确认；
- W4 专项 `35 passed, 1 warning`，warning 为既有 `StarletteDeprecationWarning`；
- 原 N1–N6 + W0–W3 前序回归 `315 passed, 1 warning`，包含 W4 的仓库全量回归 `350 passed, 1 warning`。

详见 [W4 实施记录](W4_MEMORY_QA_CONTROLLED_NOTES.md)。

## W5 已完成

- Web 支持明确选择的 PDF/UTF-8 文本文件、直接粘贴文本和显式 HTTP/HTTPS URL，未点击生成预览时不读取 URL；
- 原始内容按 SHA-256 写入 `attachments/`，带页/行/网页段 locator 的结构化提取写入 `imports/`，受控 policy 只提议一篇支持/冲突/空白整理笔记；
- `source_ref + locator + content_hash` 精确三元组在准备与提交内双重去重，同内容不同来源可安全共享 content-addressed attachment；
- 未确认提案不写 Vault，确认后在单 PaperPilot 进程的共享 Vault `RLock` 内成组发布 attachment/import/note，以内容哈希复核后的 `Home.md` 原子替换为线性化点；
- 失败会按文件身份回滚本次未线性化文件，已被其他完成 Import 引用的共享 attachment 不会被失败事务回滚删除；
- attachment 规范路径和保留扩展名的 WikiLink、frontmatter、跨 Memory/路径逃逸、symlink/junction、恶意 Markdown/HTML 以及 URL SSRF/DNS/重定向/大小/超时均有确定性保护；
- W5 专项 `61 passed, 1 warning`，原 N1–N6 + W0–W4 前序回归 `350 passed, 1 warning`，包含 W5 的仓库全量回归 `411 passed, 1 warning`，warning 为既有 `StarletteDeprecationWarning`。

W5 没有实现 CLI 导入收口、legacy 迁移、文件树、内置阅读器、文件系统监控、Repository、持久化索引或新 Agent。W5 不引入 journal、bundle 或 cross-process lock，不保证多 PaperPilot 进程同时写同一 Vault；W6 也不因此自动承诺扩展该范围。

详见 [W5 实施记录](W5_CONTROLLED_IMPORTS.md)。

## W6 已完成

- CLI/Web 的真实写入入口统一通过 `ResearchRuntime` 校验显式 managed Memory；单次 CLI、REPL 和 Web 不再把缺省选择写入 legacy 根目录；
- Web session 只在 `session_meta` 增加 nullable `memory_id`，首次显式绑定后不可切换；历史报告不再被用来推断会话 Memory；
- 根目录 `reports/evidence/sources` 作为虚拟 `M-legacy` 安全扫描和问答，研究、笔记、导入与 managed 写入均拒绝；
- CLI/Web 的 legacy 迁移先显示 Home 和所有转换 Markdown 的完整零写 preview，再在二次源快照和完整 staging 校验后用一次目录 rename 发布；根文件、旧 manifest 和当前 session 绑定保持不变；
- Langfuse 补齐 `memory_id`、检索文件/分数和写入路径/状态，且不记录问题、正文或附件字节；
- 固定离线评测真实覆盖检索命中、引用完整、无依据拒答、受控写入和继续研究，`5/5 passed`；
- W6 专项 `39 passed, 1 warning`，原 N1–N6 回归 `160 passed, 1 warning`，N1–N6 + W0–W5 前序集合 `411 passed, 1 warning`，仓库全量 `450 passed, 1 warning`；pytest warning 为既有 `StarletteDeprecationWarning`；
- 没有新增领域模型、Service、Repository、持久索引、Agent、第二套存储、内置阅读器、cross-process lock、journal 或 bundle。

详见 [W6 实施记录](W6_STABILIZATION_MIGRATION_AND_ENTRY.md)。W0–W6 既定主线至此完成，没有开始计划外阶段。

## S0 已完成，S1–S5 尚未开始

- S0 已移除无限制 FileReader，以每次运行的虚拟根授权当前 managed Memory/受控上传；真实路径、文件身份、link/reparse/TOCTOU、类型、大小、读取和解码边界均已验收；
- S1 使用 `AsyncSqliteSaver`，研究、笔记、导入和迁移确认进入 LangGraph State/interrupt；Runtime Registry 不复制工作流正文或状态；
- S2 让所有产品 Vault 写入进入持久化队列，由单一 Writer 使用 staging、journal、哈希和幂等键发布；
- S3 在安全迁移、历史路径映射和外部可恢复归档完成后，从活动 Vault 退役 legacy 根目录；
- S4 从 Markdown 增量构建可删除重建的 SQLite FTS5；
- S5 在严格 Memory 范围内增加可选多语言 embedding 与混合排序。

执行与验收的唯一边界见 [S 系列生产化与检索升级实施计划](S_PRODUCTION_HARDENING_AND_RETRIEVAL_PLAN.md)。S0 结果见 [S0 实施记录](S0_FILE_READER_SANDBOX.md)；当前没有开始 S1。

## 历史基础

以下记录描述旧架构演进，只用于了解已经解决过的问题，不再定义目标设计：

- [阶段 0 基线](STAGE_0_BASELINE.md)
- [阶段 0.5 Langfuse](STAGE_0_5_LANGFUSE.md)
- [旧阶段 1 执行正确性](STAGE_1_EXECUTION_CORRECTNESS.md)
- [旧 Phase 1 LangGraph 最小基础](PHASE_1_LANGGRAPH_FOUNDATION.md)

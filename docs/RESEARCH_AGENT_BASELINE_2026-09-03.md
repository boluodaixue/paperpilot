# Research Agent 底座设计与状态（2026-09-03）

## 冻结点

- 架构提交：`9fa07c1`（`feat: freeze dynamic root-agent research baseline`）
- 分支：`codex/supervisor-worker-v2`
- 目标：冻结当前可用底座；后续只先修复无效 Fork 的预算预检，不重构其他模块。

## 当前生产设计

1. Dynamic Planner 根据 Research Brief 现场生成约 4–5 个研究方向，不读取固定问题计划。
2. Root 把外部证据方向交给同质 Child；Child 可以在预算允许时继续 Fork Grandchild。
3. Blackboard 只记录 Plan、Assignment、Agent 状态、Coverage gap、Query/Source fingerprint、队列和等待事件，用于方向协调、查漏补缺和避免重复研究；动态生产运行不把 Evidence 写入 Blackboard。
4. Child 自主搜索、判断材料价值，并返回只覆盖自己方向的简短 `research_memo`，使用真实 `[[EVIDENCE:id]]` 标记。
5. Root 汇总 Child memo、识别缺漏并直接生成最终 Markdown 报告。动态生产路径不经过 Evidence Selector、Claim inventory、Semantic Verifier、独立 Composer 或语义 Citation Audit。
6. 运行时只对已知 Evidence 标记做确定性引用渲染；唯一匹配已知 `evidence-` ID 的裸哈希先确定性补全，仍未知的标记被移除但正文保留，不伪造引用。
7. 固定计划与 Supervisor 路径保留为实验/历史对照，不接入当前动态生产链路。

## LLM Wiki / Obsidian 接入边界

- 产品默认配置复用本基线的动态 Root/Child、结构化证据获取、700k 总预算和全局可退还租约；ResearchBench 配置及已记录分数不变。
- LLM Wiki 仍位于研究图外围：选择 Memory 时在 Brief 前做有界检索，Root 完成后才通过 Single Vault Writer 持久化报告、Evidence 和 Source。
- managed Memory 使用确定性链路 `报告 → Evidence → Source`；Obsidian backlinks 提供反向关系。LLM 不生成路径或重写链接身份。
- Obsidian 直接打开 Markdown Vault；Markdown 继续是唯一知识真相源，外部编辑会被重扫，冲突不会被静默覆盖。
- 基于知识库回答走只读 `/api/memories/{memory_id}/answers`；基于知识库继续研究仍走 `/api/alignment`，传入已绑定的 `memory_id` 后提取 known information 和 research gaps。
- 红蓝对抗、Supervisor V2、后置 report review 和语义 Citation Audit 保持关闭。

实际入口与开关：

| 能力 | 入口 | 当前开关/配置 |
| --- | --- | --- |
| 当前动态研究核心 | `build_research_runtime` → `build_research_workflow` | `structured_report.enabled=true`、`homogeneous_fork.enabled=true`、`budget_leases_enabled=true` |
| Wiki 持久化 | `persist_result` → `VaultWriteService.persist_research` | `research.vault_root=memory` |
| Obsidian | Web Memory 选择区“在 Obsidian 中打开” | 可选 `research.vault_name`；未配置时使用绝对路径 URI |
| Memory 检索 | `MarkdownMemoryIndex` | `runtime.retrieval_db_path=data/retrieval.db`；语义召回默认关闭，FTS5/WikiLink 可用 |
| Memory 问答 | `/api/memories/{memory_id}/answers` | 只读、限定 selected Memory、必须返回可验证 WikiLink 引用 |
| 继续研究 | `/api/alignment` + `memory_id` | 检索结果有界进入 Brief，研究核心仍只把新 Evidence 当外部证据 |

接入兼容修复：managed Memory 的参考文献 WikiLink 使用合法别名 `Evidence n`；正文显示的 `[n]` 不变。此前 `[n]` 被直接放入 WikiLink alias，会被 W0 安全契约拒绝并阻断持久化。

### 产品链路加固

- managed research bundle 现在确定性更新 `Home.md` 的 Reports 列表、frontmatter 时间和正文 Last updated；同一 Memory 的并发研究在持久 Writer 租约内顺序规划，避免丢失报告链接。
- 已存在的 Evidence/Source Markdown 视为不可变知识节点；后续研究复用其精确字节，不覆盖此前的 Obsidian 编辑，并继续检测规划后的并发修改。
- Memory、导入内容在进入 LLM 时明确作为不可信参考数据，不再放入 system role；路径和引用仍由运行时确定性校验。
- Web 报告、Evidence 和 Memory 回答统一经过安全 Markdown 渲染；Vault frontmatter 只保留在文件中，不进入报告正文界面。
- Web 明确展示完整/部分完成、termination reason 和 stop reason；Memory 回答可以“完成，不保存”，证据不足时可以一键切换到继续研究。
- 产品 checkpoint 使用显式 PaperPilot 类型 allowlist，消除未来 LangGraph 严格反序列化模式的兼容告警。
- 页面增加新手空状态、内嵌 Memory 创建、键盘焦点、语义化报告页签与移动端布局；不改变研究 AgentGraph、预算或评测配置。

## 当前模型与容量参数

- Research Agent 温度：`0.3`
- Judge 温度：`0.1`
- Child 单次输出：`4096 tokens`
- Root 输入上限：`60000 chars`
- Root 单次最终输出：`32768 tokens`
- Root 累计最终输出预算：`50000 tokens`
- 全局研究预算：`700000 tokens`
- 可退还全局 Child 租约：启用；初始 `60000`、单次补充 `25000`、单 Child 上限 `125000 tokens`，并保护 Root 合并与最终输出预算。
- Root 最终格式：直接 Markdown，不再把全文放入 JSON。
- 模型适配器保留 API `finish_reason`；若 Root 因 `length` 停止，则从报告尾部继续生成并拼接，直到自然结束或累计最终输出预算耗尽。
- 真实运行必须完整加载 `D:\Claude\deepresearch-agent\.env`（`override=True`），研究与 Judge 均使用其中配置的火山方舟 DeepSeek v4 Flash；不要使用系统环境中的 DeepSeek 配置。

## 当前有效结果

### Root 输出修复的同 checkpoint 对照

来源研究状态：`outputs/evaluation/researchbench-root-agent-tech-two-r6-temp03/checkpoint.sqlite`。该对照只重放 Root，没有重新规划、搜索或运行 Child。

- tech_001：同一批 4 个 Child、328 条 Evidence；旧 JSON/4k Root 报告 Judge `3.4`，直接 Markdown Root 重放后 Judge `8.2`；报告 9,505 字符、16 个引用来源、`output_status=valid`、0 次续写。
- tech_002：同一批 5 个 Child、201 条 Evidence；旧 JSON/4k Root 报告 Judge `6.0`，直接 Markdown Root 重放后 Judge `6.2`；报告 10,560 字符、13 个引用来源、`output_status=valid`、0 次续写。
- 平均 Judge：`7.2`。
- 结果：`outputs/evaluation/researchbench-root-agent-tech-two-r7-root-replay/ResearchBench_RootReplay_60K_DirectMarkdown_20260903_035535.json`

### 历史参照边界

- r4 动态 Root 直写科技题：tech_001 `7.6`、tech_002 `6.8`，平均 `7.2`。
- 财经题 `8.8` 使用不同题目，不作为这两道科技题的分数基线。
- 新建 checkpoint 后得到的 tech_001 `5.0` 属于重新规划与重新搜索的独立轨迹，不能用于判断 55k/60k Root 输入修改本身。

## 测试状态

- Fork、租约、引用与架构相关回归：119 条通过。
- Wiki、Obsidian、检索问答与继续研究专项：316 条通过；新增 Home、并发 Writer、Web 安全与 checkpoint allowlist 回归均通过。
- Memory Wiki 固定离线评测：5/5 通过，覆盖检索隔离、引用完整、证据不足拒答、受控写入和继续研究。
- 全量：1,113 passed、3 skipped。
- `git diff --check` 通过，仅有 Windows CRLF 提示。

## 无效 Fork 修复状态

- 派发前同时预检时间、token、工具与 Agent 容量；不足时不注册 Assignment、不启动 Agent，任务保留给当前 Agent。
- Controller 获得 `fundable_child_count`、最低 Child token 和可委派工具额度；容量为 0 时直接本地研究，不再消耗一次模型 Fork 判断。
- 资源拒绝指纹只用于精确去重；剩余 Child 名额按真实 `child_results` 计算，拒绝项不占逻辑名额。
- 未启用租约时使用完整研究循环的内部 token 下限；启用租约时使用配置的初始租约下限。
- Child 与 Root 提示词要求原样复制完整 Evidence ID；运行时仅对唯一对应已知 ID 的裸哈希确定性补全。

这些修改不增加领域规则、Evidence Selector、Verifier、Composer 或语义 Citation Audit，也不改变冻结的 Root/Child/Blackboard 职责边界。

## 500k 修复诊断（非 700k 新基线）

- 在 500k、租约关闭的诊断运行中，tech_001 两次 Judge 为 `5.8`、`6.2`，tech_002 为 `8.2`；三次均无零产出 Agent。
- 修改前 r6 的 tech_001 曾创建 5 个 Grandchild，但全部为 0 查询、0 工具、0 Evidence；修改前 tech_002 本来就没有 Grandchild。
- 诊断暴露出拒绝循环、逻辑名额计数和裸 Evidence ID 三个确定性问题，现已按上述最小范围修复。

## 当前在线质量基线：700k＋全局租约

该版本确认为当前质量基线。三轮均使用全新 checkpoint、串行执行并完成 Judge：

| 运行 | Judge | Child / Grandchild | 零产出 Agent | Evidence / 来源 | 最终引用来源 | 停止原因 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| tech_001 A | 7.2 | 4 / 4 | 0 | 323 / 43 | 5 | token budget exhausted |
| tech_002 | 7.0 | 5 / 3 | 0 | 329 / 42 | 14 | token budget exhausted |
| tech_001 B | 7.4 | 4 / 0 | 0 | 216 / 29 | 12 | time budget exhausted |

- tech_001 A 与 tech_002 的 7 个 Grandchild 均实际执行查询、产出 Evidence 和 memo；无效 Grandchild 问题已修复。
- 三轮租约均完整释放，结束时活动租约为 0；资源预检拒绝各为 `1 / 1 / 0`，未再形成拒绝循环。
- tech_001 B 的 29 个原始引用标记全部有效；相比旧 B 轮引用来源为 0，引用 ID 修复有效。A 轮另有 23 个裸 ID 被确定性恢复。
- tech_001 A/B Judge 为 `7.2 / 7.4`，相较旧诊断的 `5.8 / 6.2` 更高且差距更小。
- 三轮 Root 最终合并输入分别约为 `45k / 42k / 32k chars`，均未触及 `60k chars` 上限，因此本基线不提高该上限。
- B 轮没有 Grandchild 是因为初始规划请求出现约 10 分钟服务长尾，最终耗尽时间预算；当时仍有约 320k token 可申请，因此不是租约或总 token 不足。
- A 与 tech_002 仍为 `budget_forced`，说明 700k 提升了有效深度和覆盖，但完整研究循环的预算收口仍会提前停止；这不等同于报告无效。

## Root 最终合成时间

- `max_elapsed_seconds: 1200` 完整保留给研究阶段，不再通过提高合成保留比例缩短研究。
- Root 在研究截止后独占额外 `root_finalization_grace_seconds: 300`，只允许最终 Markdown 合成、续写与结构修复。
- Child、Grandchild、工具调用和 Fork 不得消费这 300 秒；未显式配置该字段的调用保持旧时间行为。


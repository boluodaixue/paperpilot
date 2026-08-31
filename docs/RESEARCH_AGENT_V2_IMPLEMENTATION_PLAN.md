# PaperPilot Research Agent V2 完整实施计划

## 1. 实施原则

本计划实现 `RESEARCH_AGENT_V2_DESIGN.md`，采用渐进迁移，不进行一次性重写。

优先级固定为：

1. 复用 PaperPilot 当前已经测试过的基础设施；
2. 直接复用或小幅移植参考仓库中边界清晰、许可证兼容的代码；
3. 适配参考设计到现有数据模型；
4. 只有在前三者都不适用时才新增实现。

实施期间必须保留 Legacy Graph，通过配置选择 V1/V2。每个阶段单独通过测试门后才能进入下一阶段。

## 2. 固定参考版本与许可证

### LangChain Deep Research From Scratch

- 仓库：https://github.com/langchain-ai/deep_research_from_scratch
- 固定提交：`93f35e5d2a51590f9542207a9ff66a01901da5bc`
- 许可证：MIT
- 主要参考文件：
  - `src/deep_research_from_scratch/state_multi_agent_supervisor.py`
  - `src/deep_research_from_scratch/multi_agent_supervisor.py`
  - `src/deep_research_from_scratch/research_agent.py`
  - `src/deep_research_from_scratch/research_agent_full.py`
  - `src/deep_research_from_scratch/research_agent_scope.py`
  - `src/deep_research_from_scratch/prompts.py`

### GPT Researcher

- 仓库：https://github.com/assafelovic/gpt-researcher
- 固定提交：`6f998577d547b1e54ec662dac63583aa11e3b84b`
- 许可证：Apache-2.0
- 主要参考文件：
  - `gpt_researcher/actions/query_processing.py`
  - `gpt_researcher/skills/curator.py`
  - `gpt_researcher/skills/writer.py`
  - `gpt_researcher/actions/web_scraping.py`
  - `multi_agents/agents/orchestrator.py`
  - `multi_agents/agents/fact_checker.py`
  - `multi_agents/agents/fact_review.py`
  - `multi_agents/memory/research.py`

如果直接复制非平凡代码，必须保留文件级来源注释，并在仓库新增或更新 `THIRD_PARTY_NOTICES.md`，包含原许可证、文件来源和固定提交。仅借鉴架构或重新实现接口时，也要在实现注释中标明设计来源。不得把整个参考仓库加入运行依赖。

## 3. 复用决策矩阵

| 能力 | PaperPilot 当前实现 | LangChain 参考 | GPT Researcher 参考 | 决策 |
|---|---|---|---|---|
| Brief 确认与恢复 | `workflow.py` 已完整 | scoping graph 较简单 | 无等价恢复 | 直接保留 PaperPilot |
| Supervisor 状态图 | 当前无独立 Supervisor | `SupervisorState`、`ConductResearch`、`ResearchComplete`、并行 gather | ChiefEditor 图 | 移植 LangChain 图骨架，接入当前 checkpoint |
| Worker 工具循环 | `agent_graph.py` 完整且带预算、Evidence、告警 | 简单 LLM/tool/compress 循环 | ResearchConductor 较重 | 从 PaperPilot 抽取；只参考 LangChain 节点边界 |
| 子问题生成 | `directions` 直接切分，过粗 | Lead prompt 动态派工 | `generate_sub_queries` 和 `_normalize_sub_queries` | 移植 GPT 的防御性规范化，使用当前 policy/parser |
| 来源搜索/抓取 | 已有 Tavily/秘塔/Exa/Bocha、Browser、论文工具和熔断 | Tavily 单工具 | 大量 retriever/scraper | 不复制；保留 PaperPilot |
| 来源质量筛选 | `evidence_selection.py` 有确定性选择 | prompt 规则 | `SourceCurator` 有失败回退 | 扩展现有选择器，借鉴 curator 的排名维度和回退 |
| 研究证据 | `EvidenceItem`、artifact、locator 已完整 | 压缩文本 notes | context strings | 保留并扩展为 Claim↔Evidence 多映射 |
| Red/Blue | `report_review.py` 已有严格 edit replay 和写入保护 | 无 | reviewer/reviser/fact checker 有界循环 | 复用本地校验器，借鉴 GPT 的有界路由，把 Red 前移 |
| 报告生成 | 当前 final JSON + renderer，结构不足 | 独立 final report node | `ReportGenerator`、无上下文拒写 | 移植独立写作节点结构，保留本地 Evidence/Vault 输出 |
| 引用检查 | 当前报告后置 review 不允许改变引用 | prompt 要求引用 | fact-check loop | 新增 Evidence ID audit；复用本地 URL/WikiLink 校验 |
| 持久化 | 单一 Vault Writer、journal、幂等 | 无 | 输出目录写入 | 完全保留 PaperPilot |
| 评测 | ResearchBench、RCS、Judge | Deep Research Bench 接口思路 | evals | 保留并修正正文覆盖评测 |

## 4. 目标模块结构

```text
src/research/
├── v2_contracts.py          # ResearchPlan、WorkPacket、EvidenceClaim、Challenge、CitationIssue
├── research_planner.py      # Brief → 轻量计划；子查询规范化
├── research_worker.py       # 从现有 AgentGraph 抽取的一层无 fork Worker Graph
├── research_supervisor.py   # ConductResearch / ResearchComplete / 波次和预算
├── research_challenge.py    # Red review、Lead adjudication、补充任务生成
├── report_composer.py       # Lead 生成带 Evidence ID 的 Markdown 草稿
├── citation_audit.py        # 确定性检查、语义检查、受约束修复
├── research_v2_graph.py     # V2 总图组装和路由
└── ...                      # 现有模块继续保留
```

如果实施中发现模块过细，可以合并 `research_challenge.py` 与 `citation_audit.py` 的共享解析/patch 逻辑，但不得把 Planner、Worker、Supervisor 和最终写作重新混入同一个循环。

## 5. 分阶段实施

### Phase 0：基线、归因与安全开关

#### 工作

1. 记录当前完整测试基线和单题 canary 基线。
2. 新增 `THIRD_PARTY_NOTICES.md`，记录两个固定参考提交与许可证。
3. 在 `configs/default.yaml` 增加：

```yaml
research:
  architecture: legacy
  supervisor_v2:
    enabled: false
    max_initial_workers: 4
    max_research_waves: 2
    red_review_enabled: true
    max_red_review_rounds: 1
    max_citation_repair_rounds: 1
```

4. `runtime.py` 严格解析配置；未知架构或非法值直接报错。
5. 不改变默认运行行为。

#### 文件

- 修改：`configs/default.yaml`、`src/research/runtime.py`、`src/research/models.py` 或新 `v2_contracts.py`
- 新增：`THIRD_PARTY_NOTICES.md`、`tests/test_v2_config.py`

#### 验收门

- V2 默认关闭；现有完整测试结果不回退；
- 配置错误可预测失败；
- 仓库中没有复制但未归因的参考代码。

### Phase 1：V2 数据契约与轻量 Planner

#### 工作

1. 在 `v2_contracts.py` 定义可序列化 frozen dataclass/Enum：
   - `CoreQuestion`
   - `ResearchPlan`
   - `WorkPacket`
   - `EvidenceClaim`
   - `ResearchChallenge`
   - `ChallengeDecision`
   - `CitationIssue`
   - `CitationAuditOutcome`
2. 为所有 ID 使用稳定内容哈希；禁止依赖 Python 随机 hash。
3. 实现 `ResearchBrief → ResearchPlan` 的结构化 policy 调用。
4. 从 GPT Researcher `query_processing.py` 移植 `_normalize_sub_queries` 的防御性输入归一化思想：支持数组、`queries` 字典、单 query、字符串和无效输出，并保留原问题作为 fallback。
5. 不建立严格矩阵；Core Questions 主要来自用户确认的 `directions`，模型只能补充真正必要的问题并标注来源。
6. 一次结构修复失败后使用确定性 Brief fallback。

#### 复用

- PaperPilot：`ResearchBrief`、`parse_json_object`、结构修复模式、稳定 ID 方式；
- LangChain：`ResearchQuestion`/structured output 的边界；
- GPT Researcher：sub-query normalization 和错误回退模式。

#### 文件与测试

- 新增：`src/research/v2_contracts.py`、`src/research/research_planner.py`
- 修改：`src/research/__init__.py`
- 新增：`tests/test_v2_contracts.py`、`tests/test_v2_planner.py`

#### 验收门

- 相同 Brief 重跑生成稳定 ID；
- malformed JSON、字典、字符串、空数组都有保守结果；
- Planner 不调用研究工具；
- 每个 required direction 至少映射一个 Core Question，或明确记录 fallback reason。

### Phase 2：抽取一层 Blue Research Worker

#### 工作

1. 从 `agent_graph.py` 抽取可复用 Worker 节点：policy/tool loop、工具调用、artifact 保存、Evidence 提取、tool availability、预算和 context compaction。
2. Worker 输入从整个 Root Task 改为 `WorkPacket + ResearchPlan projection`。
3. Worker tool schema 中不注册 `fork_research`；Worker State 不包含 pending child/fork 字段。
4. Worker 输出改为结构化：`EvidenceClaim[] + EvidenceItem[] + unresolved[] + alerts[] + usage`。
5. 搜索结果默认只是 lead；只有包含可定位来源并符合 action scope 的内容才能形成 Evidence。
6. 完整 raw artifact 仍先持久化，再进入 Working Context 压缩。
7. Legacy `agent_graph.py` 暂时不删除；优先提取共享 helper，避免复制两份工具逻辑。

#### 复用

- PaperPilot：绝大部分实现；
- LangChain `research_agent.py`：仅复用 `llm_call → tool_node → compress/output` 的图边界和 output schema 思路，不使用其 Tavily-only 工具和字符串 notes；
- GPT Researcher：不复制 scraper/retriever。

#### 文件与测试

- 新增：`src/research/research_worker.py`
- 修改：`src/research/agent_graph.py`，必要时新增 `src/research/tool_execution.py`、`src/research/evidence_extraction.py` 作为共享模块
- 新增：`tests/test_v2_worker.py`、`tests/test_v2_worker_recovery.py`
- 扩展：`tests/test_tool_availability.py`、`tests/test_context_compaction.py`

#### 验收门

- Worker 无法 fork；
- Worker 的 tool/token/time/retry 预算均生效；
- 服务不可用告警不丢失；
- Evidence/Artifact 在压缩和恢复后仍可定位；
- 并行 Worker 之间 policy 和 tool 实例隔离。

### Phase 3：Supervisor 与并行研究波次

#### 工作

1. 按 LangChain `state_multi_agent_supervisor.py` 移植 `ConductResearch` 和 `ResearchComplete` 的结构化控制语义，替换为 PaperPilot `WorkPacket`。
2. 按 `multi_agent_supervisor.py` 的两节点模式实现：
   - `supervisor_decide`
   - `supervisor_execute`
3. 同一控制回合的多个独立 WorkPackets 使用 `asyncio.gather` 并行执行。
4. Supervisor 必须保存：已派工 question IDs、packet fingerprint、wave、结果、失败、预算和未解决问题。
5. 禁止重复 packet；第一轮至少优先给尚未分配的 required Core Questions。
6. Supervisor 可以再次派发第二波，但不能超过 `max_research_waves=2`。
7. 工具、线程和 token 预算按“Lead 保留 + Worker 公平份额”分配，写作和引用预算不可委派。
8. `ResearchComplete` 必须通过确定性校验：所有 required questions 已有发现、已明确 unresolved，或资源边界已触发。

#### 复用

- LangChain：Supervisor/Tools 节点拆分、结构化控制工具、并行 gather；
- PaperPilot：全树预算分配、fingerprint、checkpoint、结果汇聚、告警传播；
- GPT Researcher：研究问题分组和失败 fallback 思路。

#### 文件与测试

- 新增：`src/research/research_supervisor.py`、`src/research/research_v2_graph.py`
- 修改：`src/research/runtime.py`、`src/research/workflow.py`
- 新增：`tests/test_v2_supervisor.py`、`tests/test_v2_parallel_workers.py`、`tests/test_v2_budget_reserve.py`

#### 验收门

- 线程深度始终为 1；
- required Core Questions 不会在未分配时被同一问题的重复 packet 挤占；
- 并行结果顺序不影响确定性合并；
- checkpoint 重入不重复 Worker；
- Supervisor 不能伪造 budget/user cancellation 终止原因。

### Phase 4：研究前 Red Challenge 与定向补充

#### 工作

1. 从现有 `report_review.py` 抽取 JSON 解析、严格 schema、受约束 issue 处理和 fallback 模式。
2. 新增 Red Research Reviewer prompt 和 parser，只允许设计文档中的六类 Challenge。
3. Red 输入仅包含 Research Plan、Evidence Claims、来源元数据、限制和 unresolved，不包含隐藏推理。
4. Lead 对挑战进行结构化裁决；拒绝挑战必须给出已有 Evidence ID 或明确理由。
5. 被接受的 high-severity challenge 转成 supplemental WorkPacket；只允许一轮补充。
6. 补充后只复查原 high-severity challenge 的状态，不启动第二次全量 Red。
7. Red 不可用时生成 checkpointed quality alert，并回退到 Supervisor gap check。

#### 复用

- PaperPilot `report_review.py`：严格 JSON、category allowlist、fallback 和可恢复执行；
- GPT Researcher `orchestrator.py`、`fact_review.py`：有界 reviewer/revision 路由；
- 不复制 GPT Researcher 基于字符串 `None` 的接受协议，统一使用结构化 schema。

#### 文件与测试

- 新增：`src/research/research_challenge.py`
- 重构：`src/research/report_review.py` 的共享 helper，保持 Legacy 测试兼容
- 新增：`tests/test_v2_research_challenge.py`、`tests/test_v2_supplemental_wave.py`

#### 验收门

- Red 无工具权限；
- 非法类别、未知 claim/evidence/question ID 被拒绝；
- 挑战最多触发一次定向补充；
- checkpoint 重入不重复 Red 或补充 Worker；
- Red 失败不会静默消失。

### Phase 5：Lead Draft、Citation Audit 与修复

#### 工作

1. 按 LangChain `research_agent_full.py` 的独立 final report node 建立 `compose_report`，但由 Lead 角色执行，不新增 Writer Agent。
2. 借鉴 GPT Researcher `ReportGenerator.write_report` 的无上下文拒写：没有有效 Evidence 时返回明确 partial/abstain，而不是生成报告。
3. Draft 输入使用经过选择的 Evidence Claims，并保留 Evidence ID；不得把全部 raw logs 塞入 prompt。
4. 定义内部引用标记格式，例如 `[[EVIDENCE:E-...]]`，在正式写入时确定性解析为 Vault WikiLink/引用。
5. 新增 Citation Audit：
   - 确定性检查 ID、来源、locator、URL/WikiLink inventory；
   - 一次语义检查陈述是否被 Evidence 支持；
   - 输出受约束 edit operations；
   - 重放 edit operations 并验证结果。
6. 复用现有 `validate_revised_report` 的 frontmatter、URL、WikiLink 和 manifest 校验思想，但允许引用变更仅指向当前 Evidence inventory。
7. Citation Repair 只允许补现有证据、换证据、缩小/限定/删除陈述。关键证据缺失且尚未使用补充额度时，允许一次 targeted Worker，并只重写受影响段落。
8. 最终 Markdown 生成稳定引用顺序；参考 GPT Researcher `test_add_references_order.py` 增加等价回归。

#### 文件与测试

- 新增：`src/research/report_composer.py`、`src/research/citation_audit.py`
- 修改：`src/research/rendering.py`、`src/research/report_review.py`
- 新增：`tests/test_v2_report_composer.py`、`tests/test_v2_citation_audit.py`、`tests/test_v2_citation_repair.py`

#### 验收门

- 0 个未知或悬空 Evidence ID；
- Writer 无研究工具；
- 无证据时拒写；
- Citation Repair 不能新增未知 URL 或 Evidence；
- 不受支持的陈述被修正、删除或明确降级；
- 引用顺序可重复、恢复后不变化。

### Phase 6：Workflow、持久化、恢复与产品集成

#### 工作

1. 在 `workflow.py` 中按配置选择 Legacy 或 V2 Research Graph。
2. V2 路径调整为：

```text
draft_brief → review_brief → prepare_research
→ research_v2_graph
→ citation-approved report
→ persist_result
→ postprocess/terminal
```

3. 正式报告只在 Citation Audit 通过或明确 partial 降级后由 Single Vault Writer 持久化；不再先持久化未审草稿再替换。
4. Evidence/Source/Artifact 仍可在研究过程中按现有幂等写入意图持久化。
5. 为每个阶段定义 checkpoint 恢复边界和去重键。
6. SSE/API/UI 增加阶段事件：planning、blue research、red review、supplemental、drafting、citation audit、persisting。
7. 运行结果披露 challenges、citation issues、补充波次和 fallback，但不泄漏隐藏推理。
8. Legacy `report_review_enabled` 在 V2 中映射为新的 Red/Citation 开关或明确拒绝冲突配置。

#### 文件与测试

- 修改：`src/research/workflow.py`、`src/research/runtime.py`、`src/research/workflow_recovery.py`、`src/research/models.py`
- 修改：`web/server.py`、`web/static/index.html`、CLI/脚本中的结果投影
- 新增：`tests/test_v2_workflow.py`、`tests/test_v2_workflow_recovery.py`、`tests/test_v2_web_progress.py`、`tests/test_v2_persistence.py`

#### 验收门

- 每个阶段都能中断恢复且不重复外部调用/写入；
- 报告、Evidence 和 Source 仍遵守 Memory 隔离与单 Writer 契约；
- Legacy/V2 可配置切换；
- UI 能区分研究失败、Red 不可用、Citation 未通过和预算终止。

### Phase 7：评测修正、canary、灰度与 Legacy 退役决策

#### 工作

1. 修改 ResearchBench 覆盖评测，只评最终报告正文，不让 Research Brief 关键词计入成果覆盖。
2. 增加 V2 结构指标：
   - Core Question 分配率；
   - Worker 重复率；
   - source-open 比率；
   - Challenge 接受/解决率；
   - material claim citation coverage；
   - invalid citation count；
   - finalization reserve 使用情况。
3. 先运行离线/固定 policy 回归，再运行 `tech_001` 单题 canary。
4. 单题通过后运行三题小样本；未通过则只修复失败层，不扩大预算或题量。
5. V2 稳定后把默认配置从 `legacy` 切换为 `supervisor_v2`；Legacy 至少保留一个发布周期。
6. 只有完成兼容与迁移审计后，才删除 recursive fork/grandchild 专用代码和测试。

#### 最终验收门

- 全部现有回归测试通过；
- V2 新测试全部通过；
- 单题输出 `output_status=valid`，不得使用结构 fallback；
- required Core Question 分配率 100%，未回答项明确 unresolved；
- material claim citation coverage ≥ 80%；
- invalid citation count = 0；
- Judge 平均分 ≥ 5/10；
- 不因 Workers 用尽写作预留预算而 `budget_forced`；
- 外部工具不可用在发生时立即告警；
- 三题扩展只在单题通过后启动。

## 6. 实施顺序与提交边界

建议每个 Phase 独立提交，提交前运行该阶段测试和受影响的既有测试：

1. `v2: add contracts, config and attribution`
2. `v2: add planner and non-recursive worker`
3. `v2: add supervisor and bounded research waves`
4. `v2: add red research challenge loop`
5. `v2: add lead report and citation audit`
6. `v2: integrate workflow persistence and UI`
7. `v2: add evaluation gates and enable canary`

不得在同一个提交中同时重写工具层、Memory/Vault 和 V2 编排层。

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| V2 与当前 dirty worktree 冲突 | 新文件优先；修改共享文件前查看 diff；不覆盖无关改动 |
| 复制参考代码引入额外依赖 | 只移植小型纯逻辑；优先使用现有 policy、JSON parser 和工具接口 |
| Supervisor 再次偏科 | required question 未分配时拒绝重复低价值 packet；记录全局派工历史 |
| Red/Blue 无限循环 | 硬限制一次完整 Red、一次补充、一次引用修复 |
| 写作仍淹没证据 | Lead 使用选定 Claims，不接收 raw logs；保留 finalization token reserve |
| 引用后补造成错误 | Draft 原生携带 Evidence ID；Audit 只验证/修复现有 Evidence inventory |
| 持久化顺序破坏恢复 | 先用 checkpoint 保存 draft/audit，再由现有 Single Writer 原子发布 |
| canary 失败难定位 | 每个阶段记录独立 trace、结构指标和 termination reason |

回滚只需把 `research.architecture` 设置为 `legacy`；V2 上线前不得删除 Legacy Graph。

## 8. 执行任务的启动说明

新的执行任务必须先阅读：

1. `docs/RESEARCH_AGENT_V2_DESIGN.md`
2. `docs/RESEARCH_AGENT_V2_IMPLEMENTATION_PLAN.md`
3. `docs/ARCHITECTURE.md`
4. `docs/RESEARCH_SUFFICIENCY_TERMINATION_DESIGN.md`

然后从 Phase 0 开始，逐阶段实施和测试。不得跳过阶段验收门，不得直接运行昂贵 canary 验证尚未通过确定性测试的代码。遇到参考实现与 PaperPilot 现有可靠性契约冲突时，以 PaperPilot 的 checkpoint、Evidence、tool alert、预算和 Single Vault Writer 契约为准，并在计划文档记录偏离原因。

## 9. 实施记录

### Phase 0：基线、归因与安全开关

- 状态：验收门通过，允许进入 Phase 1。
- 工作树基线：开始时已有 38 个文件、约 3,058 行新增和 1,201 行删除的未提交改动；全部视为用户/前序任务成果保留，V2 优先新增独立文件。
- 完整测试基线：`825 passed, 2 skipped, 1 failed`。唯一失败为既有 `tests/test_evaluation_embedder.py::test_embedder_cache_is_scoped_by_model_name`，原因是 `evaluation.embedder` 没有测试期望的 `SentenceTransformer` 模块属性，与 V2 无关。
- 单题 canary 基线：复用当前工作树已完成并保存在 `outputs/evaluation/researchbench-evidence-context-v3-canary/ResearchBench_Evaluation_20260831_002702.json` 的 `tech_001` 隔离结果，不重复消耗外部配额。结果为 12 次工具调用、56,248 token、`partial / budget_forced / valid`、Judge 5.8、RCS coverage/sufficiency 仍为 0；未运行三题扩展。
- 新增测试：`tests/test_v2_config.py`，先观察到缺失解析器的预期失败，再实现；专项结果 `24 passed`，受影响运行时与报告复核回归 `68 passed`。
- 阶段后完整回归：`849 passed, 2 skipped, 1 failed`；与基线相比新增 24 项通过，且仍只有同一个既有 embedder 失败，无回退。
- 实现：默认 `research.architecture=legacy`、`supervisor_v2.enabled=false`；V2 配置采用严格类型、范围和未知键校验。Phase 0 若显式请求 V2，会明确失败，绝不静默路由到 Legacy。
- 归因：新增 `THIRD_PARTY_NOTICES.md`，记录 LangChain Deep Research From Scratch 与 GPT Researcher 的固定提交、许可证和参考文件；当前仅借鉴架构/边界，没有复制非平凡上游代码。
- 实际偏离：没有重新运行昂贵的单题 canary，而是记录当前 dirty worktree 已完成的同题隔离 canary；原因是 Phase 0 只要求建立基线，现有 artifact 已对应当前前序实现，重复运行会额外消耗网络配额且不会验证尚未启用的 V2。
- 下一阶段：Phase 1，从测试开始实现 V2 可序列化数据契约与无工具 Planner；默认运行路径继续保持 Legacy。

### Phase 1：V2 数据契约与轻量 Planner

- 状态：验收门通过，允许进入 Phase 2。
- 测试先行：新增 `tests/test_v2_contracts.py` 与 `tests/test_v2_planner.py`，先确认缺失契约/模块的预期收集失败，再实现；专项结果 `16 passed`，与配置、requirement 和 Workflow 受影响回归合计 `78 passed`。
- 阶段后完整回归：`865 passed, 2 skipped, 1 failed`；仍只有 Phase 0 记录的既有 embedder 失败，新增 16 项通过，无回退。
- 契约：`CoreQuestion`、`ResearchPlan`、`WorkPacket`、`EvidenceClaim`、`ResearchChallenge`、`ChallengeDecision`、`CitationIssue` 和 `CitationAuditOutcome` 均为 frozen dataclass/`str` Enum，可通过 `asdict` 进入 JSON checkpoint。
- 稳定 ID：统一使用 canonical JSON（排序键、固定分隔符、UTF-8）与 SHA-256 前 16 位；相同内容跨重跑保持一致，不使用 Python 随机 hash。
- Planner：确认后的每个 atomic direction 都确定性成为 required Core Question；模型最多补充四个 non-required 必要问题，不能改写或挤掉用户确认方向。结构化 policy 调用和唯一一次修复都传入空 tools；两次失败后使用 Brief-only fallback。
- 输入防御：`normalize_sub_queries` 支持数组、`queries` 字典、单 `query`、单字符串、JSON 字符串及无效/空输出，并始终保留原问题作为保守 fallback。
- 归因：Planner 文件标注 GPT Researcher 固定提交的输入归一化设计来源；实现为 PaperPilot 自有代码，没有复制其检索或持久化实现。V2 contract 文件标注 LangChain Supervisor 图边界的设计来源。
- 实际偏离：没有让模型重写 required directions，而是先确定性建立 required 清单、再只接收 non-required 补充；原因是这能直接满足“每个 confirmed direction 必须映射”并避免 Planner 结构修复静默改变用户范围。
- 下一阶段：Phase 2，从现有 `agent_graph.py` 提取一层、无 `fork_research` 的 Blue Worker，同时继续复用 artifact、Evidence、tool availability、预算与 context compaction。

### Phase 2：抽取一层 Blue Research Worker

- 状态：验收门通过，允许进入 Phase 3。
- 测试先行：新增 `tests/test_v2_worker.py` 与 `tests/test_v2_worker_recovery.py`，先确认 Worker 模块缺失的预期收集失败，再实现；专项结果 `6 passed`。
- 受影响回归：Worker、AgentGraph、Legacy fork、tool availability、context compaction 和外部工具韧性合计 `94 passed`。
- 阶段后完整回归：`871 passed, 2 skipped, 1 failed`；仍只有既有 embedder 失败，新增 6 项通过，无回退。
- 共享执行内核：`build_research_agent_graph` 新增 state schema 与 fork capability 参数；Legacy 默认值完全不变。Worker 使用同一组 policy/tool/Evidence/artifact/assessment/compaction 节点，但独立的 `ResearchWorkerState` 明确排除 `pending_fork_calls`、completed fork、child thread 和 child result 通道。
- 无递归：Worker 图不注册 `fork_research` schema、不添加 `fork_children` 节点，Worker identity 固定 depth 1、`max_children=0`、subtree thread budget 1；即使模型伪造 fork 名称也只会作为未知工具被拒绝。
- 证据输出：新增 `BlueWorkerResult`/`BlueWorkerUsage`；只有带 question/action/artifact lineage、source、locator 和 excerpt 的已打开来源才转成 `EvidenceClaim`。`web_search` 摘要在 Worker 输出中过滤为 lead，不进入可交付 Evidence/Claim。
- 可靠性：完整大结果仍先经现有 `persist_tool_artifact`/Single Vault Writer 路径，receipt 后才缩短 Context；checkpoint 重入不会重复工具调用或 artifact 写入；服务不可用告警与 tool/token/time/retry 使用量继续由共享内核返回。
- 隔离：每次 Worker 调用 fork policy，并 clone/fork/deepcopy 工具实例；并行测试证明 policy 与 tool 实例互不共享，同时 Single Vault Writer 协调器保持全局单实例。
- 实际偏离：没有把 AgentGraph 工具循环复制到第二个文件，而是给已验证图增加“无 fork capability + Worker state schema”的可配置编译边界；原因是复制会立刻分叉 Evidence、熔断、预算、artifact 和压缩语义，违反计划的优先复用原则。
- 下一阶段：Phase 3，实现 checkpointed Supervisor 状态、确定性 WorkPacket 派发、并行 gather、两波上限和 Lead finalization reserve。

### Phase 3：Lead Supervisor 与有界并行研究

- 状态：验收门通过，允许进入 Phase 4。
- 测试先行：新增 `tests/test_v2_supervisor.py`、`tests/test_v2_parallel_workers.py` 与 `tests/test_v2_budget_reserve.py`；专项结果 `6 passed`，Phase 0–3 组合回归 `52 passed`。
- 阶段后完整回归：`877 passed, 2 skipped, 1 failed`；仍只有 Phase 0 记录的既有 embedder 失败，新增 6 项通过，无回退。
- Supervisor 边界：新增 checkpointed `supervisor_decide` / `supervisor_execute` 两节点图和 `ConductResearch` / `ResearchComplete` 显式控制契约；模型或调用方请求的终止原因不能覆盖程序计算的 coverage、预算、取消或波次事实。
- 全局派工：所有 required Core Question 在重复或补充前恰好分配一次；问题多于 Worker 上限时确定性分组，工具与 token 预算按组公平分摊，所有 Worker identity 固定为 depth 1。
- 预留预算：在创建首波 WorkPacket 前冻结 Lead finalization reserve，取总 token 的至少 15%、12,000 token 与问题复杂度下限中的最大值（不超过总预算）；Worker 只能消费剩余额度。
- 并行与合并：同一波 Worker 使用 `asyncio.gather` 并行执行；结果按稳定 `packet_id` 排序后合并，完成先后不影响输出。checkpoint 重入直接复用已保存结果，不重启 Worker。
- 有界状态：首波完成后仅在 Phase 4 接受的 Red challenge 显式提供目标问题时创建一次 supplemental wave；当前 Supervisor 不自行把“未回答”升级为补充研究，避免绕过 Red 裁决。达到配置波次上限仍未解决时明确返回 `EVIDENCE_EXHAUSTED`。
- 取消优先级：运行时 `user_cancelled` 具有最高优先级并生成 `USER_CANCELLED`；伪造的 requested completion 不会越过程序守卫。
- 归因：两节点 Supervisor 控制边界注明参考 LangChain 固定提交；稳定 packet ID、全局预算、取消、checkpoint 与确定性合并均为 PaperPilot 自有可靠性实现。
- 实际偏离：将第二波的启动入口保留为显式 `supplemental_question_ids`，而非首波后自动研究所有缺口；原因是设计要求补充波必须由 Phase 4 的 Red challenge 接受结果驱动。
- 下一阶段：Phase 4，实现无工具 Red Research Reviewer、Lead 结构化裁决，以及最多一次、可恢复的 targeted supplemental wave。

### Phase 4：研究前 Red Challenge 与定向补充

- 状态：验收门通过，允许进入 Phase 5。
- 测试先行：新增 `tests/test_v2_research_challenge.py` 与 `tests/test_v2_supplemental_wave.py`，先确认模块缺失的预期收集失败，再实现；专项结果 `9 passed`。
- 受影响回归：V2 contracts/planner/worker/supervisor、Phase 4 以及 Legacy `report_review` 合计 `78 passed`。
- 阶段后完整回归：`886 passed, 2 skipped, 1 failed`；仍只有 Phase 0 记录的既有 embedder 失败，新增 9 项通过，无回退。
- Red 边界：完整审查严格只接收 Plan、Evidence Claims、来源元数据、限制与 unresolved；所有 policy 调用显式传空 tools。仅允许六类 challenge、三档严重度和精确 schema；未知 question/claim ID 以及 Claim 指向的未知 Evidence ID 均拒绝。
- 结构化裁决：Lead 对每个 challenge 恰好返回一次 `accept`、`reject` 或 `defer`；拒绝必须提供当前 inventory 中的 Evidence ID 或明确理由，未知/重复 challenge 与 Evidence ID 均拒绝。
- 定向补充：只有 accepted + high challenge 的目标问题会生成 `wave=supplemental` WorkPacket；复用 Supervisor 剩余预算并保护 finalization reserve，初始与补充合计最多两波。补充后只把原 accepted/high challenges 发送到 delta recheck，不运行第二次完整 Red。
- 恢复：Red、裁决、补充与 recheck 分成独立 checkpoint 节点；完成后重入直接返回标准化结果，不重复 Red policy 或 Worker。checkpoint 反序列化产生的 list/tuple 差异在边界统一恢复为契约 tuple。
- 降级：Red 调用、JSON 或 schema 失败会写入稳定 `red_review_unavailable` quality alert，并由 Supervisor unresolved question 生成 `missing_question` fallback；delta recheck 失败也以独立 quality alert 披露，不会静默消失。
- 复用与归因：`report_review.py` 将既有 JSON fence/parser 提升为共享 helper，同时保留原私有别名，Legacy 行为与测试不变；研究 challenge 文件注明 GPT Researcher 固定提交的有界 reviewer/revision 路由来源。
- 实际偏离：补充研究由 Phase 4 自身 checkpoint 节点调用 Supervisor 的公开补充 packet 分配器，而不是重新打开已经终止的 Phase 3 图；原因是 completed LangGraph checkpoint 没有待执行边，显式阶段节点能保证 Red→补充的恢复边界，同时仍复用相同 packet、预算、worker identity 和执行契约。
- 下一阶段：Phase 5，实现无研究工具的 Lead composer、原生 Evidence ID 引用、确定性加语义 Citation Audit、受约束 repair 与稳定引用顺序。

### Phase 5：Lead Draft、Citation Audit 与修复

- 状态：验收门通过，允许进入 Phase 6。
- 测试先行：新增 `tests/test_v2_report_composer.py`、`tests/test_v2_citation_audit.py` 与 `tests/test_v2_citation_repair.py`，先确认三个新入口缺失的预期收集失败，再实现；专项结果 `10 passed`。
- 受影响回归：V2 contracts/composer/audit/repair、Legacy report review/rendering、Memory write plans 与 Vault Writer 合计 `98 passed`。
- 阶段后完整回归：`896 passed, 2 skipped, 1 failed`；仍只有 Phase 0 记录的既有 embedder 失败，新增 10 项通过，无回退。
- Lead Draft：新增独立 `compose_report`，只向 Lead 提供 Plan、经过 Evidence inventory 校验的 selected Claims、对应 Evidence 元数据和 unresolved question；不传 raw Worker summary/log。调用显式使用空 tools；没有 source+locator+excerpt 的有效 Evidence 时不调用模型，返回明确 abstained/fallback partial 内容。
- 内部引用：Draft 使用 `[[EVIDENCE:evidence-id]]`；composer 立即拒绝未知 Evidence ID、未知 URL 和无效输出 schema。重要事实由后续 Citation Audit 守门，Research Brief 与 Evidence Ledger 不被当作成果正文。
- 确定性审计：在模型调用前检查未知/悬空 ID、缺 locator/excerpt、search-snippet Evidence、未知 URL、提前出现的 WikiLink，以及无近邻 Evidence marker 的材料性段落；确定性失败时不浪费语义调用。
- 语义审计：最多一次、无工具调用，严格解析 `CitationIssue`；claim 必须是报告中的精确文本，Evidence ID 必须来自当前 inventory，只允许 missing/invalid/overclaim/conflict/locator。
- 受约束修复：仅允许 `ADD_CITATION`、`REPLACE_CITATION`、`QUALIFY`、`DELETE`，逐步要求唯一精确 target 并重放；最终 Markdown 必须等于声明式 edits 的结果，且不能新增未知 Evidence、URL 或 WikiLink。修复后重跑确定性检查，不能完全修复则返回 `partial` 与 unresolved。
- 稳定正式引用：`render_evidence_references` 按正文中来源首次出现排序；同一 source URL 使用同一引用号并只产生一条 References 项；相同输入重复渲染完全一致，内部 marker 在正式输出中为零。
- 归因：composer 文件注明 LangChain 独立 final-report node 与 GPT Researcher 无上下文拒写的固定提交来源；其余 marker、inventory、受约束 replay 和 Vault WikiLink 渲染为 PaperPilot 自有实现。
- 实际偏离：语义审计只在确定性检查通过后执行；原因是未知 ID/URL/locator 是程序可判定的安全错误，先调用模型既不能修复事实 inventory，也会浪费 Lead 预留预算。Citation critical follow-up 的额度由 Phase 6 总 Workflow 与既有“两波上限”统一裁决，Phase 5 repair 本身不直接拥有研究工具。
- 下一阶段：Phase 6，将 Planner→Supervisor→Red→Draft→Citation→Persist 接入可配置 Workflow，增加阶段 checkpoint、产品事件、V2 结果披露和审计后单 Writer 持久化。

### Phase 6：Workflow、恢复、持久化与产品接入

- 状态：验收门通过，允许进入 Phase 7；Legacy 默认路径仍保持不变。
- 测试先行：新增 `tests/test_v2_workflow.py`、`tests/test_v2_workflow_recovery.py`、`tests/test_v2_web_progress.py`、`tests/test_v2_persistence.py` 及共享 fake；Phase 6 专项结果 `7 passed`。受影响回归首次发现 `ResearchWorkflowResult` 字段顺序兼容问题，修复后相关 Legacy/V2 测试通过。
- Workflow 切换：`build_research_workflow` 接收 `research_architecture` 与 `supervisor_v2_config`；`legacy` 仍编译原节点和边，`supervisor_v2` 编译 `planning → blue_research → red_review → drafting → citation_audit → persist_result → postprocess`，两条路径不混跑。
- 配置路由：runtime 只在 `research.architecture=supervisor_v2` 且 `supervisor_v2.enabled=true` 时进入 V2；冲突配置明确失败。Legacy 的 `report_review_enabled` 不会隐式改变 V2 Red/Citation 开关。
- 恢复边界：每个 V2 阶段都有独立 checkpoint；阶段产物已存在时直接复用。Supervisor/Red 子图使用独立 checkpoint namespace，同时保留真实 root identity，避免恢复键与线程身份混淆。
- 单 Writer：Draft 和 Citation Audit 结果先留在 checkpoint；只有审计通过或明确 partial 降级后，`persist_result` 才把最终正文交给现有 `MarkdownMemoryStore`/`VaultWriteService` 原子发布。Evidence/Source 仍沿用既有幂等写入与 Memory 隔离。
- 稳定持久化：research bundle request hash、write plan 和 existing-commit reuse 都纳入可选 `report_body_markdown`；相同审计正文重放不产生第二份正式报告。
- 产品投影：SSE/UI 增加 `planning`、`blue_research`、`red_review`、`supplemental`、`drafting`、`citation_audit`、`persisting`；结果披露 architecture、challenges、citation issues、supplemental wave、finalization reserve 和结构计数，不暴露隐藏推理。
- 实际偏离：没有修改 Legacy Graph 节点以复用 V2 阶段，而是在同一 Workflow builder 中按 architecture 编译两张互斥图；原因是这样能维持 Legacy checkpoint/node 名称、恢复语义和回滚开关。
- 下一阶段：Phase 7，先修正 ResearchBench 正文评测与结构指标，再做固定回归和单题 canary；单题不过不得扩三题。

### Phase 7：评测修正、单题 canary 与灰度决策

- 状态：实现与确定性回归完成；单题上线门禁未通过，因此按设计停止扩量，默认配置保持 `legacy`，未运行三题样本，也未删除 Legacy/recursive fork 代码。
- 评测修正：ResearchBench 在 coverage/正文指标前剥离 frontmatter、标题、Research Brief、Memory Context、Execution 与 References/Bibliography，避免输入关键词和引用表虚增成果覆盖。
- V2 指标：新增 Core Question 分配率、Worker 重复率、source-open 比率、Challenge 接受/解决率、material claim citation coverage、invalid citation count、finalization reserve 和 supplemental wave count；新增 `v2_canary_gate` 逐项返回失败原因。
- 固定回归：新增 `tests/test_v2_evaluation.py`，并补充来源污染过滤与高严重度 challenge 裁决守卫测试。修复既有 baseline embedder 的可选依赖探测后，最终完整测试为 `910 passed, 2 skipped`；此前唯一的既有 embedder 失败已消除。
- 单题第 1 次：`outputs/evaluation/researchbench-v2-canary/ResearchBench_Evaluation_20260831_174106.json` 在进入工具层前发现 checkpoint thread identity 与 namespace 混用；修复为“真实 root identity + 独立 checkpoint thread id”，专项恢复测试通过，该次不计作质量结果。
- 单题第 2 次：`outputs/evaluation/researchbench-v2-canary-r2/ResearchBench_Evaluation_20260831_174627.json` 完成但为 `partial`，Judge `1.8/10`，material claim citation coverage `0.5`；定位到无关搜索命中进入 Evidence、Planner source guidance 约束不足，以及 Lead 用承认缺口的理由拒绝高严重度 challenge。
- 定向修复：Planner 强制在 source guidance 中携带原问题、目标、约束和拒绝泛化标题匹配的规则；Worker 拒绝 browser error/warning 与缺少目标实体锚点的泛化命中；对“无 inventory 证据且拒绝理由承认缺口”的 high challenge，程序守卫改为接受；无补充目标时明确终止为 coverage complete/evidence exhausted。
- 单题第 3 次：`outputs/evaluation/researchbench-v2-canary-r3/ResearchBench_Evaluation_20260831_175924.json` 成功触发一次 supplemental wave，无关 Evidence 从前次 17 条降到 2 条，Judge 提升到 `2.4/10`。结构指标为 assignment `1.0`、duplicate `0.0`、source-open `1.0`、challenge acceptance `1.0`、challenge resolution `0.0`、material citation coverage `1.0`、invalid citation `0`、reserve `45,000`、supplemental wave `1`；结果仍为 `partial / evidence_exhausted / repaired`。
- 门禁结论：失败项为 `output_status_not_valid` 与 `judge_average_below_5`；其余结构与预算门满足。失败层已从编排/来源污染收敛到外部可取得证据不足与正文质量，不通过增加预算或题量掩盖，因此停止 canary 扩展并恢复/保持 `research.architecture=legacy`、`supervisor_v2.enabled=false`。
- 实际偏离：计划中的“切换 V2 默认”和“三题小样本”是以单题门通过为前置条件，本次条件未满足，故不执行；这不是遗漏，而是灰度安全门的预期回滚行为。
- 后续阶段：如要继续灰度，只从 `tech_001` 同一单题修复 source acquisition/报告有效性层并重跑单题；只有 `output_status=valid` 且 Judge ≥ `5/10` 后，才允许三题扩展与默认切换。

### Phase 7 后续：Red→补研→成文闭环增强

- 状态：确定性实现与完整回归通过；允许只重跑 `tech_001` 单题验证，不允许扩三题或切换默认架构。
- Grounded Adjudication：Lead 裁决现在接收每个 Challenge 的相关 Claims 以及完整 Evidence 元数据、locator、excerpt 和 limitations；`reject` 必须引用与该 Challenge 相关的 Evidence 并给出理由。只给无关 ID 或不给证据不能驳回；承认缺口的 high rejection 继续由程序守卫改为 accept。
- Challenge 驱动补研：accepted/high Challenge 的显式 question ID 与目标 Claim 的 `question_ids` lineage 合并；`requested_evidence`、`suggested_query`、目标 Claim、Challenge 原因和已打开来源去重提示全部进入 supplemental WorkPacket。同一问题的多项要求确定性合并，仍只生成一波。
- Delta-only Recheck：checkpoint 状态记录 `supplemental_packet_ids`；Red 复查只接收本波新增 Claims/Evidence。`resolved` 必须返回本波新增、且与目标问题相关的 Evidence ID；复用初始波 Evidence、未知 ID 或无 Evidence 的 resolved 均拒绝并保留 Challenge 为 accepted。
- 结构化解决信息：`ResearchChallenge` 新增 `resolution_evidence_ids` 与 `resolution_reason`，并在 checkpoint 恢复时标准化；产品仍可通过原 `status` 字段兼容读取。
- Challenge-aware Composer：`compose_report` 接收完整 `ResearchChallengeLoopOutcome`。accepted/deferred/pending Challenge 的目标 Claims 在写作前确定性剔除，Challenge 必须进入 unresolved；模型若原样重述被阻断 Claim，Composer 明确失败，而不是依赖提示词自觉。
- Citation 条件补研：高严重度且属于 missing/conflict/locator、能够通过 Evidence/Claim 确定性映射回 Core Question、尚未使用第二波且仍有工具/token 额度的 Citation Issue，可触发一次 supplemental wave。首次 Audit 不先修文；补研后重新成文并重新 Audit，此后禁止再次研究。无法建立 lineage 的 Citation Issue 只能限定、删除或 partial 披露，不能启动开放式搜索。
- 实际偏离：没有增加完整的 pre-Blue Red Reviewer；无 Evidence 时只能做计划预检，不能检查弱来源、冲突或过度推断。轻量 Plan Critic 作为可选后续优化，不是本轮修复 Red 闭环的前置条件。
- 测试：新增/扩展 10 个测试，覆盖相关 Evidence 裁决、无关 Evidence 驳回、claim-only 映射、Red 指令进入 WorkPacket、补研解决证据、旧 Evidence 不能冒充 delta、Challenge 阻断成文、Citation lineage 与一次条件补研、分批裁决漏项降级、Composer 附加元数据和程序化 unresolved。最终完整回归：`920 passed, 2 skipped`。环境未安装 Black/Mypy，相关命令明确报告模块缺失；`git diff --check` 无内容错误，仅有既有 LF/CRLF 提示。
- 下一步：临时显式启用 V2，只重跑同一个 `tech_001`；运行结束立即确认配置仍为 Legacy。单题仍须同时达到 `output_status=valid`、Judge ≥ `5/10`、invalid citation `0` 且不 `budget_forced`，否则不扩题、不切默认。

#### 闭环增强后的 `tech_001` 复验

- 所有复验都只运行 `tech_001`；没有运行三题。每次运行结束均恢复 `research.architecture=legacy`、`supervisor_v2.enabled=false`。
- `r4` 首次暴露 Composer 对无害附加 JSON 字段过度严格；改为必须包含 `report_markdown`，保留 Evidence/URL/正文验证，忽略未使用元数据。
- `r5` 暴露无相关 Evidence 的 Lead reject 被升级成异常；改为程序守卫阻止驳回且不中断 Workflow：high→accept，medium/low→defer。
- `r6` 暴露多项 Challenge 裁决包超过上下文压缩阈值、Lead 漏项；改为每批最多三项、每项最多六个 Claim/八个 Evidence、字段长度有界。漏项或无效批次使用 high→accept、其他→defer 的保守裁决，保证每项恰好一个决定。
- `r7` 已完成研究与报告生成，但评测命令未传隔离 checkpoint/vault，Single Writer 正确拒绝覆盖此前同一 memory ID 的不同报告；这是 canary 调用隔离错误，不是放宽持久化冲突契约。后续均使用独立 `checkpoint.sqlite + vault`。
- `r8` 暴露 Composer 研究包仍过大且模型省略 `unresolved`；改为每个 Core Question 最多三条、总计最多十八条高质量 Claim，并紧凑投影 Evidence/Challenge。模型 `unresolved` 只作为可选补充，程序始终加入 Supervisor unresolved question 和未解决 Red Challenge。
- 最终隔离结果：`outputs/evaluation/researchbench-v2-canary-r9/ResearchBench_Evaluation_20260831_193342.json` 完整运行到 Judge，无编排或持久化异常。结果为 `partial / evidence_exhausted / fallback`，Judge `2.2/10`，5 条 Evidence、5 个来源、19 次工具调用、199,857 token、56 项 unresolved；结构指标 assignment `1.0`、duplicate `0.0`、source-open `1.0`、challenge acceptance `0.2`、challenge resolution `0.0`、material citation coverage `0.0`、invalid citation `0`、reserve `45,000`、supplemental wave `1`。
- 门禁结论：闭环可靠性从异常退出提升为可恢复、安全拒写，但上线门仍因 `output_status_not_valid`、material claim citation coverage `<80%`、Judge `<5/10` 失败。报告正确阻断了被 Red 判定为缺失、弱来源或不可比的 Claims；当前瓶颈已经转为 source acquisition 与 Challenge resolution，而非 Red→补研→成文的路由可靠性。不得通过扩大预算或题量掩盖该问题，默认继续保持 Legacy。

#### 替代题 `fin_006` 资料匹配度对照

- 选题：“比较绿色债券（Green Bond）与可持续发展挂钩债券（SLB）在募集资金用途、信息披露和投资者保护方面的差异。”该题预期可由 ICMA GBP/SLBP 官方原则集中支撑，用于区分“题目本身资料稀缺”与“获取链路失效”。
- 首次运行在 Composer 产生未知 `[[EVIDENCE:...]]` 占位符时停止；改为整行删除含伪造 Evidence marker/未知 URL 的内容，加入安全披露并返回 `repaired`，而不是删掉 marker 后保留无支撑断言。从同一隔离 checkpoint 恢复后完整通过 Workflow 与 Judge。
- 最终结果：`outputs/evaluation/researchbench-v2-alt-fin006-r1/ResearchBench_Evaluation_20260831_195340.json`，为 `partial / evidence_exhausted / repaired`，Judge `3.4/10`，6 条 Evidence、3 个来源、24 次工具调用、145,653 token、44 项 unresolved。结构指标为 assignment `0.5`、duplicate `0.0`、source-open `1.0`、challenge acceptance `0.7143`、challenge resolution `0.2`、material citation coverage `0.0476`、invalid citation `0`、reserve `45,000`、supplemental wave `1`。
- 对照结论：更窄、官方来源更集中的题目使 Judge 从 `tech_001` 的 `2.2` 提升到 `3.4`，证明题目与来源匹配度是影响因素；但仍未达门禁。报告显示系统找到正确 ICMA 页面和 GBP PDF，却主要抽取到站点导航文本与 PDF 引言，没有定位 GBP/SLBP 的募资用途、KPI/SPT、披露、验证和条款调整等关键条文。因此不是“网上没有资料”，而是 HTML 正文清洗、PDF 页面/章节定位、Evidence 切片与问题分配仍为主瓶颈。
- 验证：Composer/Citation/Workflow 定向回归 `13 passed`；最终 V2 全部回归 `94 passed`。没有扩展三题，运行结束后已恢复 `research.architecture=legacy`、`supervisor_v2.enabled=false`。

### Phase 8 后续：全文抽取与可定位 Evidence 增强

- 开关与兼容：新增严格 `content_extraction` 配置，默认 `mode=legacy`、Tavily Extract 与 Docling 均关闭、OCR 永久拒绝开启。金丝雀使用独立配置显式开启；`configs/default.yaml` 始终保持 Legacy/V2 disabled。
- HTML：有界下载后使用 BeautifulSoup 清除脚本、导航、页眉页脚、菜单等噪声；按 Core Question 选择相关正文容器，再用 `markdownify` 保留标题、链接、强调、列表和表格；按标题切成 `section:<slug>` 区块并围绕问题排序，不再截取固定的前 8,000 字。
- Tavily：本地 HTML 质量低于门限时才调用 `/extract`，请求 Markdown `raw_content`；不会在搜索阶段为所有候选 URL 抓全文。金丝雀环境未配置 `TAVILY_API_KEY`，因此该回退仅通过确定性测试，金丝雀没有消耗 Tavily 额度。
- PDF：pypdf 逐页提取并生成真实 `page:N` locator；检测到复杂文本版式且开关开启时才懒加载 Docling。Docling 使用本地 PyPdfium 后端，关闭 OCR、远程服务、外部插件、图片与 VLM enrichment，并设置 90 秒文档超时；扫描件明确返回 unsupported。Windows 中文工作路径下，默认 docling-parse 后端出现字符序列错误，已改用官方支持的 PyPdfium 后端并用单页文本 PDF 实测得到一页 provenance 文本。
- Evidence：Browser 新字典输出按区块生成独立 Evidence，保存真实 URL、section/page locator、excerpt、extractor warning 以及 requirement/action/artifact lineage；旧字符串 Browser 输出继续兼容。AgentGraph 对声明支持的 Browser 自动注入当前 action query。
- 参考边界：LangChain 继续只作为 Supervisor/Worker 编排参考；GPT Researcher 只借鉴 scraper routing、清理与错误回退模式，没有引入整个运行时或复制非平凡代码；Docling 是可选本地库；没有加入 Firecrawl、动态浏览器、PyMuPDF 或 OCR。
- 确定性验证：最终内容抽取/Evidence 专项 `83 passed`，V2 组合 `118 passed`，最终完整回归 `945 passed, 2 skipped`。

#### `fin_006` 全文抽取金丝雀

- 基线仍为 `researchbench-v2-alt-fin006-r1`：Judge `3.4/10`、6 Evidence、3 sources、24 tool calls、145,653 tokens、material citation coverage `0.0476`、challenge resolution `0.2`。
- 中间 `structured-r1`：Judge `5.0/10`、26 Evidence、7 sources、20 tool calls。它证明 pypdf 成功产生 9 个 `page:N` 区块，但也暴露 ICMA HTML 页脚标题会吸收正文并产生错误 section locator，因此不作为最终结果。
- 最终 `structured-r2`：`outputs/evaluation/researchbench-v2-structured-fin006-r2/ResearchBench_Evaluation_20260831_222212.json`。结果为 `partial / evidence_exhausted / repaired`，Judge `5.4/10`、11 Evidence、7 sources、16 tool calls、154,671 tokens、51 unresolved。结构指标 assignment `0.6`、duplicate `0.0`、source-open `1.0`、challenge resolution `1.0`、material citation coverage `0.7333`、invalid citation `0`、reserve `45,000`、supplemental wave `1`。
- 结论：新抽取链路显著改善来源可用性和报告质量，同时 HTML 容器修正将导航噪声 Evidence 从 26 收敛到 11；但上线门仍因 `output_status != valid`、material citation coverage `< 0.8`、partial/evidence exhausted 失败。Judge 已越过 `5/10`，不能抵消其余硬门。因此不扩展三题、不切默认；下一瓶颈是无 Tavily key 导致搜索后端不可用、模型猜测过期 PDF URL，以及部分 worker 问题分配和无关学术检索。

#### Tavily 密钥恢复后的 `fin_006` 对照

- 环境核对：当前隔离 worktree 原本没有 `.env`/`.env.local`，而原项目 `D:\Claude\deepresearch-agent\.env` 含可用 `TAVILY_API_KEY`。经用户确认后只把该被 Git 忽略的 `.env` 复制到 worktree；验证仅检查变量存在和长度，没有输出密钥内容。
- 独立结果：`outputs/evaluation/researchbench-v2-structured-fin006-r3/ResearchBench_Evaluation_20260831_231542.json`。结果为 `partial / evidence_exhausted / fallback`，Judge `1.8/10`、3 Evidence、2 sources、24 tool calls、221,027 tokens、40 unresolved。结构指标 assignment `1.0`、duplicate `0.0`、source-open `1.0`、challenge resolution `0.0`、material citation coverage `0.0`、invalid citation `0`、reserve `45,000`、supplemental wave `1`。
- 工具轨迹：共持久化 23 个工具 artifact，其中 20 次 `web_search` 全部由 Tavily 成功返回、无 backend error；仅有 3 次 `browser`，且只打开 ICMA GBP/SLBP 两个落地页（GBP 重复一次）。三次均由 `beautifulsoup+markdownify` 本地抽取；`tavily-extract=0`、`pypdf=0`、`docling=0`。
- 根因：Tavily 已在搜索结果中返回 World Bank PDF、中国交易商协会规则正文和 ICMA 相关来源，但当前研究循环仍由模型自行决定是否把搜索 URL 交给 Browser。模型连续使用搜索摘要，却没有打开这些候选 URL；Worker 又按设计过滤 `Search-result snippet`，禁止其进入可交付 Evidence。于是更好的搜索召回没有转化为强证据，Red 正确判定只有落地页概述并触发安全拒写。
- 决策：这轮证明瓶颈已不是密钥或网上资料不足，而是 discovery→acquisition 缺少确定性交接。下一步应在搜索结果之后程序化排序、去重并打开高价值候选（优先官方域名/PDF，按 Core Question 绑定且有上限），再进入现有格式路由：HTML→本地 Markdown/必要时 Tavily Extract，PDF→pypdf/必要时 Docling。修复前不再重复昂贵 canary，不扩三题、不切默认架构。

#### 统一资料获取器与宽松兜底预算

- V2 新增 `acquire_evidence` 高层工具，内部复用现有 Tavily/秘塔/Exa/博查/SerpAPI 搜索回退与 Browser 抽取；Legacy 继续暴露原 `web_search`/`browser`，默认架构仍为 `legacy`。V2 同时配置搜索和 Browser 时只向 Worker 暴露高层工具，单次 Agent 动作确定性完成搜索、候选排序、规范化 URL 去重、自动打开与 Evidence 返回。
- 候选排序优先查询/标题匹配、显式 preferred domain、官方/教育域名、原始标准/报告，再考虑 PDF；发行人模板、case study 等次级文档降权。真实小流量测试已从误选 ICMA 域名下发行人模板修正为优先 ICMA GBP 官方页面，并同时打开监管机构 PDF。
- 跨 Worker 使用共享 registry 缓存完整文档；相同规范化 URL（去 fragment、UTM/追踪参数、规范查询顺序）并发只抓一次，其他 Worker 复用文档并保留各自 requirement/action/artifact lineage。
- Agent 硬兜底放宽为 `18` iterations、单 Worker `30` tool calls、全局 `96`、`900s`、`500,000` tokens、`10` threads、单次/全局 retry `2/12`，研究波次上限 `3`。四 Worker 首轮在 96 次全局池下约获 24 次，而不是旧配置的约 9 次；未使用额度由后续定向波按实际消耗重新分配。Lead 继续预留 15% token。
- 新增真实性指标：候选数、打开数、`search_to_open_rate`、重复/缓存率、acquisition call 数和每次 acquisition 的 Evidence 增量；`source_open_ratio` 不再用 Evidence 数自比得到虚假的 `1.0`。Core Question assignment 的分母修正为必须问题，而不是 Planner 可选补充问题。
- 第四轮独立获取阶段：`researchbench-v2-structured-fin006-r4` 共持久化 19 个 acquisition artifact，搜索候选 148、返回文档 46（含跨 Worker cache hit 21）、正文区块 189；44 个 HTML 使用 `beautifulsoup+markdownify`，2 个 PDF 使用 `pypdf`。最终研究结果为 148 Evidence、26 sources、24 Agent tool calls、335,798 tokens，并以 `coverage_complete` 而非 budget forced 结束，证明 discovery→acquisition 闭环已贯通。
- 第四轮首次在 Composer 非 JSON 中断；加入紧凑 Composer inventory（每问题 2 Claim、总计 12）与一次无工具 JSON 修复后，从原 checkpoint 恢复且没有重跑 19 次 acquisition。评测结果 `ResearchBench_Evaluation_20260901_003513.json` 为 `partial / coverage_complete / repaired`、Judge `4.4/10`。该报告仍因安全引用回退输出 123,748 字符、material citation coverage `0.2512`、invalid citations `4`，表明瓶颈从证据不足转为 Evidence 过量与下游选择。
- 后续确定性收敛：Worker 可交付 Evidence 现在每问题最多 6、同来源最多 2、每 packet 最多 18；引用修复失败时先仅删除不安全精确行并重跑确定性审计，只有无法保留结构化 Draft 时才生成最多 18 条、每条 1,000 字符的安全部分报告。该后续修复已通过专项测试，尚未用新的昂贵单题从头复验，因此不得以 r4 Judge 分数宣称最终质量门通过。
- 测试：V2 组合 `124 passed`，共享 AgentGraph/Legacy 回归 `122 passed`，最终完整套件 `952 passed, 2 skipped` 且只有一个短租约阻塞输入时序测试在整套高负载下超时；该测试单独重跑 `1 passed`。默认仍为 `research.architecture=legacy`、`supervisor_v2.enabled=false`，未扩三题、未切默认。

#### Evidence 限流与安全回退后的第五轮干净复验

- 使用独立 `researchbench-v2-structured-fin006-r5` checkpoint、Vault 与 retrieval DB 从零运行 `fin_006`，没有复用第四轮研究结果。研究阶段耗时 `888.578s`，在 `900s` 研究兜底内以 `coverage_complete` 结束；含规则评测与 LLM Judge 的总耗时为 `915.391s`。
- 最终评测：`outputs/evaluation/researchbench-v2-structured-fin006-r5/ResearchBench_Evaluation_20260901_010539.json`，结果为 `partial / coverage_complete / repaired`，Judge `7.2/10`、规则综合分 `0.6426`、34 Evidence、21 sources、30 tool calls、409,417 tokens、39 unresolved。最终报告正文为 `9,205` 字符，完整进入单个 Judge chunk；相较 r4 的 148 Evidence 和约 123,748 字符安全回退，Evidence 限流与有界 fallback 已实际生效。
- 获取轨迹：共 27 个工具 artifact，其中 24 次 `acquire_evidence`、3 次 `arxiv_reader`。统一获取器从 Tavily 返回 182 个候选，选择 74 个 URL，成功返回 63 个文档，11 个抓取失败，跨 Worker cache hit 21；抽取器分布为 55 个 `beautifulsoup+markdownify`、6 个 `pypdf`、2 个 `tavily-extract`，共形成 277 个正文区块。真实 `search_to_open_rate=0.3462`，不是把搜索摘要误计为 Evidence。
- V2 结构指标：required Core Question assignment `1.0`、Worker duplicate `0.0`、duplicate source `0.1154`、每次 acquisition 产生 `1.4167` 条最终 Evidence、Challenge acceptance `0.625`、Challenge resolution `0.2`、material claim citation coverage `1.0`、invalid citations `0`、supplemental wave `1`。
- 质量判断：Judge、引用覆盖、无效引用、assignment 和非 budget-forced 项已经过门，但 `output_status` 仍为 `repaired` 而非 `valid`，整体仍为 `partial`。人工检查还发现安全删行使对照表缺少表头/前几行，Red 留下 4 项 accepted 未解决挑战，并指出部分关键结论依赖求职问答站、咨询博客或二手转载。故本轮证明获取与下游收敛路线有效，但尚未达到切默认条件。
- 决策：不扩三题、不切默认；`configs/default.yaml` 继续保持 `research.architecture=legacy`、`supervisor_v2.enabled=false`、`content_extraction.mode=legacy`，Tavily Extract、Docling 与 OCR 默认关闭。下一步优先修复“引用安全删行破坏 Markdown 表格结构”、提升官方一手来源排序/约束，并让 Red 的 accepted challenge 能更高比例映射为定向补证，而不是继续放宽全局预算。

#### 第五轮后：安全完成、Red 未决披露与表格修复

- 状态语义：V2 不再把“存在任何质量披露”直接等同于 `partial`。只有 required Core Question 未覆盖、Draft fallback 或 Citation Audit 仍为 partial 才视为关键不完整；Worker 限制、Red 已披露未决和修复历史仍进入审计信息，但不会单独降级研究状态。
- 修复与有效性解耦：Composer/Citation 的安全修复在最终确定性审计无剩余问题时返回 `output_status=valid`；`ResearchResult` 和 `ResearchWorkflowResult` 新增 `repair_applied`、`repair_actions`，评测 JSON 同时报告修复历史。仍有 citation issue 时继续返回 `repaired/partial`，fallback 语义不变。
- Red 终态：新增 `unresolved_disclosed`。accepted/deferred/pending Challenge 在有界补研/复核结束后必须进入该终态，相关目标 Claim 继续被 Composer 阻断或限定；它是安全完成而非伪造 `resolved`。报告新增“红方未解决问题 / Unresolved Red-team issues”表格，包含重要程度、已采取行动、正文影响和后续建议，不再输出内部 challenge ID 日志。
- 高严重度缺口守卫：`missing_question/high` 不能由 Lead 在初始裁决中直接 reject，必须至少经过一次有界 supplemental verification；其他 reject 继续要求直接相关 Evidence。medium/low 问题允许不触发补研，并在报告中以 `unresolved_disclosed` 公开。
- 指标：保留 Challenge resolution rate，同时新增 `unresolved_challenge_disclosure_rate`。上线安全条件关注 high Challenge 是否 resolved、grounded rejected 或安全披露/阻断，而不要求所有现实问题达到 100% resolution。
- Markdown 安全修复：未知 Evidence/URL 或 Citation issue 不再用无结构的逐行过滤。安全 body row 可单独删除；若表头或分隔行不安全，则把剩余安全行降级为项目列表，避免留下从 `|---|` 开始的损坏表格，同时不保留不安全断言。
- 一手来源排序：提高 government/education/international/organization 域名和 official/principles/standard/regulation/framework 等信号权重，降低 blog/interview/opinion/summary/转载等二手信号；对权威域名允许同次 acquisition 打开两个高分文档，避免 GBP/SLBP 同域时被“一域一个”多样性规则互相挤掉。搜索后端仍为可回退的通用接口，并未硬编码 Tavily。
- 预算核对：第五轮的 `30` 是 6 个 Worker 的累计工具用量，单 Worker artifact 数最高为 `8`，没有触发 `max_tool_calls=30`。初始 packet 继续均分全局 `96` 次硬上限，补充波按实际已用额度重新计算；本轮未决的主因是 Red 路由与证据相关性，不是局部预算耗尽，因此没有继续放宽预算。
- 验证：新增状态/修复历史、表格降级、高严重度缺口守卫、未决终态和同域官方双文档排序测试。专项 `40 passed`，扩大 V2/获取/结构抽取回归 `132 passed`，最终完整回归 `958 passed, 2 skipped`。默认配置继续保持 Legacy；尚未启动第六轮外部 canary。

#### 第六轮 `fin_006` 干净复验与 Composer 库存修复

- 独立运行：`outputs/evaluation/researchbench-v2-structured-fin006-r6/ResearchBench_Evaluation_20260901_015631.json` 使用全新 checkpoint/Vault 从零执行。状态语义按设计变为 `completed / coverage_complete / valid`，并单独记录 `repair_applied=true`、`repair_actions=[composer_safety_repair]`；证明质量披露与修复历史已经成功从有效性状态中解耦。
- 结构结果：35 Evidence、25 sources、35 tool calls、410,766 tokens、44 unresolved；required assignment `1.0`、invalid citations `0`、Challenge resolution `0.2857`、unresolved disclosure `1.0`。Red 最终为 2 resolved、5 `unresolved_disclosed`、0 rejected，报告中的未决表格结构完整。
- 质量失败：Judge `2.0/10`、material citation coverage `0.0`，报告正文仅 `3,200` 字符。Composer 使用了大量未知 Evidence marker，现有安全路径正确删除不安全行，但几乎只剩章节标题和 Red 未决表。因此 `valid` 只表示最终产物没有悬空引用，不代表内容质量门通过；Judge 与 citation coverage 继续正确阻止灰度。
- 获取轨迹：30 次 acquisition 返回 238 candidates、选择 90 URLs、得到 63 documents、27 fetch errors、18 cache hits；抽取结果为 56 HTML、5 pypdf、2 Tavily Extract，共 236 blocks。全部搜索由 Tavily 成功完成，通用回退未触发。
- Docling 性能发现：至少 4 份 PDF 触发重型版面解析，模型每次重复初始化；其中 2 次超过 90 秒并出现后台 table thread 未及时终止，最终 63 份可用文档中 `docling=0`，即本轮付出数分钟成本却没有一个 Docling 结果优于 pypdf。研究阶段因此达到 `995.468s`，超过配置的 900 秒目标，虽然当前 termination reason 仍为 coverage complete。后续应缓存 converter，并在 pypdf 已有足够查询命中时跳过 Docling；不应提高总时限掩盖该问题。
- Composer 库存修复：在未知 Evidence ID/URL 与删行之间新增一次无工具、允许库存受限的重写。修复调用同时获得原 bounded Evidence package、错误报告、允许的 Evidence IDs/URLs；只有重写结果完全落在 inventory 内才接受，否则继续执行安全删行。修复成功会保留正文并记录 `composer_safety_repair`，不会把修复历史当作研究不完整。
- 最终验证：Composer/Workflow/Citation 专项 `23 passed`；加入新测试后完整回归 `959 passed, 2 skipped`。r6 作为失败样本保留且不覆盖；本轮之后没有再次消耗外部 canary。默认配置继续保持 Legacy，未扩三题、未切默认。

#### Docling 触发收敛、进程复用与熔断

- 触发规则：Docling 不再仅因 PDF 看起来版式复杂就运行。只有查询明确要求表格、矩阵、明细表等结构，且 pypdf 提取的正文仍不足以回答查询时，才允许进入 Docling；普通文本型 PDF 即使版式复杂也继续使用 pypdf。
- 轻量优先：pypdf 先按页提取并检查有效字符量、页数和查询词覆盖。正文已经充分时直接提前结束，避免为同一份资料重复支付重型解析成本。Docling 的表格结构模型也只在查询明确需要表格时开启；OCR、远程服务、插件、图片和 VLM enrichment 继续关闭。
- 运行时复用：Docling 移入独立子进程，并在进程内按解析选项缓存 converter。同一研究进程后续确实需要 Docling 时复用已加载模型，不再每份 PDF 重复初始化。
- 硬超时与熔断：单份文档设 60 秒硬超时。超时会终止整个 Docling 子进程，阻止后台表格线程继续占用资源，并在当前主进程余下生命周期内打开熔断；后续 PDF 立即回退 pypdf，不再反复尝试同一重型路径。
- Windows 真实验证：使用同一份 8 页 ICMA PDF，首次独立进程解析约 `25.0s`，第二次复用 converter 约 `7.5s`，两次得到相同 8 页结果；将硬超时设为 `0.1s` 时，子进程被实际终止，熔断打开，第二次调用立即拒绝重试。
- 下一轮配置：新增独立 `researchbench-v2-structured-fin006-r7-config.yaml`，明确设置 `docling_enabled=false`。第七轮先验证统一获取器、Composer 库存修复和 Red 闭环，不让 Docling 性能干扰结果；需要表格结构的专项评测以后再单独显式启用。
- 验证：内容抽取专项 `33 passed`；最终完整回归 `961 passed, 2 skipped`。测试结束后的 LangSmith `429`/连接重试只影响遥测上传，进程最终以退出码 `0` 完成，不影响本地测试结论。
- 默认审计：`configs/default.yaml` 继续保持 `research.architecture=legacy`、`supervisor_v2.enabled=false`、`content_extraction.mode=legacy`，Tavily Extract、Docling 与 OCR 均默认关闭；没有切换默认架构，也没有启动新的外部 canary。

#### 第七轮环境失效与第八轮有效复验

- `r7` 使用全新隔离 checkpoint/Vault 运行，但运行环境未安装项目已经在 `pyproject.toml` 与 `requirements.txt` 声明的 `markdownify`。28 次 acquisition 找到 211 个候选、选择 80 个 URL，却只有 6 个 PDF 成功打开；74 个 HTML 全部在结构化抽取时触发 `ModuleNotFoundError: No module named 'markdownify'`，而获取器将其统一压缩成 `Browser returned no structured content`。最终仅 16 Evidence、10 sources，状态 `partial / repaired`，Judge `2.8/10`、material citation coverage `0.1268`、invalid citation `2`、研究耗时 `933.813s`。该轮属于运行环境无效样本，不用于判断架构质量。
- 环境修复：在实际执行评测的共享虚拟环境安装已声明依赖 `markdownify 1.2.3`。随后对 ICMA SLBP 与 IFC SLB 页面做真实网络复验，两页均恢复 `beautifulsoup+markdownify`，分别得到 1 与 3 个结构化正文区块；Docling 与 OCR 仍关闭。
- `r8` 使用新的隔离 checkpoint/Vault 从零重跑 `fin_006`，没有复用 r7 坏数据。20 次 acquisition 找到 160 个候选、选择 60 个 URL、成功打开 40 个文档、20 个抓取失败、11 个 cache hit；抽取器为 34 个 `beautifulsoup+markdownify`、3 个 `tavily-extract`、3 个 `pypdf`，Docling 为 0。
- 最终结果：`outputs/evaluation/researchbench-v2-structured-fin006-r8/ResearchBench_Evaluation_20260901_033617.json`，状态 `completed / coverage_complete / valid`，Judge `7.2/10`、规则综合分 `0.6435`、24 Evidence、14 sources、25 tool calls、407,325 tokens。研究阶段 `629.625s`，含规则评测与 Judge 总计 `696.625s`，明显低于 900 秒兜底，也优于 r5 的约 889 秒与 r6 的约 995 秒。
- 硬门：required assignment `1.0`、material citation coverage `1.0`、invalid citation `0`、Worker duplicate `0.0`，且非 budget-forced；Citation repair 被明确记录为 `repair_applied=true`，不再错误降级有效状态。本轮首次同时保住 r5 的 Judge `7.2` 内容质量与 r6 的 `valid` 状态，单题金丝雀通过。
- Red：8 个 Challenge 全部进入 `unresolved_disclosed`，披露率 `1.0`、resolution `0.0`。报告安全保留了中国监管原文、真实案例/市场数据、SLB coupon/违约条款等证据缺口，没有伪造解决；但 Composer 正文中仍有一句“无挑战信息”，与程序追加的 Red 未决表矛盾。扩展多题灰度前应确定性删除或改写这类与实际 Challenge 状态冲突的模板句。
- 决策：默认仍保持 Legacy，不直接切换。下一步先做依赖 fail-fast 与 acquisition 原始错误透传，修复 Composer 挑战状态一致性，再用两道不同类型问题进行小规模灰度，而不是继续重复 `fin_006`。

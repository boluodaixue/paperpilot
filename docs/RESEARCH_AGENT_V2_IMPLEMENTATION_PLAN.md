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

#### 依赖预检、Browser 错误透传与跨类型小规模灰度

- 启动依赖检查：结构化 Browser 初始化时先验证 `beautifulsoup4`、`markdownify`、`pypdf`；仅在显式启用 Docling 时验证 `docling`。缺包或导入失败会在研究开始前一次性报出，而不是消耗搜索预算后让所有 HTML/PDF 静默失败；Legacy 模式不加载这些可选依赖。
- Browser 原始错误：统一获取器的并发共享任务现在同时返回 document/error；失败记录保留请求 URL、Browser status、具体异常和 warnings。两道灰度共 51 个 fetch error 中，`Browser returned no structured content` 为 0；可见错误包括 HTTP 403/404/412、SSL 失败及具体重定向目标。
- Composer/Red 一致性：只要存在 research challenge，Lead 提示明确禁止声称“没有挑战”；成文后再确定性删除中英文矛盾句，并把安全修复加入 unresolved/history。`unresolved_disclosed` 继续作为有约束力的未解决状态，目标 Claim 不会因披露而重新进入事实正文。
- Composer schema 容错：法律题前两次隔离运行分别暴露“JSON 修复仍不是合法 JSON”和 `unresolved` 类型错误。第二次格式修复仍失败或正文缺失时，Composer 现在只用已经过 inventory 校验的 Evidence Claims/IDs 生成确定性降级报告；`null`、单字符串、混合数组或其他 `unresolved` 形态会安全规范化，并明确记录 `repaired`，不再把已完成的研究整体判为失败。
- 定向回归：依赖、获取器、Composer 与 Citation 相关回归最终为 `43 passed`。灰度前的扩大回归为 `75 passed, 1 skipped`；此前完整回归为 `963 passed, 3 skipped, 1 failed`，唯一失败是 60ms lease heartbeat 的既有负载时序抖动，隔离重跑 `1 passed`。本节新增 Composer 容错后关闭 LangSmith/LangChain tracing、使用 ASCII 临时目录执行最终完整回归：`973 passed, 3 skipped`，耗时 `146.84s`、退出码 `0`。

法律灰度 `law_002`（PIPL 与 GDPR 跨境传输及合规成本）：

- 有效结果：`outputs/evaluation/researchbench-v2-grey-law002-r3/ResearchBench_Evaluation_20260901_051425.json`；状态 `partial / coverage_complete / repaired`，Judge `3.2/10`，30 Evidence、21 sources、25 tool calls、388,868 tokens、59 unresolved；研究阶段 `961.688s`，总计 `1,029.953s`。
- 结构指标：material citation coverage `0.3333`、invalid citations `7`、Challenge acceptance `1.0`、resolution `0.0`、unresolved disclosure `1.0`；报告公开 9 项未解决 Red 问题，没有出现“无挑战”矛盾句。
- 获取轨迹：21 次 acquisition、168 candidates、64 selected、45 opened、7 cache hits、19 fetch errors；40 个 `beautifulsoup+markdownify`、3 个 `pypdf`、2 个 `tavily-extract`。调用预算未耗尽，主要失败不是搜索/打开上限。

医疗灰度 `med_004`（CRISPR 治疗 SCD/TDT 的疗效与长期安全）：

- 有效结果：`outputs/evaluation/researchbench-v2-grey-med004-r1/ResearchBench_Evaluation_20260901_053203.json`；状态 `partial / coverage_complete / repaired`，Judge `3.8/10`，43 Evidence、21 sources、29 tool calls、369,836 tokens、58 unresolved；研究阶段 `935.813s`，总计 `1,024.110s`。
- 结构指标：material citation coverage `0.8857`、invalid citations `17`、Challenge acceptance `0.9091`、resolution `0.0`、unresolved disclosure `1.0`；报告公开 10 项未解决 Red 问题、拒绝 1 项，没有把长期安全性缺口伪装成已解决。
- 获取轨迹：25 次 acquisition、195 candidates、79 selected、47 opened、18 cache hits、32 fetch errors；28 个 `beautifulsoup+markdownify`、4 个 `pypdf`、15 个 `tavily-extract`。NEJM 直连 403 时仍从可访问 PDF 得到页码化临床结果，证明 PDF 轻量抽取链路可用。

灰度结论：

- 本轮要求的三个可靠性问题已经关闭：结构化依赖可 fail-fast、Browser 原始错误可审计、Composer 不再生成与 Red 状态直接矛盾的“无挑战”断言；两类问题也都能跑到完整评测而不因 Composer schema 崩溃。
- 多领域质量门未通过。两题均非 budget-forced，低分主因是 Citation Audit/repair 把大量带内部 Evidence ID 的材料性段落降级，同时抓取正文中的站内 URL 进入最终报告后被判为 invalid；报告因此膨胀成“原始正文片段 + citation issue 清单”，而不是紧凑的研究综合。法律题还混入澳大利亚隐私法和低质量成本估算，医疗题则出现同一 NEJM/EMA URL 同时被列为 invalid 与 missing 的审计矛盾。
- 下一步不应继续提高调用预算或立即切默认。应先修 Citation Audit 的内部 marker/正式 URL 边界、禁止原始抓取正文直接进入最终 Supported findings、合并去重互相矛盾的 citation issues，并让审计失败保留可验证的已引用综合正文，而不是把整段移入 unresolved。
- 默认审计保持 `research.architecture=legacy`、`supervisor_v2.enabled=false`、`content_extraction.mode=legacy`，Tavily Extract、Docling、OCR 默认关闭；本轮没有切换默认架构。

#### 结构化 Claim/Citation 边界与跨类型复验

- Composer 合同由自由 `report_markdown` 改为结构化 `ReportSection -> ReportAssertion{text, claim_ids}`。Lead 不再书写 Evidence marker、URL 或引用列表；程序通过 `claim_ids -> evidence_ids` 确定性生成 `[[EVIDENCE:id]]`，最终 Renderer 再统一生成 References。Composer 上下文不再包含 `source_ref` 或原始 excerpt，只接收清洗后的 Claim、来源标题/类型和 locator。
- 新增 Claim hygiene：正文禁止裸 URL、Markdown 图片/链接、HTML、导航与长网页块。对于含有效事实但形态嘈杂的 finding，采用纯抽取式原子 Claim 派生，从已有文本中选择与 Core Question 最相关的事实句；不调用模型改写，不新增事实。纯导航/元数据仍隔离，并记录 `reportable_claim_rejection_count`。
- Core Question 选择改为轮转：先为每个问题选择第一条 Claim，再选择第二条，避免前几个问题占满 Composer 上限。Composer 若遗漏已有 Claim 支撑的必答问题会触发一次结构修复，仍失败则确定性使用验证后的 Claims；没有任何可报告 Claim 的问题写入 `uncovered_question_ids` 并使 ResearchStatus 保持 partial。
- Citation Audit 现在只接受内部 Evidence marker，任何正文裸 URL 均为结构错误；含 URL/未知 marker 的行不会同时再生成 missing issue。同一文本只保留优先级最高的问题（invalid > conflict > overclaim > locator > missing）。修复后的历史 issue 保存在结构化 ledger，并标记 `repaired/removed`；评测只把仍未解决的 invalid/locator 计入 `invalid_citation_count`。
- 安全回退不再把 `Citation audit removed or downgraded: <完整原文>` 写回报告。报告只显示聚合数量，详细 claim_text 仅保存在评测 JSON；Red 未解决问题独立追加，并对 reason/follow-up 做 URL 清洗和长度限制。新增硬指标：`raw_url_count_before_render`、`citation_issue_conflict_count`、`audit_log_leak_count` 必须均为 0。
- 定向回归：结构化 Composer、Citation、Workflow、评测与并发新增的共享管线/Blackboard 组合为 `51 passed`。完整回归在中间版本得到 `968 passed, 3 skipped, 1 failed`；唯一失败是 Web 异步轮询在 LangSmith 429 负载下仍停留在 proposal，关闭遥测后隔离重跑 `1 passed`。合并最终代码并关闭遥测后全量回归为 `996 passed, 3 skipped`，退出码 `0`。

法律 `law_002`：

- `r4`（结构化 Citation 边界、原子 Claim 派生前）：`completed / coverage_complete / valid`，Judge `3.4/10`，38 Evidence、23 sources、33 tool calls、411,400 tokens。material citation coverage `1.0`、invalid `0`、裸 URL `0`、citation issue conflict `0`、audit-log leak `0`；但 `reportable_claim_rejection_count=31`，正文几乎只有 GDPR 两个二手来源，缺失 PIPL 与成本比较。
- `r5`（加入 Core Question 轮转与抽取式原子 Claim）：`completed / coverage_complete / valid`，Judge `2.6/10`，34 Evidence、23 sources、30 tool calls、418,054 tokens。四项 Citation 边界指标继续全部为 0，Claim 隔离数从 31 降到 1；但语义 Citation Audit 将 16 条 invalid 与 5 条 overclaim 标记为 removed，最终只剩一份中国网信办来源。该结果证明简单清洗能恢复候选 Claim，却不能替代 claim/excerpt 的真实语义支撑。

医疗 `med_004`：

- `r2` 在研究正式开始前因一次 SQLite `database is locked` 中止，0 Evidence、0 tool calls，属于无效运行样本；全新 checkpoint 的 `r3` 未复现锁错误。
- `r3`（结构化 Citation 边界 + 问题覆盖守卫）：`partial / coverage_complete / valid`，Judge `1.6/10`，33 Evidence、17 sources、33 tool calls、413,321 tokens。material citation coverage `1.0`、invalid `0`、裸 URL `0`、citation issue conflict `0`、audit-log leak `0`，`reportable_claim_rejection_count=23`。问题覆盖守卫正确把缺失临床疗效、长期安全和监管一手文件的产物保持为 partial；最终仅一条 ATMP 上市后措施证据存活，未把空报告误标 completed。

本轮结论：

- 已解决原灰度的 Citation 边界根因：没有正文裸 URL、同一内容不再同时 invalid/missing、审计原文不再泄漏到 Supported findings、正式引用只有一条确定性链路。
- 多领域内容门仍未通过，且 Judge 均低于 5。下一瓶颈不再是预算、JSON schema 或 Citation renderer，而是获取/选择阶段没有稳定命中问题所需的一手资料，以及 `EvidenceItem.finding` 与 `excerpt` 缺少逐条蕴含保证。下一步应在 Evidence 进入 Claim 前增加 entailment/relevance gate，并针对 Core Question 做官方来源缺口回补；不应放宽 Citation Audit 或继续提高预算。
- Canary gate 同时要求 `research_status=completed`；因此最终自动判定为：`law_002` 因 `judge_average_below_5` 阻断，`med_004` 因 `research_status_not_completed` 与 `judge_average_below_5` 阻断。新增 gate 回归与 CLI 评测回归 `24 passed`。
- 默认继续保持 `research.architecture=legacy`、`supervisor_v2.enabled=false`、`content_extraction.mode=legacy`；未切换默认架构。

#### 第一性原理 Evidence–Claim 中间层重构

- 根因修正：删除 V2 生产路径中的 `EvidenceItem.finding -> EvidenceClaim` 直通。`finding` 不再被视为可交付结论；原始资料依次进入 `SourceDocument -> EvidencePassage -> CandidateClaim -> SupportAssessment -> EvidenceClaim`。`EvidencePassage` 保存 exact text、locator 与 content hash；Candidate 必须携带 exact quote 且该 quote 可确定性定位于 Passage。
- 证明义务：新增 `EvidenceRequirement`。Planner 可为每个 Core Question 生成 1–3 个明确证明槽位，声明 evidence kind、最低 verified Claim 数、最低独立来源数和是否必须一手来源；模型缺省或结构错误时，每个 Core Question 至少生成一个保守 Requirement。Worker 的 research requirement ID 已改为 EvidenceRequirement ID，而 Blackboard 继续以 Core Question 协调所有权。
- 独立验证：Worker 完成资料获取后，另起一次无工具 Support Verifier 调用。Verifier 只看到 Requirement、Candidate 和对应 Passage，不能搜索、改写 Claim 或使用外部知识；verdict 为 `entailed / partially_entailed / contradicted / irrelevant`。只有 `entailed` 且 confidence >= 0.7 才物化 EvidenceClaim。partial 仅在 supported_scope 是 Passage 中的 exact substring 时生成更窄 Candidate，并再独立验证一次。
- 安全回退：Verifier 不可用时只允许“exact quote 一致且与 EvidenceRequirement 有确定性词项关联”的抽取式 Candidate；跨语言或相关性无法确定时保守返回 partial，不默认放行。网页导航、完整 Markdown 链接、last-updated 元数据、乱码、表格行、英文句子残片和无事实形态标题在 Candidate 阶段即被隔离。
- 不可绕过的 proof graph：生产 Worker 同时返回 documents、passages、candidate_claims 和 support_assessments。Supervisor、Red 和 Composer 都会验证 EvidenceClaim 的 passage IDs、assessment IDs、Candidate 文本、entailed verdict、confidence 与 Evidence ID lineage；篡改 Claim 文本或伪造 ID 不能进入覆盖计算或报告。
- 完成语义：Supervisor 不再按“存在任意 Claim”判定 resolved。每个 EvidenceRequirement 必须满足 verified Claim 数、独立来源数和一手来源条件；Core Question 只有在其全部 required Requirements supported 时才 resolved。Red fallback 也从宽泛问题改为针对具体未覆盖 Requirement 生成 requested evidence 和 suggested query。
- Composer/Citation：bounded Claim 选择先覆盖每个 required EvidenceRequirement，再加入第二条证据；Composer 只消费 verified Claims。Citation Audit 不再触发新的研究波次，V2 图固定为 `red_review -> drafting -> citation_audit -> persist_result`；Audit 只承担最终结构/语义保险和有界修复。
- 新指标与门：新增 Claim entailment pass rate、verified Claim yield、Evidence Requirement coverage、primary-source Requirement coverage、Composer Claim survival、Citation Audit removal rate。Canary 要求 Requirement coverage 与 primary-source coverage 均为 100%，且 Citation Audit removal rate <= 5%。
- 离线真实样本：新增 `scripts/analyze_saved_evidence_claims.py`，无需网络即可重放旧 checkpoint。`law_002 r5` 的 34 条旧 Evidence 生成 31 个 extractive Candidates，exact-quote violation 为 0，但保守 Requirement-aware fallback 仅放行 4 条；`med_004 r3` 的 42 条旧 Evidence 生成 30 个 Candidates，exact-quote violation 为 0，仅放行 8 条。这说明旧系统的 Evidence 数量显著高估了可证明结论数量，新覆盖语义会将其正确暴露为研究缺口。
- 测试：新增 law/medical gold Passage fixtures、exact quote、Verifier rejection、partial narrowing/reverification、tampered lineage、primary-source coverage 和 Requirement-first Composer tests。相关 V2/共享管线/Blackboard/CLI 指标回归 `129 passed`。首次最终全量为 `1011 passed, 3 skipped, 1 failed`；唯一失败仍是 150ms lease 的既有全量负载时序抖动，隔离复跑 `1 passed`。第二次最终全量为 `1013 passed, 3 skipped`、退出码 `0`。默认仍为 Legacy，尚未启动新的外部灰度。

#### 第一性原理外部灰度与状态修正

- `law_002 r6`：首次新链路真实运行，`partial / evidence_exhausted / fallback`、Judge `2.4`、20 Evidence、12 sources、22 tool calls、研究约 730 秒。proof graph 指标为 30 Candidate Assessments、2 entailed，entailment/yield `0.0667`，Requirement coverage 与 primary-source coverage 均为 0。诊断发现 Planner 未约束每个 Requirement 的最低 Claim/来源数，模型生成了 20 Claims、8 enforcement Claims、4 independent sources 等超预算门槛；该轮作为无效门槛配置样本保留。
- 门槛修复：`EvidenceRequirement` 合同硬限制 `minimum_verified_claims=1..3`、`minimum_independent_sources=1..2`；Planner 对模型值进行 clamp，并新增越界合同/Planner 回归 `51 passed`。
- `law_002 r7`：门槛修复后的有效研究运行，`partial / evidence_exhausted / fallback`、Judge `2.0`、31 Evidence、20 sources、31 tool calls、研究约 854 秒。entailment/yield `0.0968`，Requirement coverage 仍为 0。Red 找到少量真实 SCC/TikTok事实，但旧逻辑把 medium weak/non-comparable challenge 的目标 Claim 全部封锁，报告再次整体 abstain。
- Red 语义修复：`unsupported/conflict/weak_source` 继续 hard-block；high non-comparable/uncertainty hard-block；medium/low non-comparable/uncertainty 只禁止扩大比较，允许保留 verified narrow fact 并披露限制。
- `law_002 r8`：限定保留后的运行变为 `completed / coverage_complete / valid`，Judge `2.4`、37 Evidence、24 sources、28 tool calls、研究约 1226 秒。entailment/yield `0.3143`、Requirement/primary coverage 均为 `1.0`，但 material citation coverage 为 0，Citation Audit removal rate 达 `1.3`；最终仅保留两条二手引用。该轮进一步暴露两个状态模型错误，不能作为上线通过样本。
- Red 后 coverage 修正：所有 unresolved Red 目标 Claim 均不再贡献 Requirement coverage；hard-block Claim 从报告 proof graph 移除，soft-limit Claim 可保留窄事实但不能使 Requirement completed。Red 结束后重新计算 Requirement/Core Question coverage 和 termination reason，避免“报告内容被挑战但状态仍 completed”。
- Composer 不可改写：r8 的 11 个 verified Claims 被 Composer 改写为 15 个综合句，加入 Passage 未直接支持的新事实，Audit 因此删除 13 条。现已取消 Composer 的事实文本权限：模型只返回章节和 claim_ids；每个 ID 确定性展开为一个 immutable Verified Claim 原文，多 Claim 不再合并成新事实句。模型返回的 text 字段即使包含 URL/错误陈述也被忽略。
- 最终验证：Red coverage 重算、hard/soft constraint 和 immutable Composer 专项 `39 passed`；最终完整回归 `1021 passed, 3 skipped`，退出码 `0`。按预先约定未继续运行医疗题；三轮法律灰度已经证明共同瓶颈仍是一手来源检索与 Passage 质量，继续换领域不会验证新的架构假设。默认继续保持 Legacy。

#### 单一语义裁判、可用 partial 与最终 law r9

- 目标函数修正：Support Verifier 成为唯一 Claim 语义裁判。V2 Citation Audit 固定 `semantic=false`，只检查 Evidence ID、locator、Search snippet、裸 URL、WikiLink 和 marker coverage；不再用第二个模型重新推翻已验证 Claim。
- Red 职责收敛：只有 unresolved `conflict` hard-block Verified Claim。unsupported/weak-source/non-comparable/uncertainty 仍从 Requirement coverage 中排除目标 Claim，但窄事实可进入 partial 报告并披露限制；Red 不再重复裁决 Passage entailment。
- 信息量恢复：每个 Passage 最多提取2条原子 Candidate，每个 Worker 总计最多24条；partial support 仍只能 exact-scope 缩窄后重验。Composer 继续只选择 IDs 并输出 immutable Claim 原文。
- 有用性指标：新增 weighted Evidence Requirement coverage 和 verified Claims in report；Canary 除完整 coverage 外，还要求最终至少3条 Verified Claims。Citation Audit removal rate 继续要求 <=5%。
- 官方来源路由：Requirement 查询可自动推断 CAC/gov.cn、EUR-Lex/EDPB/EC、FDA、EMA、ClinicalTrials.gov 等 preferred domains，并将初始候选池提高到12。`law r9` artifact 证明 preferred domains 已正确传入，但 Tavily 首轮仍未召回官方站点，只返回维基文库与失效镜像。因此新增通用二阶段获取：首轮没有 preferred-domain 候选时，自动追加至多两个 `site:domain` 定向搜索并合并去重，不硬编码页面 URL。
- `law_002 r9`：`partial / evidence_exhausted / valid`，Judge `1.8`、19 Evidence、14 sources、23 tool calls、研究约607秒。material citation coverage `1.0`、invalid/raw URL/conflict/log leak 均为0，Citation removal `0`，Composer survival `1.0`；但 34 Candidate Assessments 仅2条 entailed（yield `0.0588`），Requirement/weighted/primary coverage均为0，最终报告只有2条与核心问题弱相关的 Claim。该轮证明多重删除问题已关闭，剩余主瓶颈确定为官方来源召回和 Passage/Requirement 相关性。
- 测试：单一裁判、多 Candidate、官方域名推断/定向重搜、有用性门专项 `39 passed`。最终全量为 `1025 passed, 3 skipped, 2 failed`，两项失败均为150ms/200ms超短 lease 在全量负载下过期；隔离重跑 `2 passed`。默认继续保持 Legacy；二阶段 site 搜索完成后未再消耗外部灰度。

#### 回滚严格 Proof Graph，恢复旧版 V2 生产路径

后续逐阶段执行计划见 [`RESEARCH_AGENT_V2_BASELINE_IMPROVEMENT_PLAN.md`](RESEARCH_AGENT_V2_BASELINE_IMPROVEMENT_PLAN.md)。

- 回滚范围：`run_research_worker()` 恢复 `EvidenceItem -> 轻量原子 Claim -> BlueWorkerResult`，不再调用 Support Verifier；Supervisor 恢复按 source-locatable Claim 的 question IDs 判断覆盖；supplemental merge 恢复同一语义；Red 恢复读取完整 Evidence package，结束后不再用实验 Requirement coverage 重写 Supervisor 状态。
- Planner 恢复只生成 Core Questions、report outline、source guidance 和 work hints；EvidenceRequirement 仍由合同自动生成一对一默认对象供实验工具使用，但模型不再生成证明槽位，生产 Supervisor/Worker 不读取其阈值。
- Composer 恢复综合文本能力：每个 Assertion 重新包含 `text + claim_ids`，允许基于多个已选 Claims 形成可读分析；继续保留结构化 ID、裸 URL/图片/HTML 拒绝、未知 ID inventory repair、Red 状态一致性和审计日志隔离。
- Citation Audit 恢复旧版语义审计，同时保留后续已经证明有效的 deterministic marker/locator、URL、WikiLink、表格安全和 issue 去重修复。旧版 V2 Canary gate 也恢复为 output status、budget、Core Question assignment、material citation coverage、invalid citation、Lead reserve 和 Judge，不再使用实验 Requirement/Verifier 指标硬阻断。
- 保留的改进：统一 acquire_evidence、HTML/PDF 全文抽取、Tavily/多后端回退、共享文档缓存、Browser 原始错误、dependency fail-fast、Docling 关闭/熔断、动态预算、Red 未决披露、官方域名推断，以及 preferred domain 未召回时的二阶段 `site:domain` 定向重搜。
- 实验代码保留但断开：SourceDocument/EvidencePassage/CandidateClaim/SupportAssessment、离线 replay、Verifier/narrowing 和 Requirement coverage 仍可由测试或实验函数显式调用，不接入 V2 生产图，便于以后在有标注集时校准而无需重写。
- 回归：回滚相关 Planner/Worker/Supervisor/Red/Composer/Citation/Workflow/共享比较测试全部通过；最终完整回归 `1041 passed, 3 skipped`，退出码 `0`。默认仍为 Legacy；回滚后尚未启动新的外部灰度。

#### 同质递归 Fork 与 Supervisor–Worker 的共享黑板公平对照

- 对照边界：固定使用 `fin_006`、同一 `ResearchPlan`（5 个 required Core Questions）、同一 DeepSeek 采样、同一 `acquire_evidence`/结构化抽取、同一动态 Evidence Selector、Claim hygiene、Composer 与 Citation Audit。两边均关闭 Red 和 supplemental wave，预算统一为 `1200s / 500,000 tokens / 96 tool calls`；唯一运行时差异是 Legacy 根 Agent自然选择 `fork_research`，或 Supervisor 确定性分配 WorkPacket。
- 共享黑板：新增独立 SQLite blackboard，记录不可变计划、assignment/lease、query fingerprint、canonical source、agent status、跨范围 Evidence signal 与 append-only events。Agent只研究 own scope；相同查询/来源复用；跨范围 Evidence 只发布 signal，不主动扩张。黑板是被动协调基础设施，不做规划、充分性裁决或动态重分配。最初与 LangGraph checkpoint 共库的 Supervisor r1 在 4 Worker 并发下触发 `database is locked`，已改为同目录独立 `checkpoint.blackboard.sqlite`；失败样本保留，r2/r3 未复现。
- 共享下游：两种架构的原始 Evidence 均完整保留；最终报告统一按 `max(12, required_count*4)`、上限 36，单问题 6、普通来源 2、官方一手来源 4 且 locator 不同进行选择，再统一生成 EvidenceClaim。Composer 与 Citation 只接收该有界 inventory；报告持久化保留各自 `architecture` frontmatter。V2 公平模式不再在 Worker 边界提前丢弃 Evidence。
- Legacy r2（自然 Fork 成功）：1 批 4 个 child，`thread_count=5`；黑板为 `18 queries / 30 sources / 20 source reuses`。结果 `partial / budget_forced / valid`，Judge `6.2`，103 Evidence、26 sources、22 Agent tool calls、389,313 tokens；研究 `974.25s`。4 个 child 均 partial/blocked，父 Agent随后本地补缺。该轮证明同质 Fork 有能力产出本组最高质量报告，但耗时、Token 和收尾压力较高。
- Legacy r3（自然未 Fork）：Fork 连续 offered 10 次但调用 0 次，`thread_count=1`；黑板 `8 queries / 17 sources`。结果 `partial / budget_forced / valid`，Judge `1.6`，35 Evidence、15 sources、11 tools、347,422 tokens；研究 `1186.766s`。同配置下 r2 Fork、r3 不 Fork，说明自然触发存在高方差；黑板只能协调已创建 Agent，不能解决根 Agent不 Fork 的串行墙钟问题。
- Supervisor r2/r3：两轮都确定性创建 4 Worker、覆盖全部 questions；Judge 均为 `4.4`。r3 严格有界结果为 `partial / coverage_complete / valid`，88 Evidence、24 sources、25 tools、301,245 tokens，研究 `463.75s`；黑板 `23 queries / 34 sources / 27 source reuses`。Supervisor 比 Legacy r3 快约 2.56 倍、Token 少约 13%，但报告仍混入网页元数据、缺少系统性对照，内容质量没有超过 Legacy Fork r2。
- 架构判断：同质递归 Fork 不是能力上不可行；当自然 Fork 发生时，本题 Judge 高于 Supervisor（6.2 vs 4.4）。其主要问题是 Fork 触发不稳定、父子预算/充分性状态均容易以 fallback/blocked 收尾、整体墙钟和结果方差较大。Supervisor 的优势是 assignment 稳定、并行墙钟短、Token 较低；当前弱点是 Worker 独立检索产生更多但更嘈杂的 Evidence，集中调度没有自动转化为更好的对比综合。单题两轮不足以宣称任一架构全面胜出。
- 结构指标：两边共同下游均达到 required assignment `1.0`、material citation coverage `1.0`、invalid citation `0`、裸 URL `0`；因此质量差异不再来自引用格式或 Composer/Citation 配置。真实瓶颈是 Core Question 与 Evidence 的语义相关性、官方条款命中率以及 Worker/child 的充分性判断。真实运行未产生 cross-scope signal，说明该机制可用但模型尚未主动采用。
- 验证：黑板、自然 Fork、Supervisor 共用合同、共享计划、动态 Selector、持久化和共享 Composer/Citation 的扩大回归 `185 passed`；进入真实对照前全量 `992 passed, 3 skipped`。最终有界 Citation 与子树指标合并后全量回归为 `997 passed, 3 skipped`，耗时 `146.21s`、退出码 0；默认配置未切换，所有公平配置独立位于 `configs/researchbench-shared-fin006-*.yaml`。

#### 同质 Fork 显式控制决策与可返还预算租约

- 三种原策略保持不变：`parallel`、`context_isolation`、`deep_tool_chain` 继续由现有 Fork Gate 校验。新增的 `fork / local_research / merge / complete` 只要求同一个 Agent 在每轮工具调用前明确控制选择和 rationale；它不强制 Fork，也不硬编码问题拆分。连续本地轮达到阈值时仅要求重新考虑 Fork，仍可说明理由后继续本地研究。
- 控制结构修复：真实模型会表达 Fork 意图但使用近似 reason（如 `parallel_work`），首次 r4 因严格枚举安全回退 local。现加入一次仅修 JSON/枚举结构的无工具调用，并明确禁止改变原 `fork`/`local` 语义；修复历史写入 control event。r4 作为无效灰度 checkpoint 保留。
- 预算资金池：黑板新增全局 pool 与持久化 lease。500k 总预算中保护 75k finalization 与 50k parent merge，剩余 375k 可租赁；Child 初始 60k，接近 assessment/finalization 阈值时按 25k top-up，单 Child 上限 125k，结束后原子归还未用额度。top-up 在 think 前和 assessment 前都执行，避免一次本地轮后直接因局部上限停止。默认配置仍关闭，灰度配置显式启用。
- 递归委托修复：r5 显示 Child 把自己 owned requirement 委托给 Grandchild 时，被黑板误判为同级抢占。现允许“当前 owner -> 直接子 Agent”的原子转交，同时继续拒绝 sibling 抢占；相关 assignment、恢复与递归测试通过。r5 作为无效样本保留。
- r6 有效灰度：Root 显式决策自然 Fork 4 个一级 Child，随后 3 个方向进入二级递归，总 `thread_count=8`。黑板记录 `5 fork_called / 4 accepted batches / 11 fork_not_called`，15 queries、32 canonical sources、11 source reuses。共授予 7 个 lease，7 个均释放；7 次 top-up 成功、6 次因 pool 已分配完拒绝。子树实际占用约 362k，池剩约 12.6k，125k 父/最终保护未被侵占。运行没有 assignment failure 或 SQLite lock。
- r6 结果：`outputs/evaluation/researchbench-shared-fin006-legacy-r6/ResearchBench_Evaluation_20260901_202624.json`；`partial / budget_forced / valid`，Judge `3.4`，99 Evidence、25 sources、19 Agent tool calls、398,955 tokens；研究 `887.719s`、总计 `957.062s`。结构指标保持 assignment `1.0`、material citation coverage `1.0`、invalid `0`；但 Evidence requirement coverage `0.6`，最终仍缺系统性的募集资金、披露与投资者保护比较。
- 架构判断：显式决策解决了“Fork 被普通工具淹没”的可观测性和触发问题；可返还 lease 解决了局部 60k 一刀切，Child 能使用全局 refill pool，且保护父合并/报告额度。新瓶颈是二级拆分粒度：当前黑板 assignment 以 requirement 为唯一 owner，而 Child 往往希望把同一 requirement 拆成多个互不相同的 clause/source subtask。Gate 只能接受其中一个 Grandchild，其余因同 requirement/child budget 被拒绝；更稳定 Fork 因而没有自动提升质量，r6 Judge 仍低于旧 r2 的 6.2。
- 下一步不是继续放宽预算。应在保留 requirement owner 的同时增加 `sub_assignment_id` 层，让一个 requirement 内的不同 clause/source 任务可并行、可见且不重复；或者在该能力完成前把灰度 `max_fork_depth` 暂时限制为 1。还需继续提高 Evidence entailment/requirement relevance，避免更多 Agent 只产生更多弱 Evidence。
- 验证：控制合同、三类 Fork reason、结构修复、预算 pool/top-up/release、父子转交、同级防抢占、恢复与既有递归回归均通过；最终全量为 `1044 passed, 3 skipped`，耗时 `180.19s`、退出码 0。默认 `configs/default.yaml` 未启用 `homogeneous_fork`，未切换默认架构。

#### 同质递归 Assignment Tree、全局线程兜底与递归侦察门

- 数据模型拆分：固定 Research Plan 的 requirement 只负责顶层覆盖与最终责任；递归执行改用独立 `assignment_id`，并记录 `parent_assignment_id`、多 requirement IDs、scope signature、objective、Fork reasons、owner、depth、status 与 lease。Root、Child、Grandchild 组成可恢复 assignment tree；一个孙 assignment 完成不会直接把整个 requirement 标成 supported。
- 同 requirement 多方向：一个 requirement 下允许多个 scope 不同的 sibling assignment，例如 investor protection 可分别派发 coupon/term adjustment、default/acceleration、investor remedies。相同 parent 下 scope signature 或 objective 高度重叠会拒绝；改名为 `v2/retry` 不能绕过去重。只有 assignment owner 可更新状态，只有父 assignment owner 可创建直接子节点，sibling 不能偷取任务。
- 全局黑板视图：所有同质 Agent 可见不可变全局计划、完整 assignment tree、兄弟 scope、recent queries/sources、Evidence lineage 与 requirement gaps。Query、source、Evidence、Agent 状态和事件均携带 assignment lineage；旧 requirement 级表/API 保留作增量迁移兼容，递归主链路只使用 assignment nodes。黑板继续独立于 checkpoint SQLite。
- 预算职责拆分：`explicit_control_decision` 与 `budget_leases_enabled` 已分开。本轮显式控制开启、token lease 关闭；原 pool/top-up/release 代码保留为 opt-in。旧的静态子树线程平均切分被黑板原子全局 `max_total_threads` 替代：每个 Child 可提出多个 Grandchild，只有整个 run 的 assignment 总数达到硬上限时拒绝，避免每个 Child 被预先锁成一个孙节点名额。
- Fork 语义保持：`parallel`、`context_isolation`、`deep_tool_chain` 仍为 OR，满足任意一项即可 Fork，没有增加第四种 reason。为稳定递归时机，depth>0 且存在研究工具的 Child 必须先完成至少一次本地 research tool call，再根据已观察到的 Evidence gap 决定是否递归；Root 首轮不受此限制。该侦察门是决策时机约束，不改变三种理由。
- 证据与完成语义：Grandchild Evidence 先归并到直接父 assignment，再由 Child 聚合进 requirement，最后 Root 汇总计划。只有 Root sufficiency assessment 可写 requirement coverage；assignment 的 completed/blocked/failed 与 requirement 的 unsupported/weak/supported/conflicted 分开。查询、来源和 Evidence lineage 在 SQLite 重启后保持。
- r7（Assignment Tree，仍受旧静态线程切分）：`partial / budget_forced / valid`，Judge `8.2`，133 Evidence、19 report sources、17 tools、444,867 tokens、10 threads，研究 `940.812s`；requirement coverage `0.8`、Claim pass `0.5833`、Citation coverage `0.8077`。相比 r6 Judge `3.4`、coverage `0.6` 明显提升，但 Child 的三个不同孙 scope 仍有两个被 `child budget exhausted` 拒绝。
- r8（只放宽为全局线程上限，无侦察门）：递归宽度跑通，但多个 Child 在本地研究前立即 Fork，早期泛化分支占满 10 threads。结果退化为 `partial / budget_forced / fallback`，Judge `1.4`，33 Evidence、7 sources、7 tools、418,032 tokens。该轮证明“能递归”不等于“应立即递归”，显式控制会从不 Fork 摆向过度 Fork。
- r9 首次尝试在 alignment 阶段返回非 JSON brief，33 秒失败且 0 tools/threads/Evidence，作为无效上游结构样本保留。全新 checkpoint 的 r9b 正常运行。
- r9b（全局线程上限 + 递归侦察门 + objective 去重）：Root 首轮 Fork 4 个一级方向；4 Child 先完成本地查询，再由披露和投资者保护分支按具体缺口派发 5 个 Grandchild。黑板共 10 assignments、7 fork calls、17 completed queries、24 completed/6 failed source fetches，未出现 requirement overlap 或 sibling theft。结果 `partial / budget_forced / valid`，Judge `8.8`，75 Evidence、24 completed sources、18 tools、434,480 tokens，研究 `809.781s`；material citation coverage `1.0`、invalid `0`、requirement coverage `0.8`、primary-source coverage `1.0`、Claim pass/yield `0.625`。这是同题当前最高 Judge，也比本次同模型 r7 少约 204 秒。
- r9b 的剩余问题：仍有 59 unresolved，Citation Audit removal `0.1667` 并触发一次 citation repair；5 个孙任务中 2 blocked、3 failed，主要缺口是 ICMA GBP/SLBP 正文和中国 SLB 官方规则的可定位条款。最终仍以 `budget_forced` 结束，说明下一阶段应减少达到全局线程上限后的无效 Fork 控制轮，并改善官方全文命中/子任务终止质量，而不是继续扩展递归宽度。
- 验证：同 requirement 不同 scope、duplicate/renamed retry、父子授权、sibling 防抢占、Requirement 不提前完成、query/source/Evidence lineage 恢复、全局原子线程上限、三 reason OR、max depth/thread、显式控制与 lease 关闭等专项通过。最终完整回归为 `1052 passed, 3 skipped`，退出码 0。`configs/default.yaml` 仍保持 Legacy 默认且未启用 homogeneous Fork；Supervisor–Worker 未套用递归 assignment 调度，公平对照边界未改变。

#### 同质 Agent 递归 Fork 完成版：父级语义、自主首轮 Fork、公平队列与 synthesis 分类

- 语义职责回归父 Agent：Root 只判断自己的 Child，Child 只判断自己的 Grandchild。运行时代码不再使用 objective/scope 的 0.92 词面相似度，也不阻断“与祖先措辞相似”的新 scope；唯一 assignment 重复条件是同 parent、同 requirement IDs、同规范化 objective 与 scope 的确定性 fingerprint。Query、canonical URL 与 Evidence ID 的确定性去重保持不变。Fork reason 仍严格为 `parallel | context_isolation | deep_tool_chain` 的 OR；`parallel` 至少两个独立候选，`deep_tool_chain` 至少三次预估工具调用。
- 删除递归侦察硬门：`recursive_fork_min_local_tool_calls` 仅作为旧配置/checkpoint 的兼容字段读取，值不再参与路由。每个 Agent 每轮都读取 Research Plan、own/sibling Assignment、已有 query/source/Evidence、coverage gap 与容量，自行选择 `fork / local_research / merge / complete`；提示词允许资料不足时先侦察，但不强制。确定性集成测试把该兼容字段设为 99，Child 仍能在第一个控制轮合法 Fork，且研究工具调用为 0。
- 单一容量合同：规范字段为 `max_concurrent_agents=10`、`max_total_agents=24`、`max_children_per_agent=5`、`max_fork_depth=2`。旧 `max_total_threads`、`max_children` 继续安全归一到规范字段，旧 checkpoint 缺少新属性时从旧值增量恢复；没有 initial/hard/dynamic 多套容量语义。累计上限包含 Root、运行中和 queued Assignment。
- 公平调度：新增同进程 `FairAgentScheduler`，Blackboard Assignment 状态增加 `queued`、`waiting_children`、`cancelled_due_to_budget`。通过 ownership/scope/total 校验的任务先持久化为 queued；父 Agent 等待子结果时转为 waiting 并释放活跃槽位，完成后以普通公平票据恢复 merge。队列优先轮转不同 parent Assignment，同 parent 按创建顺序；启动、等待、恢复、取消与 queue wait 全部写 append-only event。checkpoint 节点重放时，确定性 assignment ID 与 owner-resume 会重建 live queue，不丢持久状态。
- 安全边界：queued 不占活跃槽位；启动前若时间或分配 token 已越界，只取消从未启动的 Assignment，并向父结果写入 scoped unresolved。达到 depth、单父 5、累计 24 或 exact duplicate 才硬拒绝。终止性拒绝的 fingerprint 会记入状态，避免达到上限后反复提出同一 Fork。预算 lease 保持 opt-in，本轮和默认均关闭。
- Requirement 分类：`ResearchRequirement` 与 `CoreQuestion` 新增 `requires_external_evidence`。只有 research requirement 进入 Agent 的 Evidence coverage、补证动作、EvidenceRequirement 分母和 Claim coverage；synthesis requirement 进入 Composer 计划，不生成独立 EvidenceRequirement。Composer 必须生成对应 report-outline 结构，所有重要结论仍由 verified Claim IDs 渲染并接受 Citation Audit，缺结构或资料不足会进入 unresolved/partial。`fin_006` 第五项“形成结构化对照和结论”已标为 synthesis，外部 Evidence 分母从 5 项变为 4 项，没有放宽 Citation Audit 或伪造 coverage。
- 状态收尾：child unresolved 现在携带 requirement+scope，并按该键聚合去重；父子重复披露不会线性累积。`budget_forced` 仍只来自真实时间、token、工具或轮数硬边界；多策略无进展保持 `evidence_exhausted`。Root 最终综合 reserve 与可选预算 lease 代码未扩大。
- 验证：新增 exact/near fingerprint、10/24/5/2、并发溢出排队、父等待不占 slot、多 parent 轮转公平、预算边界取消、Blackboard reopen、Child 首轮 Fork、synthesis coverage/Composer 结构、fin_006 第五项分类和旧字段迁移测试。扩展定向回归 `158 passed`；第一次完整回归 `1062 passed, 3 skipped`，耗时 `138.46s`、退出码 0。最终 ID 兼容微调后的完整回归为 `1061 passed, 3 skipped, 1 failed`；唯一失败是既有 `100ms` Vault Writer lease 在全量负载下过期，隔离复验 `1 passed`，与本次 Research Agent 代码无关。LangSmith/LangChain tracing 全程关闭。默认仍为 `research.architecture=legacy`、`supervisor_v2.enabled=false`，未运行 Supervisor 对照、未推送。

#### `fin_006` 首次尝试（无效环境样本）

- 干净隔离目录：`outputs/evaluation/researchbench-shared-fin006-recursive-complete-r1`，结果为 `ResearchBench_Evaluation_20260902_024956.json`。只运行一次同题完整评测与 Judge，没有 Supervisor 对照。最终为 `partial / evidence_exhausted / fallback`，Judge `0.0`；研究 `183.969s`、总计 `231.625s`，18 Evidence、15 report sources、10 Agent tool calls、291,542 estimated tokens、21 aggregated unresolved。该轮没有触发 `budget_forced`。
- Fork/queue 结构：Root 首轮明确把四个 external research requirements Fork 为 4 个 Child，并明确把 synthesis 留给 Root；累计 Agent `5`，Fork tree 为 `Root -> 4 Child`，本次模型没有选择 Grandchild。accepted 4、rejected 0、exact duplicate rejection 0、预算取消 0。active 峰值 `4`、queued 峰值 `4`、waiting parent 峰值 `1`，4 个 queue wait 为约 15/21/27/32ms；Root 等待期间 active 从 1 降为 0，四个 Child 完成后才恢复 merge，证明 waiting 不占槽且无死锁。
- Requirement 分类观测：Blackboard 持久化 5 个计划项，但 root `requirement_coverage_updated` 只针对四个 research requirements；共享 EvidenceRequirement coverage 分母为 4。第五个 synthesis 项没有请求独立 Evidence，也没有造成虚假的预算终止。
- 失败归因：运行环境没有 Tavily API key，四个 Child 首轮均记录搜索后端不可用/熔断；4 个 acquisition 调用和后续 arXiv fallback 产生的 18 条 Evidence 全部与绿色债券/SLB 不相关，动态选择正确返回 `shared_selected_evidence_count=0`，Composer 因无 source-locatable verified Claim 安全 abstain，Citation 结果为 `citation_partial`。因此 fallback 来自外部来源不可用与 relevance gate 的正确隔离，不是 synthesis coverage、队列、并发上限或 `budget_forced`。运行中有一次既有消息截断造成的 DeepSeek 400，自动恢复后未改变上述根因。
- 判定修正：该工作树没有携带原始工作树中被 Git 忽略的 `.env`，执行时又未显式加载原始环境，因此本轮不构成有效内容验收；“外部来源不足”的结论撤回。控制/队列事件仍可作为离线结构观测，但不得用于评价最终实现质量。后续 r2 从原始工作树 `.env` 仅向评测进程加载密钥，使用全新 checkpoint/Vault 重新验证，不复制或记录密钥值。

- r2/r3 环境校准：直接加载完整原始 `.env` 时，Ark endpoint 曾在 alignment 前返回 `401`，两轮均为 `0 tools / 0 threads / 0 evidence`，不属于研究运行。该现象是当时的凭据响应，不代表模型族发生变化；本系列有效运行使用的模型均为 DeepSeek v4 Flash。

#### `fin_006` r4 有效受控递归验证

- 环境与结果：搜索凭据来自原始工作树 `.env`，Tavily 与 DeepSeek v4 Flash 均实际返回成功。结果保存在 `outputs/evaluation/researchbench-shared-fin006-recursive-complete-r4/ResearchBench_Evaluation_20260902_030654.json`，为 `partial / tool_failure / valid`，Judge `6.2`；研究 `168.015s`、总计 `216.953s`，78 Evidence、14 completed sources、17 Agent tool calls、296,975 estimated tokens、18 aggregated unresolved。根结果没有 `budget_forced`。
- Fork/queue：Root 首轮把四个 external research requirements 派给 4 个 Child，synthesis 保留给 Root；模型本轮仍未选择 Grandchild。累计 Agent `5`、accepted `4`、rejected `0`、budget cancellation `0`。active 峰值 `4`、queued 峰值 `4`、waiting parent 峰值 `1`、queue wait 峰值 `39ms`；等待/恢复与 r1 结构观测一致，无死锁或 slot 泄漏。
- 检索与引用：Blackboard 为 15 queries（14 completed/1 failed）、29 canonical sources、78 Evidence；共享 Selector 保留 7 条 Evidence。Evidence Requirement coverage/weighted coverage 均为 `0.5`，primary-source coverage `1.0`；material citation coverage `1.0`、invalid citation `0`、Citation removal `0`、Composer Claim survival `1.0`，Judge 从无效 r1 的 0 恢复到 `6.2`。
- 剩余状态问题：4 个 Child 均在各自真实 token hard limit 上以 `budget_forced / token_budget_exhausted` 返回 partial；Root 尚有最终综合 reserve，但其 sufficiency assessment 提出了已重复的 strategy family，结构修复仍违反合同，确定性 fallback 因而把根 termination 标成 `tool_failure`。这不是搜索凭据或队列故障，而是 assessment fallback taxonomy 的剩余校准点；内容上仍缺 ICMA GBP/SLBP 可定位全文、中国披露/验证条款及投资者救济边界。Composer 两次结构输出无效后使用确定性 Claim 报告，最终 Citation Audit 仍为 valid，但 synthesis 对应章节未完整形成，因此保持 partial 是正确的。

#### r5/r6 最小控制提示词校准与局部收尾

- r5 只加入“Requirement 可继续拆分、single requirement 不能作为不 Fork 理由”的强提示，结果从 r4 的不递归摆向过度递归：23 Agents、13 次 Fork、6 个 queued budget cancellation、24 Evidence、8 report sources、6 tools、415,252 tokens、Claim yield `0.1667`、coverage `0.5`，Judge `4.8`。该轮证明方向正确但措辞缺少批次、具体目标和容量刹车，不能保留。
- 最终平衡提示词要求候选必须绑定具名条款/jurisdiction/primary document/tool chain，不得机械拆 objective；depth 1 通常 2 个、最多 3 个候选；一批派发后优先 merge；只有 children 完成后出现新的非重叠高价值 gap 才再次 Fork。控制 payload 新增 `remaining_total_agent_slots` 和 `delegable_token_budget`，但不新增硬 Validator、不恢复首次工具门。
- r6 研究侧结构收敛为 Root + 4 Child + 2 Grandchild，共 7 Agents；0 rejection、0 cancellation，active/queued/waiting 峰值为 `5/4/2`。得到 99 Evidence、22 completed sources、19 tools、365,042 tokens、研究 `262.984s`；11 verified Claims、Claim yield `0.4583`、Evidence Requirement coverage `0.75`、primary coverage `1.0`、material citation coverage `1.0`、invalid citation `0`。相比 r4 的 78 Evidence、14 sources、7 Claims、yield `0.2917`、coverage `0.5`，关键质量漏斗明显恢复。Judge 当时因同一 DeepSeek v4 Flash 服务返回 `402` 而失败；这是账户状态，不是模型环境不同，随后由 r7 在相同模型下完成有效复测。
- `reconsider_after_local_rounds` 保持 `2`，不需要改成 `1`。局部收尾同时完成：deterministic Composer fallback 按 `report_outline`/CoreQuestion 分章节，并为缺失 research/synthesis 章节写 gap disclosure；当 Root 已保留 Evidence、无未尝试动作且 incomplete Child 均到真实 budget/evidence 边界时，assessment repair 失败归为 `evidence_exhausted`，不再误标 `tool_failure`。Citation Audit、Selector、预算和默认架构均未改动。
- 验证：平衡控制、容量 payload、同 requirement 多 scope、Composer plan-shaped fallback 与 termination taxonomy 定向回归 `77 passed`；最终完整回归 `1065 passed, 3 skipped`，耗时 `135.56s`、退出码 0。LangSmith/LangChain tracing 关闭，未运行 Supervisor 对照，未推送。

#### r7 原始 `.env`、DeepSeek v4 Flash 有效复测

- 运行方式：以 `override=True` 完整加载 `D:\Claude\deepresearch-agent\.env`，关闭 LangSmith/LangChain tracing，不读取系统模型配置作为 fallback；模型及 Judge 请求均命中该 `.env` 指定的火山方舟 DeepSeek v4 Flash endpoint 并返回 HTTP 200。结果为 `outputs/evaluation/researchbench-shared-fin006-recursive-complete-r7/ResearchBench_Evaluation_20260902_145438.json`。
- 最终结果：`partial / evidence_exhausted / valid`，Judge `5.4`，规则综合分 `0.6833`；研究 `1,014.218s`、总计 `1,080.359s`，60 Evidence、15 completed sources、12 Agent tool calls、406,542 estimated tokens、21 unresolved。Judge 分项 factual accuracy `8`、logical consistency `7`、citation quality `5`、comprehensiveness `3`。
- 递归/队列：本轮随机拓扑为 Root + 2 Child + 4 Grandchild，共 7 Agents；0 rejection、0 budget cancellation，active/queued/waiting 峰值 `4/2/3`。说明平衡提示词能产生递归且没有调度阻断，但未稳定复现 r9b 的 Root + 4 Child + 5 Grandchild。
- 来源获取已经不是主因：8 queries 得到 19 canonical sources，其中 15 completed、4 failed，正文打开成功率 `78.9%`，接近 r9b 的 `80%`。但 acquisition 只有 8 次、Agent tools 只有 12 次，明显少于 r9b 的 17 queries/18 tools；同时消耗 406,542 tokens 和 1,014 秒，表明预算更多花在控制与等待而非有效检索。
- 当前主瓶颈是 Evidence→Claim→Report：60 Evidence 只选出 7 条，Claim entailment/yield `0.3043`、requirement coverage `0.5`，Citation Audit removal `0.1429`；最终报告仅 1,885 字符、4 个引用。Vault 中已经存在 GBP 四核心组件、SLB 一般公司用途与 KPI/SPT/票息联动、SLBP 五组件及 coupon step-up 风险等直接证据，但没有进入最终报告。Composer 已按要求章节成文，material citation coverage `1.0`、invalid `0`，所以这次降分不是模型环境、来源打开、队列或引用格式，而是关键 Evidence 没有进入 verified Claim inventory。
- 对比判定：同为 DeepSeek v4 Flash，r7 的 5.4 仍显著低于 r9b 的 8.8；最小 Fork 修复解决了 r4 不递归和 r5 过度递归，却未恢复后半段选择质量。当前不继续改 Fork、Scheduler、预算或 Citation Audit；若开展下一轮，只应局部诊断/修正 Evidence selector 与 Claim verifier 对每个 external requirement 的关键直接证据保留。

#### r9b 行为基线恢复与 Evidence/Claim 收敛

- 恢复的是 r9b 行为约束，不回退当前基础设施。Root 仍由同质 Agent 显式作控制决策及结构修复，但初始 coverage wave 会通用规范化为“一项 required external-evidence Requirement 对应一个一级 Child”；synthesis 不下发。Child 的 `parallel/context_isolation` 递归 Fork 必须已有本 Assignment 的 query/source/Evidence 信号，明确的三步 `deep_tool_chain` 仍可首轮 Fork。公平 Scheduler、Blackboard Assignment Tree、exact fingerprint、10/24/5/2 规范字段、synthesis 分类、Composer/Citation 和 termination taxonomy 均保留。
- `fin_006` 比较配置将 `max_total_agents` 收敛到 r9b 的 `10`，不修改默认配置的 `24`。r8/r9 两轮均完整加载原始工作树 `.env`，模型和 Judge 均为 DeepSeek v4 Flash；Root 两次都稳定产生 4 个单 Requirement Child，不再复现 r7 的两项捆绑。r8 进一步形成 5 个 Grandchild，完整拓扑为 Root + 4 Child + 5 Grandchild。
- r8 有效质量恢复：`outputs/evaluation/researchbench-shared-fin006-r9b-behavior-r8/ResearchBench_Evaluation_20260902_175731.json` 为 `partial / budget_forced / valid`，Judge `8.6`；54 Evidence、12 completed sources、9 queries、12 Agent tools、409,284 tokens，研究 `787.812s`。Claim pass/yield `0.65`，高于 r9b 的 `0.625`；material citation coverage `1.0`、invalid `0`。Judge 分项 factual/logical/citation/comprehensiveness 为 `9/9/9/8`。这证明主要质量退化确实来自初始 Requirement 合并和后续 Claim 漏失，而不是 Scheduler 或当前模块化架构。
- r8 随后暴露共享 verifier 的安全缺陷：一次提交 20 个 Candidate 时，模型只返回前 8 个 assessment；旧代码把遗漏的 12 个 Candidate 自动落到 deterministic fallback，其中包含与 Requirement 无关的背景 Claim。该轮内部 coverage 因此仍为 `0.75`，尽管最终报告得到 8.6。不能把这个不完整响应行为作为最终实现保留。
- r9 对“不完整 verifier 响应”先采用每批 6 条、遗漏 fail-closed、partial 二次验证和 uncovered 回填。结果 `outputs/evaluation/researchbench-shared-fin006-r9b-behavior-r9/ResearchBench_Evaluation_20260902_182710.json` 为 `completed / evidence_exhausted / valid`，内部 Evidence Requirement coverage `1.0`、Claim yield `0.5833`，但 Judge 降至 `6.8`；134 Evidence、22 completed sources、15 queries、16 tools，研究达到 `1,200.922s`。Citation Audit removal `0.4167`，报告仍遗漏 SLB coupon/KPI 联动，并被 2025/2026 ICMA 新闻类材料稀释。该过细验证方案因延迟与最终质量均不合格而拒绝。
- 最终收敛实现：semantic verifier 每批最多 8 条；有效但遗漏 Candidate 的响应 fail-closed，不再自动判定通过；完全不可解析时仍保留确定性 exact-quote fallback。共享路径不再对已经是精确摘录的 partial Claim 做第二轮改写验证。Evidence 先通过“能否抽取 reportable Candidate”门，再按 Requirement、来源和 authority 选择；每个 Requirement 最多由 3 个不同 primary sources 占位，同一 primary 的不同 locator 仍可保留，剩余名额留给独立来源。若某 Requirement 经语义验证后仍为空，只从其未选 Evidence 做一次最多 6 条的有界回填。候选提取补充规则/因果事实信号，避免只选择市场规模和导航背景。
- 验证：新增 Root 四 Requirement 独立 ownership、Child evidence-aware/deep-chain 首轮 Fork、跨语言 exact Claim、semantic incomplete fail-closed、unclaimable authority page、semantic backfill、primary-tier diversity 等回归。最终定向回归 `56 passed`；最终完整回归 `1075 passed, 3 skipped`，耗时 `142.15s`、退出码 0。对 r9 已保存 Evidence 的无网络重放保持四项 requirement 均可形成 Claim，并让 IFC 等非新闻正文进入有界候选。为避免再次陷入单样本调参循环，最终收敛版未继续发起第三次在线 Judge；当前已验证在线质量高点仍为 r8 的 8.6。

#### 恢复 r8 并进行两道科技领域泛化测试

- 按要求恢复产生 8.6 分的 r8 代码状态：保留 Root Requirement ownership 规范化、evidence-aware Child Fork、claimable Evidence 预过滤、单次 semantic verifier 与 deterministic fallback；撤销 r9 之后的 8 条分批、incomplete fail-closed、uncovered backfill、primary-tier cap 和额外事实信号。恢复后定向回归 `71 passed`，最终完整回归 `1072 passed, 3 skipped`，耗时 `130.02s`。
- 新增 `configs/researchbench-r8-tech.yaml`。该配置复用 r8 的 Legacy 同质递归 Agent、structured extraction、10 Agent 总上限、500k token 和 1,200 秒预算，但关闭 `fin_006` 专用 fixed plan，避免用绿色债券提纲研究科技题。模型与 Judge 继续完整加载原始 `.env` 中的 DeepSeek v4 Flash。
- `tech_001`（2024 主流 LLM 对比）：Judge `4.6`，`partial / budget_forced / valid`；190 Evidence、32 completed sources、10 threads、16 tools、374,100 tokens、研究 `528.922s`。Judge 分项 factual/logical/citation/comprehensiveness 为 `7/6/5/2`。报告缺少四模型可比 benchmark 分数和完整技术路线分析，并混入 Reactor Mk.1、GPT-5.6 菜单、GPT-4 非 GPT-4o 等无关或错代内容。
- `tech_002`（AI 芯片市场）：Judge `5.4`，`partial / budget_forced / valid`；286 Evidence、33 completed sources、6 threads、17 tools、426,521 tokens、研究 `674.859s`。Judge分项为 `8/7/7/2`。报告主要覆盖 NVIDIA/AMD，Intel、昇腾、寒武纪和 oneAPI/CANN/寒武纪软件栈基本缺失，且 B200 内存规格出现 192GB/180GB 冲突。
- 两题结果保存在 `outputs/evaluation/researchbench-r8-tech-two/ResearchBench_Evaluation_20260902_192104.json`，平均 Judge `5.0`。总计 476 Evidence、65 completed sources、33 tools、800,621 tokens。结论是 r8 的 8.6 依赖 `fin_006` fixed plan 与共享结构化 Composer；在动态科技 ResearchBrief + 通用 Legacy 汇总路径上，大量 Evidence 没有转化为覆盖完整的比较结论。r8 因此不是已经验证的跨领域通用高质量版本。

#### 动态计划接入 r8 结构化研究后半程

- 实现边界：继续使用已有 `plan_research()`，不新增 Planner、Plan Validator、第二规划器、coverage wave 或调度规则。Planner 直接从确认后的 ResearchBrief 动态生成 4–5 个 required Core Questions，并在提示中要求合并属于同一证据链的方向、把 synthesis 留给 Root/Composer；模型输出规范化后最多保留 5 项。fixed plan 与 ResearchBench `ground_truth`/Judge rubric 均不进入动态规划。
- 流程解耦：新增独立 `research.structured_report.enabled` 开关，把结构化后半程从 `shared_comparison.fixed_plan` 解耦。开启后，动态 Legacy 流程为 `prepare_research -> planning -> research_agent -> normalize_shared_research -> drafting -> citation_audit -> persist_result -> postprocess_report`，复用 r8 的 Evidence Selector、Claim、Composer 与 Citation Audit；`shared_comparison` 仍只表示真正的 fixed-plan 对照。默认配置保持该开关关闭，原 Legacy 行为不变；科技 canary 配置显式开启。
- 验证：新增动态计划最多 5 项、动态 Legacy 结构化报告路径、配置解析与默认兼容测试。受影响模块回归 `54 passed`；最终完整回归 `1083 passed, 3 skipped`，耗时 `152.18s`、退出码 0。
- 有效复测环境：完整加载 `D:\Claude\deepresearch-agent\.env` 且 `override=True`，模型与 Judge 请求均命中该环境指定的火山方舟 DeepSeek v4 Flash endpoint 并返回 HTTP 200；输出与 checkpoint 使用全新目录 `outputs/evaluation/researchbench-dynamic-structured-tech-two-r1`。两题均现场生成计划，没有读取 fixed plan、ground truth 或 Judge rubric。
- `tech_001` 动态生成 4 个 Core Questions，assignment rate `1.0`。结果为 `completed / budget_forced / valid`，Judge `4.2`；254 Evidence、32 completed sources、10 threads、15 tools、401,255 tokens、研究 `1,168.531s`。Selector 仅保留 13 条 Evidence，最终报告 11 个 verified Claims、5,357 字符，Composer Claim survival `0.4583`。报告缺失四模型量化 benchmark、长上下文和 Gemini 1.5 技术路线。
- `tech_002` 动态生成 5 个 Core Questions，assignment rate `1.0`。结果为 `completed / budget_forced / valid`，Judge `6.2`；186 Evidence、30 completed sources、6 threads、15 tools、420,747 tokens、研究 `945.437s`。Selector 同样只保留 13 条 Evidence，最终报告 12 个 verified Claims、5,364 字符，Composer Claim survival `0.6`。报告缺失华为昇腾、H100/H200 训练指标、MI300 追赶地位、市场份额和关键供应链事实。
- 两题最终结果为 `outputs/evaluation/researchbench-dynamic-structured-tech-two-r1/ResearchBench_Evaluation_20260902_205426.json`，平均 Judge `5.2`，相对旧动态 Legacy 的 `5.0` 只提升 `0.2`，没有恢复 fixed-plan r8 的 `8.6`。两题合计 440 Evidence、62 completed sources、30 tools、822,002 tokens，均为 `budget_forced`；Verifier 与部分 Research Agent 输入超过 35k 字符并被客户端截断，Composer 输入本身约 21k、没有触发该截断。
- 定位结论：动态规划不是主要失败点。两题分别形成 4/5 个覆盖题意的主问题，所有 Core Questions 均进入 Blackboard；Blackboard 也把大量 Evidence 关联到每个 requirement。主要损失发生在 `Evidence -> Selector/Claim -> Composer`：440 条 Evidence 最终每题只选 13 条，报告只留下 11/12 个 Claims，并出现“官方规格已在 Blackboard、最终却选择论坛/Wikipedia/二手硬件页”的证据降级。其次，递归研究消耗接近完整时间预算，造成长上下文截断和 budget-forced 收束。下一步应先对照 r8 fixed-plan run 的 selected Evidence/Claim inventory，检查 selector 的 requirement relevance、authority 排序和 Composer 输入上限；在完成该差异审计前，不再修改 Planner、Scheduler、Fork、预算或 Citation Audit。

#### Agent 自主 Evidence 排序与动态报告容量

- 职责调整：复用现有 `RequirementCoverage.evidence_ids` 作为 Child/Root 的有序 Evidence shortlist，不新增第二个选择模型或固定领域规则。充分性提示要求 Agent 只列会用于最终报告的直接证据，按强度与权威性排序，并排除错实体、错版本/日期、导航和普通背景页；父子 coverage 合并保持该顺序，不再通过 `set + sorted` 打乱。
- Selector 收敛为装载器：共享路径把 Legacy Root 的 coverage 传入 Selector。Agent 已判断过的 Requirement 只允许 shortlist 内的 Evidence；空 shortlist 保持为空，不再由静态域名评分自动回填。Selector 仍负责 requirement 轮转、来源/locator 去重和上下文容量。未经过 Agent 判断的 Supervisor/实验路径继续使用通用 relevance/authority 排序。
- Claim 路径：Candidate 改为先让每条 Agent-ranked Evidence 产生第一条原子 Claim，再在容量允许时取第二条，避免首个长页面占满配额。动态上限提高到最多 40 个 Candidate，但所有入选内容都保留原始 Evidence/Passage lineage。Agent-ranked Evidence 不再重复做语义优先级裁判；程序只确定性验证 Candidate 确实是对应 Passage 的精确摘录。未经过 Agent 判断的 Candidate 仍使用 Semantic Verifier。
- Verifier 修正：请求 payload 对 Requirement/Passage 去重并按 28k 字符动态分组，保留所有 Candidate，不再截断。遗漏或无效响应默认不可进入报告；删除“中英文不同即视为相关”的 fallback。真实诊断进一步发现 DeepSeek 返回 `confidence="high|medium"`，旧解析器只接受 float，导致完整 batch 被误判为 ValueError；现已明确提示 0–1 数字并兼容 high/medium/low。Verifier 语义也改为“Passage 完整支持原子 Claim 且 Claim 对 Requirement 有实质贡献即可 entailed”，不要求单条 Claim 独自回答整个宽泛 Requirement。4 条保存样本在线复验得到 `entailed / irrelevant / entailed / irrelevant`，全部为 semantic_verifier。
- Composer 容量：取消固定 12 条。按 Requirement 轮转和字符预算装载，数量上限 40、Claim 上下文预算 18k；精简不需要模型读取的 Evidence/Passage/Assessment IDs 与 source guidance。`tech_002 r3` 保存 Evidence 的本地重放从旧版 12 条恢复到 31 条 Composer Claims，五个方向各 6–7 条，Composer 总输入 28,957 字符，低于 35k。
- 状态语义：结构化 Citation/Composer 成功不再把原始 `partial`、`budget_forced`、`evidence_exhausted`、`tool_failure` 或 `user_cancelled` 升级为 completed；保留 Legacy stop reason 和 termination reason。
- 在线 canary：`researchbench-agent-ranked-tech-two-r2` 的 `tech_001` 在 Alignment 前因服务重试后未返回合法 Brief，属于无效环境样本；`tech_002` 因上述 confidence 合同尚未修正而 31 个 Candidate 全部 fail-closed，Judge 0，不作为最终质量样本。修正后的 `researchbench-agent-ranked-tech-r3` 完成研究与 drafting 后在 Citation Audit 外部调用长时间不返回，超过配置墙钟十余分钟且 checkpoint 停止更新，因此主动终止、没有 Judge 分数。其最后有效状态为 `partial / time_budget_exhausted`，202 Evidence、5 Child、359,256 tokens；Root 五项 shortlist 分别为 15/10/6/8/11，结构化归并得到 27 个 claim-bearing Evidence、40 Claims，草稿使用 31 Claims/8 sections。该轮证明 Agent priority 与容量链路生效，也暴露现有外部模型调用没有被全局 1200 秒墙钟严格包住。
- 验证：Agent shortlist 不回填、空 shortlist、父子顺序、来源公平 Candidate、动态 Composer、Verifier 大输入不截断、confidence label、缺失 assessment 不放行、Agent-ranked exact lineage 和 Legacy partial 状态均有回归。最终完整测试 `1093 passed, 3 skipped`，耗时 `145.00s`、退出码 0。Planner、Scheduler、Fork、Judge 与默认 Legacy 开关未改动。
- 后续从 r3 checkpoint 恢复完成正式评分。`tech_002` 结果为 `partial / budget_forced / valid`，Judge `4.0`（factual/logical/citation/comprehensiveness=`6/4/5/2`）；202 Evidence、27 claim-bearing selected Evidence、31 report Claims、Composer survival `0.775`、Citation Audit removal `0.2581`、最终 5,795 字符。`tech_001` 全新运行结果为 `partial / budget_forced / valid`，Judge `1.6`（`2/2/2/1`）；158 Evidence 但 Root shortlist 最终只有 4 条 claim-bearing Evidence、报告 8 Claims/2,855 字符。两题平均 Judge `2.8`，显著低于同题上一版动态结构化流程的 `5.2`。判定：让 Agent 负责优先级的方向正确，但把空/过短 shortlist 当作绝对排除并完全禁止后处理补充过于激进；Root coverage 输出不是可靠的最终证据排序接口。tech_002 同时仍缺 Intel/寒武纪与完整训练-推理横向比较，tech_001 则因 shortlist 过度收缩几乎失去主要内容。当前版本不应作为质量通过版本。

#### Child 方向小报告与 Root 直接综合

- 最终职责对齐：Blackboard 只保留 Research Plan、Assignment、Agent 状态、Coverage gap、Query/Source fingerprint 和 Fork/等待事件，用于了解各 Agent 的研究方向、避免重复研究与识别缺漏；动态生产运行停止调用 `register_evidence()`，不再把 Evidence 写入 Blackboard。完整 Evidence 仍属于各 Agent 的 ResearchResult 与最终 Vault，不参与协调决策。
- 同质 Agent 输出：`ResearchResult` 新增 `research_memo` 与 `report_markdown`。Child 自主搜索、判断材料并在 `research_memo` 中提交只覆盖自己 Assignment 的简短 Markdown 方向报告，使用真实 `[[EVIDENCE:id]]`；Root 的控制与充分性判断读取 Child memo，发现重要 Coverage gap 时继续派发 Child，最终由 Root 自己在 `report_markdown` 中完成跨方向综合。父收到的 fork tool 结果改为 memo/summary/gap，不再序列化完整 Evidence inventory。
- 动态生产图改为 `prepare_research -> planning -> research_agent -> persist_result -> postprocess_report`。Root 报告通过现有 Evidence marker renderer 做已知 ID、引用编号和 References 的基础确定性渲染；不经过 shared Evidence Selector、Candidate/Claim pipeline、Semantic Verifier、独立 Composer 或语义 Citation Audit。旧模块仍保留但只服务 fixed-plan/Supervisor 实验，未进行大范围删除。
- 上下文：Root 最终输入优先 Child memo 及其引用的 Evidence，并用字符预算装载所有 Child 摘要；这是模型输入边界，不做内容语义选择。Coverage evidence IDs 恢复为充分性判断依据，不再充当 final-report whitelist。
- 测试：新增动态 Root report 路径、Root 直接 Markdown 持久化、无 Selector/Composer/Audit 调用、Blackboard Evidence count=0、Child memo 和 Root fallback report 等断言。定向扩大回归 `183 passed`，最终完整回归 `1093 passed, 3 skipped`，耗时 `150.76s`、退出码 0。
- 两道科技题有效 canary 保存在 `outputs/evaluation/researchbench-root-agent-tech-two-r4/ResearchBench_Evaluation_20260903_013630.json`。`tech_001` 为 `partial / budget_forced / repaired`，Judge `7.6`（factual/logical/citation/comprehensiveness=`9/9/8/6`）；314 Evidence、37 sources、9 Agents、16 tools、438,350 tokens、报告 5,155 字符。`tech_002` 为 `partial / budget_forced / repaired`，Judge `6.8`（`6.5/8/6/6.5`）；249 Evidence、31 sources、6 Agents、18 tools、436,983 tokens、报告 5,879 字符。两题平均 Judge `7.2`，显著高于同题 Agent-whitelist 版的 `2.8`，也高于此前动态结构化后处理版的 `5.2`。
- 行为核验：两题 `root_agent_report=true`，Blackboard `evidence_lineage_count=0`；报告由 Root 直接生成，没有 shared-structure/Claim 指标。该轮 Root 最终输入分别出现约 36.7k 与 47.3k 字符，超过当时模型层 35k 上限后被截断；对应 Judge 完整性为 6/6.5。随后只针对这个单点把输入上限变为可配置项，并将默认动态研究与科技题配置设为 55k；47.3k 回归样本不再触发截断，旧适配器默认值仍为 35k，未改变历史实验配置。尚未重新在线评分；tech_001 的直接横向 benchmark 缺口、tech_002 的少量事实/单位问题与一手来源不足仍需由下一轮结果判断。
- Root 完整输出修正：真实复测发现 55k 输入仍会在 55,274/55,226 字符处轻微裁剪，且把整篇 Markdown 放入 JSON 会在 4,096 token 输出边界形成未闭合字符串，触发结构修复并把 328/201 条 Evidence 压缩成 2,429/3,266 字符的短报告。现将动态研究输入上限设为 60k；Child 继续使用 4,096 输出 token，Root 最终调用单独使用 32,768 token、累计输出预算 50k，并直接返回 Markdown。适配器保留服务端 `finish_reason`；若为 `length`，Root 从报告尾部续写并拼接，不重写全文。使用温度 0.3 运行的同一 checkpoint 只重放 Root 后，tech_001 Judge 从 3.4 升至 8.2（9,505 字符、16 个引用来源），tech_002 从 6.0 升至 6.2（10,560 字符、13 个引用来源），平均 7.2；两题均 `output_status=valid`、一次自然结束、无需续写。受影响专项回归 69 条通过；全量为 1,095 passed、3 skipped，另有一个与本改动无关的 60ms Vault lease 心跳时序用例首次抖动失败，单独重跑通过。

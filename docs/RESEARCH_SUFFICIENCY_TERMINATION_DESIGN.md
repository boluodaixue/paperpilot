# Research Agent 研究充分性与终止机制

## 1. 文档状态

- 状态：已实施并完成确定性回归、相同三题真实研究以及完整 LLM Judge 验收；真实结果显示语义收敛仍有后续优化空间；
- 目标：替换提交 `bdf310a` 中基于来源数量和连续轮次的轻量完成门；
- 实施边界：继续使用同质 Research AgentGraph，不新增 Supervisor、Judge Agent、服务或第二条研究主链；
- 结论：来源数量、循环次数和单一总分都不能直接证明研究完成。

`bdf310a` 当前实现的以下规则已被否决，不得继续沿用：

- 根 Agent 4 个、子 Agent 3 个来源的完成底线；
- 研究方向数量乘二、最多 12 个来源的完成底线；
- 连续两轮达到来源底线后禁止工具并强制收尾；
- 全局连续两轮没有新增来源或证据就强制 `partial`；
- 任一子任务 `partial` 就无条件把根任务降为 `partial`；
- 最终输出格式错误就等同于研究本身未完成。

## 2. 核心原则

Research Agent 只在以下条件同时成立时继续研究：

1. 仍然存在会显著影响最终答案的重要信息缺口；
2. 存在有较高概率缩小该缺口的下一步研究动作；
3. 时间、token、工具、线程、递归和用户控制等资源边界仍然允许。

当前路径无效但存在其他高价值路径时重新规划；其余情况根据真实原因停止研究。

```text
重要缺口还存在吗？
        ↓ 是
继续研究仍有价值吗？
        ↓ 是
资源仍然允许吗？
        ↓ 是
      Continue
```

这三个问题中的任何一个为“否”，都不能机械继续；但必须区分完成、边际收益饱和、重要证据穷尽、资源强制和执行失败。

## 3. 四层判断

### 3.1 目标完成度

用户确认后的 Research Brief 是完成契约的来源。Brief 被拆成稳定的必要要求，每项至少包含：

```yaml
requirement_id: R1
description: 需要回答的核心问题或关键子问题
required: true
status: unsupported | weak | supported | conflicted
evidence_ids: []
remaining_gap: null
```

判断重点：

- 核心问题是否有明确答案；
- 关键子问题是否都被处理；
- 是否存在会改变最终结论的关键缺口；
- 剩余内容是关键缺口还是低价值细节。

完成不意味着查完所有可能细节，而是没有尚未处理、且足以改变最终答案的必要问题。

### 3.2 证据充分度

每个关键结论必须映射到本次执行中真实存在的 `EvidenceItem.evidence_id`。评估至少考虑：

- 证据是否真实存在且具有可定位的来源；
- 来源是否适合支持该类结论；
- 高影响结论是否只有一个弱来源；
- 多个来源是否真正独立，而不是同一材料的转述；
- 来源之间是否存在尚未解释的冲突；
- 结论语气与证据强度是否匹配。

不设置全局固定来源数。一个权威原始来源可能足够支持简单事实；高风险、争议性或比较性结论可能需要多个独立来源。

程序可以确定性验证 Evidence ID、来源定位和覆盖结构是否存在；语义相关性、可靠性和结论强度仍需要模型作出结构化判断，并由后续评测检验，不能伪装成完全确定性的事实验证。

### 3.3 继续研究的价值

Agent 不得只声明“还能继续搜索”。每次 `Continue` 或 `Replan` 都必须给出：

```yaml
requirement_id: R2
critical_gap: 缺少会影响最终比较结论的原始数据
next_action: 查找央行、BIS 或 IMF 的原始统计表
expected_value: high | medium | low
expected_improvement: 说明该动作可能改变或增强哪项结论
strategy: official_database | primary_document | query_rewrite | paper_search | other
```

`expected_value` 使用可解释的等级，不假装计算精确概率。判断依据包括：

- 下一步是否针对具体的重要缺口；
- 是否与此前失败策略实质不同；
- 是否可能带来新的可定位证据；
- 是否可能降低关键不确定性或解决冲突；
- 预期改善是否值得额外成本。

全局“本轮没有新增来源”只能作为观察信号，不能直接触发停止。只有针对同一重要缺口已经尝试多种合理且不同的策略，仍无进展并且不存在新的可行动方案，才可认定证据路径穷尽。

### 3.4 资源与安全边界

以下限制仅是硬保险丝，不负责证明研究充分：

- 最大研究轮数；
- 最大墙钟时间；
- token 或成本预算；
- 单 Agent 与全树工具调用预算；
- 最大线程数、子任务数和 fork 深度；
- 工具连续失败和重试预算；
- 用户主动取消。

触发硬限制时必须保留未解决要求和明确 `termination_reason`，不得报告为正常完成。最终综合应预留独立 token 和至少一次模型调用预算。

## 4. 三种运行决策

研究充分性评估只产生三类动作：

### Continue

存在重要缺口，当前路径仍有明确、高价值的下一步动作，并且资源允许。下一轮只处理未充分覆盖的要求，避免重复搜索已经支持的方向。

### Replan

存在重要缺口，但当前研究路径的边际价值已经很低；同时存在实质不同、仍有较高价值的替代路径。Replan 可以调整查询、来源类型和子任务拆分，但不得静默改变用户确认的研究目标。需要扩大目标范围时必须重新请求用户确认。

### Stop Research

研究不再满足继续条件，进入最终综合。停止原因必须是第 5 节中的一种，不能只记录模糊的 `normal completion`。

## 5. 停止原因与结果状态

研究结果状态和停止原因分开保存：

```yaml
research_status: completed | partial | failed
termination_reason:
  coverage_complete | saturated | evidence_exhausted |
  budget_forced | tool_failure | user_cancelled
output_status: valid | repaired | fallback
```

含义如下：

| 原因 | 含义 | 通常结果状态 |
|---|---|---|
| `coverage_complete` | 没有会改变最终答案的关键缺口，必要结论获得充分证据 | `completed` |
| `saturated` | 只剩低影响细节，继续研究的边际价值不足 | `completed` 或诚实的 `partial`，由必要要求覆盖决定 |
| `evidence_exhausted` | 重要缺口仍存在，但多种合理路径均无法推进 | `partial` |
| `budget_forced` | 仍有可行动缺口，但时间、token、工具或轮数耗尽 | `partial` |
| `tool_failure` | 外部工具或环境导致研究无法继续 | 有可用结果则 `partial`，否则 `failed` |
| `user_cancelled` | 用户主动取消 | 有可用结果则 `partial`，否则 `failed` |

`Saturated` 与 `Exhausted` 的区别：前者剩余缺口影响低、不值得继续；后者缺口仍重要，但已经没有可行证据路径。

## 6. AgentGraph 落点

研究充分性是同一张 AgentGraph 中的独立机制，不是独立 Agent：

```text
think_and_plan
      ↓
use_tools / fork_children
      ↓
assess_research_state
      ├─ Continue → think_and_plan
      ├─ Replan   → 更新局部研究策略 → think_and_plan
      └─ Stop     → finalize_output → synthesize
```

`assess_research_state` 使用相同的 Research Agent policy 和当前 checkpoint 上下文，要求结构化输出：

```json
{
  "decision": "continue",
  "coverage": [
    {
      "requirement_id": "R1",
      "status": "supported",
      "evidence_ids": ["E1", "E3"],
      "rationale": ""
    }
  ],
  "critical_gaps": [
    {
      "requirement_id": "R2",
      "reason": "缺少原始统计数据"
    }
  ],
  "next_actions": [
    {
      "requirement_id": "R2",
      "strategy": "official_database",
      "query": "...",
      "expected_value": "high",
      "expected_improvement": "可能改变当前比较结论"
    }
  ],
  "termination_reason": null
}
```

评估结果和尝试历史必须进入 LangGraph checkpoint，保证中断恢复后不会忘记已经覆盖的要求、失败策略或待执行动作。

## 7. 确定性路由校验

程序不直接判断复杂事实真伪，但必须拒绝结构上不成立的决定：

1. `coverage` 必须包含所有必要 requirement；
2. 所有引用的 Evidence ID 必须存在于当前执行树证据集合；
3. `coverage_complete` 不得同时存在 `unsupported`、`weak` 或未解释的 `conflicted` 必要要求；
4. `Continue` 必须至少包含一个针对重要缺口的可执行 `next_action`；
5. `Replan` 必须说明原路径为何低价值，并提供实质不同的新策略；
6. `evidence_exhausted` 必须能看到同一重要缺口的多种已尝试策略以及无可行下一步的理由；
7. 硬预算和用户取消拥有最高优先级，不能被模型的 `Continue` 覆盖；
8. 非法评估结果进行一次无工具的结构化修复；修复失败时保留研究状态，使用确定性 fallback，不能把格式错误等同于研究未完成。

## 8. 子任务汇聚

子 Agent 返回证据、覆盖项、尝试记录和未解决缺口。根 Agent 合并后重新评估根 Research Brief：

- 子任务 `partial` 不自动导致根任务 `partial`；
- 如果根 Agent 或其他子任务已经补齐对应必要要求，整体仍可 `completed`；
- 只有根 Brief 的必要要求最终未支持，才影响根结果状态；
- 子任务的失败或穷尽原因继续保留，供根 Agent 判断和最终报告披露。

## 9. RCS 的定位

RCS（Research Completion Score）是研究结束后的评测参考，不是运行时单一停止阈值。它与 ResearchBench 规则指标和 LLM-as-Judge 共同用于比较模型、预算与 AgentGraph 版本。

RCS 优先保存多维结果：

```yaml
objective_coverage: 0.0-1.0
evidence_sufficiency: 0.0-1.0
conflict_resolution: 0.0-1.0
uncertainty_calibration: 0.0-1.0
research_efficiency: 0.0-1.0
overall: optional
```

`overall` 权重必须通过 ResearchBench、HotpotQA 和人工/LLM Judge 结果校准后再确定。即使 RCS 总分很高，只要存在未覆盖的关键必要要求，运行时也不能判定 `coverage_complete`。

当前实现位于 `scripts/run_eval.py`：从最终 `ResearchResult` 的 coverage、critical gaps 和工具调用量生成上述五个维度；空 coverage 不获得冲突解决满分；ResearchBench 同时保存逐题 RCS 和成功题目的维度均值。`src/research/` 不读取 RCS，也不存在 RCS 停止阈值。

RCS 可以用于：

- 最终评测报告；
- Agent 与 baseline 对比；
- 不同预算和模型之间的效果/成本比较；
- 检查 `Completed`、`Saturated`、`Exhausted` 判定是否合理；
- 后续 Web UI 的研究进度与质量解释。

## 10. 实施状态

- [x] 移除 `bdf310a` 的固定来源数、方向数乘二、连续 ready 轮次和全局零增量强制停止；
- [x] 从已确认 Research Brief 建立稳定 requirement 清单；
- [x] 增加 checkpointed coverage、critical gaps、next actions 和真实 strategy attempts；
- [x] 将 `assess_completion` 重构为同图节点 `assess_research_state`，返回 Continue/Replan/Stop；
- [x] 实现第 7 节的确定性路由校验和一次无工具结构化修复；
- [x] 修正子任务 `partial` 的无条件传播；
- [x] 分离 research status、termination reason 和 output status；
- [x] 在 `scripts/run_eval.py` 增加五维 RCS 最终评测输出，不提供未经校准的 `overall`，不接入运行时路由；
- [x] 运行专项测试、N1–N6 和仓库全量回归；
- [x] 重新完整运行相同三题真实 ResearchBench，并比较前后指标与停止原因。

当前确定性验收结果：充分性、AgentGraph、fork、评测与 checkpoint 专项 `90 passed`；N1–N6 `138 passed`；仓库全量 `772 passed, 2 skipped`。全量第一次运行曾出现一个与本次改动无关且不可复现的 Windows Memory Store 并发路径抖动，单测复跑与全量复跑均通过。

真实 ResearchBench 使用相同 `tech_001`、`med_001`、`fin_001` 和宽松预算（每 Agent 84、全树 588 次工具调用，1,000,000 token，单题 1,800 秒）。最终报告和 checkpoint 保存在本地 `outputs/evaluation/researchbench-state-model-final/`，不提交仓库。研究阶段累计 3,791.735 秒；按成功的规则评测和 Judge 调用折算，完整链路累计 4,202.346 秒。

| 指标 | 被否决完成门 | 最终状态模型 | 变化 |
|---|---:|---:|---:|
| 工具调用 | 86 | 160 | +74 |
| 估算 token | 157,142 | 1,731,327 | +1,574,185 |
| 研究耗时（秒） | 364.985 | 3,791.735 | +3,426.750 |
| 平均规则分 | 0.581436 | 0.580055 | -0.001381 |
| 平均 LLM Judge（0–10） | N/A | 4.400000 | 新增 |
| 平均规则/Judge 综合分 | N/A | 0.524033 | 新增 |
| 证据数 | 347 | 165 | -182 |
| RCS | N/A | coverage 0、sufficiency 0.344444、conflict 1、calibration 1、efficiency 0 | 新增 |

| 题目 | 根轮次（旧 → 新） | 规则分（旧 → 新） | 调用（旧 → 新） | token（旧 → 新） | Judge | 新停止/输出 |
|---|---:|---:|---:|---:|---:|---|
| `tech_001` | 4 → 9 | 0.586136 → 0.590288 | 45 → 26 | 78,146 → 216,030 | 5.0 | `max_iterations_exhausted → budget_forced` / `valid` |
| `med_001` | 4 → 5 | 0.578829 → 0.575370 | 33 → 86 | 68,913 → 1,000,000 | 3.4 | `token_budget_exhausted → budget_forced` / `valid` |
| `fin_001` | 4 → 6 | 0.579341 → 0.574508 | 8 → 48 | 10,083 → 515,297 | 4.8 | `time_budget_exhausted → budget_forced` / `valid` |

三题均没有因来源数或固定 ready 轮次在第 4 轮强制结束；真实递归还验证了子任务 `tool_failure` 不会无条件传播，父任务可在吸收子证据后合法判定 `evidence_exhausted`。根任务最终均为诚实的 `partial / budget_forced / valid`，没有伪装成完成；RCS 必要要求覆盖仍为 0，说明新增成本没有转化为稳定的根 Brief 充分覆盖，继续研究价值的语义判断通常晚于最优停止点。

验收期间还发现并修复了评测层配置遗漏：LLM Judge 过去没有读取 `modules.judge` 的采样配置，实际回退到 1,024 输出 token，长报告分块 JSON 会被截断。现在 Judge 显式使用配置的 `temperature=0.1`、`max_tokens=2048`，相同 `med_001` checkpoint 的六个报告分块和最终评分全部成功。该修复不改变 Research AgentGraph 或运行时终止路由。

仍需后续单独处理的观察项包括：提高检索证据与必要 requirement 的匹配质量、校准模型对 `saturated/evidence_exhausted` 的语义判断，以及让通用消息截断在极端情况下严格满足声明的字符上界。这些限制不得被解释为正常完成，也不应通过恢复固定轮次或来源阈值掩盖。

## 11. 验收标准

必须覆盖以下确定性场景：

1. 来源很多但关键 requirement 未覆盖：必须 Continue 或 Replan；
2. 来源较少但所有必要 requirement 已被充分支持：允许 Completed；
3. 重要缺口存在且有高价值下一步：必须 Continue；
4. 当前路径无效但有替代路径：必须 Replan；
5. 只剩低影响细节：允许 Saturated；
6. 重要缺口经过多种不同策略仍无法推进：允许 Exhausted；
7. 子任务 `partial` 但根证据已补齐：根任务允许 Completed；
8. 最终或评估 JSON 格式错误：执行一次格式修复，不污染研究状态；
9. 硬预算耗尽：必须 `budget_forced`，不得报告正常完成；
10. checkpoint 恢复后保持 requirement、coverage、attempt 和 decision 一致；
11. 真实三题不得因为来源数量或固定 ready 轮次在第 4 轮被强制结束；
12. 全量回归通过，不破坏既有同质 AgentGraph、fork policy、递归上限、checkpointer、Memory/Vault、Chat Store、Writer、检索与可选 Red/Blue。

## 12. 明确不做

- 不新增固定 Planner、Supervisor、Judge 或 Summarizer Agent；
- 不把 RCS 总分作为单一停止阈值；
- 不使用全局固定来源数量证明完成；
- 不因为一两轮没有新增来源直接停止；
- 不在 Replan 时自动扩大用户确认的研究范围；
- 不为本机制引入新的外部数据库、向量库或服务；
- 不改动 LLM Wiki、Obsidian、Vault Writer、Memory 检索等无关主线。

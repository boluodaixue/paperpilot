# fin_006：为什么 Judge 从 8.8 降到 6.2/5.4

## 技术结论

主要退化不是公平队列、并发上限或 synthesis 分类，而是两层质量漏斗同时变差：

1. r9b 的 5 个 Grandchild 在 r4 中完全消失，缺口没有再按条款族或来源族专业化；
2. 来源正文打开成功率和 Evidence→Claim 转化率显著下降；
3. Composer 两次结构输出失败后退化为平铺 Findings，进一步损害报告完整性。

r4 与 r9b 的工具调用几乎相同（17 vs 18），Evidence 数甚至略多（78 vs 75），但 Judge 从 8.8 降至 6.2。这说明问题不是“研究次数不够”，而是同样数量的调用没有形成同等质量的来源、Claims 和结构化综合。最小修复后的 r7 使用原始工作树 `.env` 中的 DeepSeek v4 Flash 完整复测并取得有效 Judge 5.4；它进一步表明递归恢复本身不足以恢复 8.8，当前主要瓶颈已经后移到 acquisition 效率和 Evidence→Claim→Report 选择。

## 精确对比

| 指标 | r9b（8.8） | r4（6.2） | 变化 | 诊断含义 |
|---|---:|---:|---:|---|
| Judge | 8.8 | 6.2 | -2.6（-29.5%） | 最终质量明显下降 |
| Fork tree | Root + 4 Child + 5 Grandchild | Root + 4 Child | Grandchild -5 | 缺口专业化消失 |
| Agent 数 | 10 | 5 | -50% | 模型未继续 Fork，不是队列拒绝 |
| Evidence | 75 | 78 | +4.0% | Evidence 数量不是瓶颈 |
| 报告来源 | 24 | 14 | -41.7% | 可用来源广度下降 |
| 工具调用 | 18 | 17 | -5.6% | 调用量基本持平 |
| Estimated tokens | 434,480 | 296,975 | -31.6% | 更早停止，没有维持质量 |
| 研究时间 | 809.8s | 168.0s | -79.3% | 探索深度明显下降 |
| Requirement coverage | 0.8（旧 5 项分母） | 0.5（新 4 项 research 分母） | 不完全同口径 | r4 只覆盖一半外部研究要求 |
| Primary-source coverage | 1.0 | 1.0 | 持平 | 来源权威等级不是主因 |
| Claim yield | 0.625 | 0.292 | -53.3% | Evidence 到可报告 Claim 的转化变差 |
| Material citation / invalid | 1.0 / 0 | 1.0 / 0 | 持平 | Citation 不是降分原因 |

Coverage 的分母已经变化，不能直接把 0.8→0.5 当作完全同口径回归；但 r4 的 4 个 external research requirements 中只有一半形成覆盖，这个事实仍然成立。

## r7 同模型有效复测

r7 明确加载原始工作树 `.env`（`override=True`），模型请求全部发往该配置的火山方舟 DeepSeek v4 Flash endpoint，并连续返回 HTTP 200；Judge 同样由 DeepSeek 正常完成。r9b、r4、r6、r7 使用的模型均为 DeepSeek v4 Flash，因此不需要把质量对比降级为模型环境不可比。

| 指标 | r9b（8.8） | r7（5.4） | 诊断含义 |
|---|---:|---:|---|
| Fork tree | Root + 4 Child + 5 Grandchild | Root + 2 Child + 4 Grandchild | 已恢复递归，但一级任务合并成两个大包，结构仍不稳定 |
| Agent 数 | 10 | 7 | 未触顶、无拒绝或预算取消 |
| Evidence | 75 | 60 | 候选证据略少，但不是零证据问题 |
| completed / failed sources | 24 / 6 | 15 / 4 | 打开成功率约 80% vs 79%，来源访问已恢复 |
| queries / Agent tools | 17 / 18 | 8 / 12 | acquisition 明显不足 |
| Estimated tokens | 434,480 | 406,542 | token 接近，但更多消耗在控制/等待而非检索 |
| 研究时间 | 809.8s | 1,014.2s | 更慢却少 9 个 query，效率显著下降 |
| Verified Claims | — | 7 | 60 条 Evidence 只留下 7 条 Claim |
| Claim yield | 0.625 | 0.304 | 后半段转化仍远低于 8.8 分版 |
| Requirement coverage | 0.8（旧口径） | 0.5（当前 research 口径） | 最终仍只覆盖一半外部研究要求 |
| Material citation / invalid | 1.0 / 0 | 1.0 / 0 | 引用合法性不是主因 |

r7 最终报告只有 1,885 字符和 4 个引用。Judge 分项为 factual accuracy 8、logical consistency 7、citation quality 5、comprehensiveness 3，明确指出缺少 ICMA GBP、SLB 募集资金可用于一般公司用途、KPI/SPT 与票息或债券条款挂钩、中国 SLB 规则，以及披露和投资者保护的系统比较。

这些内容并非都没有检索到。Vault 已包含但最终报告未使用的直接 Evidence 至少包括：

- `evidence-efc09b0f4367b85f`：GBP 四个核心组件，包括募集资金用途、项目评估、资金管理和报告；
- `evidence-4c768bab88ea3ad1`：SLB 不专项圈定募集资金、通常用于一般公司用途，并由 KPI/SPT 触发票息等条款变化；
- `evidence-9e286702023fba38`：SLBP 的 KPI、SPT、债券特征、报告与验证框架；
- `evidence-453b03e5a566cb5a`：未达标时 coupon step-up 及 call option 对投资者保护效果的影响。

因此 r7 的直接失败点是：已经抓到的关键证据没有通过或没有进入 Claim/Composer inventory。60 条 Evidence 仅选出 7 条、Claim entailment/yield 为 0.304，随后 Citation Audit 又移除约 14.3%；最终只剩目录规则、可持续发展债券用途变更和一条 Natixis 的 KPI/SPT 修订信息。Composer 按目标章节输出改善了版式，但无法弥补上游 Claim inventory 缺项。

## 主因一：Child 把“单一 Requirement”错误理解为“不可继续拆分”

r4 的 12 次 Child 控制决策全部选择 `local_research`。其 rationale 反复出现：

- `single requirement`
- `focused scope`
- `further forking would fragment the work`

但这些 Assignment 实际并不窄。例如 investor protection 同时包含：

- 资金追踪；
- 票息或条款调整；
- default triggers；
- 法律救济边界；
- ICMA 与中国规则两个独立来源体系。

Requirement 是最终覆盖单位，不应被模型当作执行拆分的最小单位。r9b 正是通过 disclosure 与 investor-protection 分支的 5 个 Grandchild，把具体缺口按来源/条款 scope 隔离后取得更好结果。

当前 exact fingerprint、队列和 5-child 限制都没有阻止递归：r4 accepted 4、rejected 0、cancelled 0，且 Child 仍有合法 Grandchild 容量。没有 Grandchild 是控制语义选择，不是硬校验或调度拒绝。

## 主因二：来源打开成功率下降，Evidence 数量掩盖了质量退化

r9b 记录了 24 completed / 6 failed source fetches，成功率约 80%。r4 最终为 14 completed、15 个失败 URL，并产生 18 次 failure events，成功率约 44%。

r4 的失败集中在：

- 4 个 ICMA PDF 抽取超时；
- PBOC 旧链接 404；
- 403、429、521；
- 一条带尾随冒号的坏 URL；
- 学术 PDF/商业页面抽取超时。

因此 r4 虽然产生 78 条 Evidence，但报告只保留 14 个来源，最终只有 7 个 verified Claims。Claim yield 从 0.625 降至 0.292，说明主要损失发生在“找到候选→成功打开正文→形成可报告 Claim”的中后段。

这也解释了为什么简单增加工具调用或 Evidence 上限不会解决问题：r4 已经有更多 Evidence，却更少可用结论。

## 放大器：Composer fallback 破坏了最终结构

r4 的 Composer 两次返回无效结构，随后使用 deterministic fallback。最终报告变成通用 `Findings / Limitations / References`，没有按以下要求形成系统对照：

- 定义与规则框架；
- 募集资金用途；
- 信息披露与验证；
- 投资者保护；
- 对照结论与证据边界。

Judge 对 r4 的 comprehensiveness 仅给 5，明确指出内容散、没有系统覆盖募集资金用途、披露与投资者保护。Citation coverage 仍为 1.0、invalid citation 为 0，因此引用审计并未造成退化；Composer 结构失败直接放大了上游覆盖不足。

## 次级状态问题：Root 被错误标成 Tool Failure

r4 的四个 Child 都在真实子 token hard limit 上返回：

`budget_forced / token_budget_exhausted`

Root 随后提出了已经重复过的 strategy family，assessment repair 再次违反合同。确定性 fallback 将根 termination 标成 `tool_failure`。这不是搜索工具整体不可用：r4 有 14 个 completed queries 和 14 个成功打开的 canonical sources。

这个问题影响状态准确性，但不是 Judge 从 8.8 降到 6.2 的主因。即使改成 `evidence_exhausted`，报告内容本身不会自动改善。

## 已排除的主因

- **公平队列**：active 峰值 4、queued 峰值 4、waiting 峰值 1；0 rejected、0 cancelled，queue wait 峰值 39ms。
- **并发/总 Agent 上限**：只使用 5/24 Agents，远未触顶。
- **工具调用数量**：17 vs 18，几乎相同。
- **Evidence 总量**：78 vs 75，r4 反而更多。
- **Citation Audit**：两版 material citation coverage 都是 1.0，invalid citation 都是 0。
- **synthesis requirement 分类**：它修正了 coverage 分母，没有占用外部检索预算，不能解释 source-open 和 Claim yield 的下降。

## 最小修复顺序

### 1. 只修控制提示词，不恢复硬侦察门

在 `research_control_prompt` 中明确三件事：

- Requirement 是覆盖单位，不是不可拆分的 Assignment；
- `single requirement` 或 `focused requirement` 不能单独作为拒绝 Fork 的理由；
- 当一个 gap 含两个以上独立条款族、来源体系或三步以上工具链时，应使用不同 scope Fork Grandchild。

这是最小、最直接的修复，因为它正面针对 r4 中 12 次相同的错误 rationale，不需要改 Validator、Scheduler、Blackboard 或预算模型。

### 2. 只跑一次 fin_006 验证该提示词

第一道门只看中间指标：

- 恢复 2–5 个 Grandchild；
- 不出现 duplicate/ownership/queue rejection；
- source-open success 高于 r4；
- Claim yield 明显高于 0.292。

不要在同一轮同时调整预算、并发、Selector 或 Citation Audit，否则无法确认恢复来自哪里。

### 3. 如果仍不 Fork，再把灰度配置的 reconsideration 从 2 调到 1

只调整 fin_006 灰度配置的 `reconsider_after_local_rounds`，不改默认配置，也不重新引入“必须先调用工具”的硬门。它只让 Child 在第一次本地研究后立即显式重审 Fork。

### 4. 局部增强 deterministic Composer fallback

只改 `report_composer.py` 的 fallback：

- 使用 `plan.report_outline` 作为章节；
- 按 Claim 的 CoreQuestion/Requirement 归组；
- 始终生成 synthesis 与 gap disclosure 章节；
- 继续只使用 verified Claim IDs，不放宽 Citation Audit。

这不能替代检索修复，但能避免一次 Composer JSON 失败把已有证据降级成无结构列表。

### 5. 最后修 termination taxonomy

当 Root 已有 Evidence、没有未尝试动作、Child 已真实耗尽子预算，而 assessment 仅因重复 strategy family 失败时，应优先归为 `evidence_exhausted`，而不是 `tool_failure`。这一步只修状态，不应和质量修复混在同一次实验中。

## 不建议做的修改

- 不提高 `max_concurrent_agents`、`max_total_agents` 或 `max_fork_depth`；
- 不增加总 token/tool budget；
- 不恢复模糊相似度阻断；
- 不恢复 Child 首次工具调用硬门；
- 不放宽 Claim/Citation 审核；
- 不切换默认架构或引入 Supervisor 对照。

## 最小修复实施结果

第一版提示词只强调“Requirement 可拆”，r5 立即从不 Fork 摆向过度 Fork：23 Agents、13 次 Fork、6 个预算取消，仅 24 Evidence、8 个报告来源、3 个 verified Claims，Judge 降到 4.8。该轮证明不能只增加 Fork 倾向，必须同时约束具体目标、批次大小、已有子任务和剩余容量。

平衡提示词随后增加以下语义刹车：

- 不按 objective 的每个短语机械拆分；
- 必须有具名条款、jurisdiction、primary document 或 tool chain；
- depth 1 通常只派 2 个、最多 3 个候选；
- 一批派发后优先 merge，只有出现新的非重叠高价值 gap 才再次 Fork；
- 控制上下文显式给出剩余总 Agent 槽位和可委托 token。

r6 因此落在两端之间：Root + 4 Child + 2 Grandchild，共 7 Agents；0 预算取消、0 队列拒绝。与 r4 相比：

| 指标 | r4 | r6 | 变化 |
|---|---:|---:|---:|
| Agent 数 | 5 | 7 | 恢复 2 个 Grandchild |
| Evidence | 78 | 99 | +27% |
| 报告来源 | 14 | 22 | +57% |
| Verified Claims | 7 | 11 | +57% |
| Requirement coverage | 0.50 | 0.75 | +0.25 |
| Claim yield | 0.292 | 0.458 | +57% |
| 工具调用 | 17 | 19 | 基本持平 |
| Tokens | 296,975 | 365,042 | +23% |

r6 Judge 调用当时因 DeepSeek `402 Insufficient Balance` 失败，因此不能把中间指标改善等同于最终分数恢复；该错误是同一 DeepSeek v4 Flash 服务的账户状态，不是换用了不同模型。随后同模型、原始 `.env` 的 r7 Judge 正常完成并得到 5.4，证明平衡 Fork 提示词关闭了“不递归”和“过度递归”两个极端，但没有恢复最终质量。`reconsider_after_local_rounds` 保持 2，没有必要调成 1。

r7 后不应继续调整 Fork 宽度。下一步若继续修复，应只做一个小实验：检查 Evidence selector/Claim verifier 为什么拒绝上述四条直接证据，并给每个 external research requirement 保留至少一个高相关、可定位的候选 Claim；不要同时改 Scheduler、预算、Citation Audit 或默认架构。

## 最终实施判定

后续不再以“Grandchild 数量”单独调提示词，而是恢复 r9b 的行为不变量：Root 初始四个 external requirements 分别拥有一级 Child，Child 只有在观察到本 scope 的 query/source/Evidence 后才以 `parallel/context_isolation` 继续 Fork，三步 `deep_tool_chain` 例外。当前 Scheduler、Blackboard、exact duplicate、规范容量、synthesis 分类、Composer、Citation Audit 和 termination taxonomy 保留。

r8 在同一 DeepSeek v4 Flash 环境下稳定得到 Root + 4 Child + 5 Grandchild，并恢复 Judge `8.6`、Claim yield `0.65`、研究 `787.812s`；这验证了 r9b 行为基线。r9 对 semantic verifier 采用过细的 6 条分批及 partial 二次验证后，内部 coverage 达到 `1.0`，但研究耗尽 1,200 秒且 Judge 降到 `6.8`，因此该方案被拒绝。

最终代码保留 semantic verifier，但收敛为 8 条分批、遗漏 fail-closed、无 partial 二次验证；selector 先过滤不可形成 Claim 的页面，并限制每个 Requirement 最多由 3 个不同 primary sources 占位，对语义验证后仍为空的 Requirement 做一次有界回填。最终完整回归 `1075 passed, 3 skipped`；没有继续用第三次在线样本追分。

## 后续状态：按要求恢复 r8，科技泛化未通过

工作树随后恢复到 r8 的实际代码状态，并使用关闭 `fin_006` fixed plan 的同预算配置运行 `tech_001` 与 `tech_002`。两题 Judge 分别为 `4.6` 和 `5.4`，平均 `5.0`；尽管共取得 476 Evidence 和 65 个成功正文来源，两份报告 comprehensiveness 都只有 `2/10`。因此 r8 的 8.6 应解释为“固定金融题行为恢复成功”，不能解释为跨领域 Research Agent 已达到同等质量。当前首要泛化缺口是动态 ResearchBrief 如何进入与 fixed plan 相同的 Requirement→Claim→结构化 Composer 路径，而不是继续增加 Evidence 或 Agent 数。

另外完成两个局部修复：deterministic Composer fallback 现在按 `report_outline`/CoreQuestion 分章节，对缺失 research 与 synthesis 章节写明确 gap disclosure；Root 在保留 Evidence、无未尝试动作且所有 incomplete Child 已到真实 budget/evidence 边界时，assessment repair 失败会归为 `evidence_exhausted`，不再误标通用 `tool_failure`。

## 证据与限制

- r4 使用评测 JSON、Blackboard SQLite、LangGraph checkpoint 和最终报告核验。
- r7 使用独立评测 JSON、Blackboard SQLite、Vault Evidence 和最终报告核验；Judge 有效，所有模型请求均由原始 `.env` 指定的 DeepSeek v4 Flash 完成。
- r9b 原始 JSON/checkpoint 当前不在此工作树，细节来自已提交的实施记录和 e8abeb9 配置快照。
- 两轮均为 temperature 0.7 和实时网页环境下的单样本，不能把全部 2.6 分差异严格因果归于某一处代码；但递归深度、来源成功率、Claim yield 与 Composer 结构同时下降，而队列和 Citation 指标保持正常，因此主因排序具有较高置信度。

# PaperPilot 实现与验收计划

## 1. 当前完成基线

当前版本已经形成一条统一产品链路：

```text
Web / CLI
→ ResearchRuntime
→ Research Workflow + LangGraph interrupt/checkpoint
→ Homogeneous Research AgentGraph
→ Durable Vault Write Queue / Single Writer
→ Markdown Memory Vault
→ Obsidian / Memory QA / Continue Research
```

已实现并通过确定性测试的核心能力：

- 根、子、孙同质递归与全树硬预算；
- 基于确认后 Research Brief 必要要求的 Continue / Replan / Stop Research；
- checkpointed coverage、critical gaps、next actions 和真实 strategy attempts；
- 原子 requirement 与非证据型 expected-output deliverable、requirement/action/artifact 三段 lineage；
- 语义工具错误门、Single Writer 原始 artifact、L1–L5 有界 Working Context；
- research status、termination reason、output status 三分状态与一次结构修复；
- 仅用于离线最终评测的五维 RCS（不参与运行时停止）；
- Research Brief 修改/确认和 checkpoint 恢复；
- 多长期 Memory 的隔离研究与 Markdown/WikiLink 持久化；
- Obsidian 定位、Memory 问答和受控保存笔记；
- PDF、文本和显式 URL 的受控导入；
- Runtime Registry、TTL、租约与 SSE outbox；
- 单一 Vault Writer、staging/journal 和崩溃恢复；
- legacy 外部归档与历史路径映射；
- SQLite FTS5 和可选本地多语言混合检索；
- 默认关闭的单次 Red/Blue 报告复核。

当前回归基线以 CI/本地全量 `pytest -q` 的最新结果为准；真实 ResearchBench 只有在纯函数、Workflow、Writer/recovery、adversarial 和全量回归全部通过后才能启动。

## 2. 统一验收原则

每次修改按风险选择以下层级：

1. 纯函数和数据契约单元测试；
2. Workflow、checkpoint、Writer、Memory 和检索专项；
3. 进程重启、租约接管、并发、重复请求和失败注入；
4. Web/CLI 固定离线端到端；
5. 仓库全量 `pytest -q`；
6. 对研究终止机制使用相同题目和宽松预算运行真实 ResearchBench；
7. 真实模型、真实网络和 Obsidian 手工 smoke test。

外部服务 smoke test 不能替代确定性测试，固定 fixture 也不能冒充真实模型效果。

研究终止机制的真实验收固定比较同一组 `tech_001`、`med_001`、`fin_001`：逐题和汇总记录研究工具调用数、估算 token、耗时、ResearchBench 规则分、五维 RCS、research status、termination reason、output status、轮次与未解决项。历史版本没有 RCS 或稳定停止分类时记为 `N/A`，不得补造。验收还必须确认任务没有因为来源数量或固定 ready 轮次在第 4 轮强制结束。

本次最终真实验收使用每 Agent 84 次、全树 588 次工具调用和 1,000,000 token 的宽松预算。三题根轮次分别为 1、7、2，不再固定于第 4 轮；全部由真实墙钟边界停止并记录为 `time_budget_exhausted → budget_forced`。平均规则分从 0.581436 提高到 0.591106，但工具调用从 86 增至 104、估算 token 从 157,142 增至 578,771、耗时从约 365 秒增至约 981 秒；一题输出 `valid`、两题 `fallback`，五维 RCS 显示 objective coverage 与 evidence sufficiency 均为 0。该结果证明机械完成门已移除和状态分类生效，同时把语义收敛与递归最终综合稳定性保留为后续真实模型优化项。

证据闭环与 L1–L5 实现后的分阶段 canary 使用新的隔离 checkpoint/Vault。首轮暴露出同一 requirement 细分抢占 fork 名额、多轮 fork 侵蚀父最终化预留、失败路径继续耗尽 token 三个问题；加入 requirement 广度优先、15% subtree 父级预留下限和三种策略族无进展熔断后，完整确定性回归为 `792 passed, 2 skipped`。第二轮 `tech_001` 将工具调用从 31 降至 12、估算 token 从 200,000 降至 56,248，输出由 `fallback` 改为 `valid`，LLM Judge 从 3.2 升至 5.8；但仍因 9 次外部检索失败在 600 秒墙钟边界 `budget_forced`，RCS 覆盖仍为 0，因此按门禁停止，未扩展到三题。

## 3. 作品集版交付

### P0：公开仓库整理

- 精简 README，展示问题、演示链路、架构和工程难点；
- 修正包元数据、仓库 URL、作者、LICENSE；
- 添加 GitHub Actions 自动运行测试；
- 只保留当前架构和精选技术记录；
- 完整阶段历史保存在独立 archive 分支。

### P1：真实链路 smoke test

使用实际模型和搜索后端完成一次固定研究题目：

1. 创建 Memory；
2. 生成、修改并确认 Research Brief；
3. 触发至少一次 fork，记录根/子执行；
4. 获取网页和论文来源；
5. 在任务中途或等待确认时重启服务并恢复；
6. 检查最终报告、证据、来源和 WikiLink；
7. 在 Obsidian 中打开报告；
8. 执行 Memory 问答和继续研究；
9. 保存耗时、调用次数、成本和已知限制。

### P2：演示资产

- 60–90 秒 GIF 或视频；
- 一张最终界面截图；
- 一份脱敏的真实研究报告；
- README 中展示真实运行指标和限制。

## 4. 可选改进

- 将 semantic/evaluation 重依赖拆为可选 extras，缩短首次安装；
- 为核心 runtime/workflow/retrieval 收敛静态类型检查；
- 拆分单文件 Web 前端，改善首次使用提示和移动端布局；
- 增加检索模式的 Web 状态展示和简单离线召回指标。

这些改进不能引入新的固定 Agent 角色、第二知识真相源、默认跨 Memory 检索或绕过用户确认的写入。

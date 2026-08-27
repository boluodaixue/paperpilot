# N6：可选 Red/Blue 报告审查

## 状态

已完成（2026-08-28）。生产代码、关键专项与回归以及全量回归均已通过主 Agent 验收。

## 目标

N6 在原始最终报告已经成功写入 Markdown Memory Store 后，提供一个默认关闭的单次报告质量后处理。它只允许在不改变来源关系和持久化契约的前提下改善报告正文；即使审查完全不可用，N2–N5 的正常交付也必须成立。

```text
Research Workflow 完成
→ 原始 report / evidence / source 持久化
→ 取得原始 Report Markdown + MemoryManifest
→ research.report_review.enabled = false：直接返回原结果
→ research.report_review.enabled = true：
   Red 一次 → Blue 一次 → 确定性保护校验
   ├── 通过：原路径原子写回报告
   └── 失败：保留并返回原报告
```

## 实现边界

### 执行方式

- 默认关闭；关闭时不得新增 policy 调用或文件写入；
- 只复用 `ResearchRuntime` 已经装配的同一个 policy，不创建第二套模型路由；
- Red 和 Blue 各至多调用一次；
- 不给 Red/Blue 暴露工具定义，不执行工具调用；
- 不进入同质 Research AgentGraph，不参与其条件路由、完成判断或恢复；
- 不 fork，不创建 `thread_id`、`parent_thread_id`、`root_thread_id` 或 checkpoint；
- Red/Blue 输入输出只作为本次后处理的临时结构化数据，不持久化为新的知识实体。

### Red 输出

Red 只允许返回以下三类问题：

| 类型 | 含义 |
|---|---|
| `factual` | 与报告现有材料不一致、过度断言，或无法由已有内容支持的事实陈述 |
| `logical_consistency` | 前提、推理、结论之间跳跃，或报告内部存在逻辑冲突 |
| `citation_quality` | 引用位置、引用对象，或引用对相邻结论的支持关系存在问题 |

每个问题只需定位报告片段并说明问题与建议方向。该结构不是新的 claim-evidence 领域模型，不写入 Memory Store，也不承担评分或完成判断。

### Blue 动作

Blue 只能返回四类动作：

- `ADD`：补充不引入新来源的解释、限定或过渡文字；
- `DELETE`：删除无法安全保留的正文；
- `MODIFY`：修改既有正文，使其与已有证据和逻辑一致；
- `VERIFY`：记录该问题已由现有报告内容核验，无正文变更。

未知动作、额外字段驱动的副作用或要求重新研究的动作都视为无效。`VERIFY` 不调用工具，也不访问网络或文件来源；无法由现有内容核验的问题不得假装已解决。

根工作流按 Blue 返回的顺序确定性重放所有动作。重放得到的报告必须与 Blue 同时返回的完整 Markdown 完全一致；如果二者不同，说明完整报告包含未由四类动作声明的修改，整次后处理降级到原报告。

## 确定性保护

保护基准来自后处理前已经持久化的原报告和 `MemoryManifest`。接受修订前必须全部满足：

1. YAML frontmatter 与原报告逐字一致；
2. 全部 Markdown WikiLink 的 target 和出现次数一致；alias 只是显示文本，可以调整，但不得改变链接目标；
3. 全部 URL 的值和出现次数一致；
4. `MemoryManifest` 对象及 `report_path`、`evidence_paths`、`source_paths` 均不改变；
5. 只写回 manifest 已指向的同一个 report 文件；
6. evidence 和 source 文件不被修改、创建或删除。

这些检查由确定性代码完成，不把“是否安全”再次交给 policy 判断。只有校验全部通过，修订报告才可以用原子替换写回。保护失败不尝试放宽规则。

## 已知边界

现有 `ResearchResult` 没有 claim 到 evidence 的逐条映射。因此，确定性校验只能证明 frontmatter、WikiLink target、URL 和 manifest 等结构未被损坏，不能机械证明每条新增或修改后的表述都语义归因于正确证据。语义层面的事实、逻辑和引用审查由 Red/Blue 基于本次已有 evidence 完成。

N6 不为获得这种强保证而扩展 claim-evidence 数据模型。若未来需要对每条表述与证据归因进行机械证明，必须另行对齐数据契约、迁移影响和验收标准，不能在 N6 内隐式扩大范围。

## 失败降级

以下任一情况都必须保留并返回原始已持久化报告：

- Red 或 Blue policy 调用异常；
- Red 问题或 Blue 动作无法解析；
- Red 返回类别越界，或 Blue 返回动作越界；
- Blue 无法确定性应用动作；
- Blue 动作重放结果与其完整 Markdown 不一致；
- frontmatter、WikiLink、URL 或 manifest 保护失败；
- 原子写回失败。

失败降级不得再次执行研究、重复持久化 evidence/source、生成替代 manifest 或创建备用报告路径。调用者继续获得有效的原 `ResearchWorkflowResult` 与原 manifest。

## 明确不做

- RCS、报告质量评分、奖励模型或新的完成判断；
- 独立 Red Agent / Blue Agent、LangGraph 节点、工具循环或多轮对抗；
- 新研究线程、fork、checkpoint 或恢复协议；
- claim-evidence 新领域模型、Review Store、Review Repository 或第二套 Memory；
- 修改原始 evidence/source、伪造引用或新增来源；
- 因审查失败阻断原报告交付。

## 验收清单

- [x] 默认关闭且零额外 policy 调用；
- [x] 开启时只在原报告持久化成功后运行；
- [x] Red/Blue 复用同一 policy，各至多一次且无工具；
- [x] Red 只接受三类问题，Blue 只接受四类动作；
- [x] Blue 动作按顺序确定性重放，且与其完整 Markdown 完全一致；
- [x] 不创建线程、fork、checkpoint 或新持久化实体；
- [x] 有效正文修订可以原路径原子写回；
- [x] frontmatter、WikiLink target/次数、URL 值/次数与 manifest 篡改均被拒绝，alias 调整可通过；
- [x] policy、解析、动作、校验和写回失败均返回原报告；
- [x] N6 专项测试通过；
- [x] N1–N6 全量回归通过。

## 验收结果

- 关键专项与回归：`65 passed, 1 warning`；
- N1–N6 全量回归：`160 passed, 1 warning`；
- 全量回归中的 warning 为既有 `StarletteDeprecationWarning`。

主 Agent 已完成生产实现、架构边界、专项测试和全量回归验收，N6 正式完成。

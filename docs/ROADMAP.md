# PaperPilot Roadmap

## 当前状态

核心研究与长期 Memory 链路已经完成：

| 能力 | 状态 | 结果 |
|---|---|---|
| 同质 Research AgentGraph | ✅ | 根、子、孙复用同一图，fork 与全树预算可恢复 |
| 研究充分性与终止机制 | ✅ | 固定完成门已替换；真实三题完整运行到 LLM Judge，确认不再第 4 轮强停，并诚实披露预算收束 |
| 证据闭环与有界上下文 | ✅ | requirement/action/artifact lineage、语义工具错误门、Single Writer artifact offload、L2–L5 分级压缩与动态父预算 |
| Research Workflow | ✅ | Brief 修改/确认、interrupt、SQLite checkpoint 和重启恢复 |
| Markdown Memory | ✅ | 多 `memory_id`、报告/证据/来源/笔记/导入和 WikiLink |
| Obsidian 接入 | ✅ | 安全打开 Memory、报告和引用，不写 `.obsidian/` |
| Memory 问答与继续研究 | ✅ | 当前 Memory 检索、带路径引用、确认后保存笔记 |
| 受控资料导入 | ✅ | PDF/文本/URL 预览确认后成组写入 |
| 单一 Vault Writer | ✅ | 持久队列、lease、staging/journal、幂等和崩溃恢复 |
| 持久化检索 | ✅ | FTS5 增量索引、最终哈希复核和可删除重建 |
| 可选混合检索 | ✅ | 本地多语言语义 + FTS + WikiLink，失败降级 |
| 离线自动化验收 | ✅ | 全量确定性、故障注入、恢复和 Web/CLI 回归持续作为真实评测前置门禁 |

## 当前优先级

### 0. 修正研究终止机制

- [x] 保存研究充分性与终止机制设计；
- [x] 移除固定来源数和连续轮次强制停止；
- [x] 实现 Continue / Replan / Stop Research；
- [x] 分离 research status、termination reason 和 output status；
- [x] 将 RCS 接入最终评测，而不是运行时停止阈值；
- [x] 运行专项、全量回归和相同三题真实验收；三题分别按轮次、token、时间边界 `budget_forced` 诚实停止，完整 LLM Judge 均成功，后续需改善 requirement/证据匹配与语义收敛。
- [x] 补齐 requirement/action/artifact 证据归属、语义工具错误识别、跨 requirement 覆盖拒绝和同策略族无进展上限；
- [x] 完成 L1–L5 有界上下文、Single Writer artifact 恢复、动态父 assessment/finalization 预算和失败保真回退；
- [x] 用隔离 `tech_001` canary 校准 requirement 广度优先、不可侵蚀父级 token 预留和三策略族无进展熔断；第二轮输出有效且资源显著下降，但因外部检索失败仍在墙钟边界停止，故未放行三题扩展；

### 1. 作品集发布

- [x] 整理公开文档，并分离完整历史归档与单提交作品集版；
- [x] 配置 GitHub Actions；
- [ ] 将作品集版发布到远端 `main`；
- [ ] 打版本标签。

### 2. 真实模型演示

- [x] 使用真实模型、搜索和论文读取跑三题完整 ResearchBench 与 LLM Judge；
- [ ] 验证一次服务重启恢复；
- [ ] 在 Obsidian 中检查 Markdown 与 backlinks；
- [ ] 验证 Memory 问答与继续研究；
- [x] 记录调用、token、耗时、规则分、RCS、Judge、停止原因和失败点。

### 3. 展示材料

- [ ] 添加产品截图和 60–90 秒演示；
- [ ] 提交一份脱敏真实样例报告；
- [ ] 在 README 展示真实运行指标；
- [ ] 准备面试中的架构取舍说明。

## 暂不投入

- 多租户登录、RBAC、计费和公网 SaaS 安全体系；
- Kubernetes、多区域容灾和分布式基础设施；
- 外部向量数据库、图数据库或 Evidence Graph；
- 新的固定 Agent 角色；
- 默认跨 Memory 检索和未经确认的自动写入。

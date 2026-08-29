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
- Research Brief 修改/确认和 checkpoint 恢复；
- 多长期 Memory 的隔离研究与 Markdown/WikiLink 持久化；
- Obsidian 定位、Memory 问答和受控保存笔记；
- PDF、文本和显式 URL 的受控导入；
- Runtime Registry、TTL、租约与 SSE outbox；
- 单一 Vault Writer、staging/journal 和崩溃恢复；
- legacy 外部归档与历史路径映射；
- SQLite FTS5 和可选本地多语言混合检索；
- 默认关闭的单次 Red/Blue 报告复核。

当前离线回归基线为 `702 passed, 2 skipped`。

## 2. 统一验收原则

每次修改按风险选择以下层级：

1. 纯函数和数据契约单元测试；
2. Workflow、checkpoint、Writer、Memory 和检索专项；
3. 进程重启、租约接管、并发、重复请求和失败注入；
4. Web/CLI 固定离线端到端；
5. 仓库全量 `pytest -q`；
6. 真实模型、真实网络和 Obsidian 手工 smoke test。

外部服务 smoke test 不能替代确定性测试，固定 fixture 也不能冒充真实模型效果。

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

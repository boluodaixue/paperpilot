# PaperPilot Roadmap

## 当前状态

核心研究与长期 Memory 链路已经完成：

| 能力 | 状态 | 结果 |
|---|---|---|
| 同质 Research AgentGraph | ✅ | 根、子、孙复用同一图，fork 与全树预算可恢复 |
| Research Workflow | ✅ | Brief 修改/确认、interrupt、SQLite checkpoint 和重启恢复 |
| Markdown Memory | ✅ | 多 `memory_id`、报告/证据/来源/笔记/导入和 WikiLink |
| Obsidian 接入 | ✅ | 安全打开 Memory、报告和引用，不写 `.obsidian/` |
| Memory 问答与继续研究 | ✅ | 当前 Memory 检索、带路径引用、确认后保存笔记 |
| 受控资料导入 | ✅ | PDF/文本/URL 预览确认后成组写入 |
| 单一 Vault Writer | ✅ | 持久队列、lease、staging/journal、幂等和崩溃恢复 |
| 持久化检索 | ✅ | FTS5 增量索引、最终哈希复核和可删除重建 |
| 可选混合检索 | ✅ | 本地多语言语义 + FTS + WikiLink，失败降级 |
| 离线自动化验收 | ✅ | `702 passed, 2 skipped` |

## 当前优先级

### 1. 作品集发布

- [x] 整理公开文档，并分离完整历史归档与单提交作品集版；
- [x] 配置 GitHub Actions；
- [ ] 将作品集版发布到远端 `main`；
- [ ] 打版本标签。

### 2. 真实模型演示

- [ ] 使用真实模型、搜索和论文读取跑完整任务；
- [ ] 验证一次服务重启恢复；
- [ ] 在 Obsidian 中检查 Markdown 与 backlinks；
- [ ] 验证 Memory 问答与继续研究；
- [ ] 记录成本、耗时、失败点和改进项。

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

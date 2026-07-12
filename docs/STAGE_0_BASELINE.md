# 阶段 0：基线封存记录（历史记录）

> 本文只保存当时的代码与测试基线，不再定义当前目标架构。现行设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。

日期：2026-08-27

## 目标

在进入执行内核和动态 Fork 重构前，封存当前已完成产品能力、统一目标架构文档，并建立可重复验证的稳定起点。

## 基线提交

| 提交 | 内容 |
|------|------|
| `cdc6a97` | Obsidian 自动/手动导出、证据图节点富化、关系筛选、悬浮详情及测试 |
| `81e16e6` | Research Manager 驱动的同质子 Agent 动态 fork 架构、路线图和实施计划 |

## 验证结果

执行：

```powershell
.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

结果：

```text
132 passed, 1 warning
```

唯一警告来自当时使用的旧 tracing 依赖导入路径，不影响阶段 0 功能基线；该实现已在阶段 0.5 迁移到 Langfuse。

覆盖的阶段 0 关键能力包括：

- Web 研究报告持久化；
- Obsidian 自动导出成功与失败降级；
- Obsidian 手动导出端点；
- 报告、证据、论文和关系笔记生成；
- 报告 Evidence ID 到 Obsidian 双链转换；
- 证据图节点 claim、摘录、论文信息富化；
- 前端语义关系筛选所需数据契约；
- 原有 Orchestrator、Planner、Agent、Memory、Evidence 和评测单元测试。

## 未纳入版本库的本地产物

以下内容保留在本地，但通过 `.gitignore` 排除：

- `outputs/reports/` 运行报告；
- `outputs/obsidian/` 生成的 Vault；
- `outputs/viz_smoke*` 可视化冒烟产物；
- `configs/e2e_fast.yaml` 临时联网 E2E 配置；
- pytest、Torch 和阶段验证临时目录。

没有删除用户报告或其他本地数据。

## 下一阶段入口

下一阶段是“阶段 1：执行正确性”，范围严格限定为：

1. Policy 调用状态与 tools 并发隔离；
2. AgentPool 类型、回收和合成生命周期修复；
3. 全局超时降级合成；
4. Researcher 有效检索、工具重试和替代策略；
5. 配置生效检查与 HotpotQA 短答案评测修复。

在阶段 1 验收前，不开始 ForkSpec、AgentFork 或 ForkController 编码。

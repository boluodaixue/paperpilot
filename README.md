<div align="center">

# PaperPilot

### 基于 LangGraph 的同质 Research Agent 递归研究系统

[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](https://python.org)
[![LangGraph](https://img.shields.io/badge/Orchestration-LangGraph-green.svg)](https://www.langchain.com/langgraph)

</div>

## 项目目标

PaperPilot 先由根 Research Agent 与用户对齐研究目标。用户确认后，根 Agent 开始研究，并在需要并行、隔离上下文或处理较深工具调用链时 fork 同质子 Agent。子 Agent 可以再 fork 一次，所有层级运行同一个 Research AgentGraph。

```text
用户问题
→ 根 Agent 对齐研究方向
→ 用户修改 / 确认
→ 同质 Research AgentGraph
   ├── 思考与拆解
   ├── 检索 / 阅读 / 分析 / 比较 / 证据提取
   ├── 按条件 fork 同质 Agent
   └── 汇聚并总结本级结果
→ 根 Agent 生成最终报告
→ 报告、证据、来源写入 Markdown Memory Store
→ 可选 Red/Blue
```

完整定义见 [目标架构](docs/ARCHITECTURE.md)，具体步骤见 [实施计划](docs/IMPLEMENTATION_PLAN.md)，当前进度见 [路线图](docs/ROADMAP.md)。

## 核心设计

### 同一个 Research AgentGraph

根、子、孙 Agent 使用相同的图、工具协议和结果协议。每个 Agent 都能：

- 思考和拆解当前任务；
- 判断本地执行还是 fork；
- 检索、阅读、分析、比较和提取证据；
- 汇聚子 Agent 结果；
- 总结并返回结构化 Research Result。

根 Agent 只额外负责用户交互和最终报告发布，不是另一种 Manager Agent。

### 有界 Fork

满足以下任一条件时可以 fork：

1. 多个任务没有依赖，可以并行；
2. 中间材料较多，需要隔离上下文；
3. 预计需要至少三层连续工具调用。

递归限制为：根 `depth=0` → 子 `depth=1` → 孙 `depth=2`。孙 Agent 禁止继续 fork。

### Markdown Memory Store

系统只使用一个持久化 Memory Store：

```text
memory/
├── reports/
├── evidence/
└── sources/
```

报告通过 `[[Evidence-...]]` 链接证据，证据通过 `[[Source-...]]` 链接论文、网页或文件来源。关系由 Markdown WikiLink 和 Obsidian backlinks 表达，不建设独立 Evidence Graph。

### 简单完成判断

当前不实现 RCS。Agent 根据任务完成情况、关键结论的来源、未解决问题、信息增量和硬预算判断继续或停止。RCS 只有在未来有真实评测数据时才可能作为可插拔辅助能力加入。

## 当前仓库状态

仓库仍包含原 Deep Research 架构的 Orchestrator、Planner DAG、AgentPool、Summarizer、Evidence Store 和 Evidence Graph 等代码。它们是迁移来源，不是目标架构。

目前已经具备：

- 搜索、论文、网页、文件、计算等研究工具；
- 模型与配置适配；
- Langfuse tracing；
- LangGraph 最小单线程入口、checkpointer 和线程身份；
- CLI、Web、评测以及旧研究链路。

下一步是实现新的单个同质 Research AgentGraph。新路径通过对应验收后，再逐步删除无调用者的旧模块。

## 快速开始

### 安装

```bash
git clone https://github.com/qiqihezh/deepresearch-agent.git
cd deepresearch-agent

uv venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.template .env
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
```

### Web

```bash
python web/run.py
```

### 单次研究

```bash
python scripts/run_single.py --query "分析 AI Agent Memory 的演进、评测方法与关键证据"
```

### 交互式研究

```bash
python scripts/run_repl.py
```

### 测试

```bash
pytest -q
```

以上入口目前仍可能运行旧研究链路；切换到新 Workflow 属于实施计划 N5。

## 仓库结构

```text
deepresearch-agent/
├── configs/       # 当前模型、工具和运行配置
├── docs/          # 现行设计、实施计划、路线图和历史记录
├── src/           # 当前实现与后续新 Research AgentGraph
├── web/           # 当前 Web 入口
├── evaluation/    # 评测
├── scripts/       # CLI 与维护脚本
└── tests/         # 自动化测试
```

## 文档

- [文档索引](docs/README.md)
- [目标架构](docs/ARCHITECTURE.md)
- [实施计划](docs/IMPLEMENTATION_PLAN.md)
- [路线图](docs/ROADMAP.md)

## 明确不做

- 不设置不同类型的根 Agent、Manager Agent、Planner Agent 或 Summarizer Agent；
- 不建设 Evidence Graph、证据边或图数据库；
- 不把运行 checkpoint 和持久知识混为一套存储；
- 不在当前路线实现 RCS；
- 不允许超过两层的递归 fork；
- 不为迁就旧实现而复制领域模型、服务或 Repository。

## License

MIT

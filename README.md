<div align="center">

# PaperPilot

### Research Manager 驱动的同质子 Agent 动态 Fork 研究系统

[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](https://python.org)
[![Async](https://img.shields.io/badge/Async-asyncio-orange.svg)](https://docs.python.org/3/library/asyncio.html)

</div>

## 项目定位

PaperPilot 面向需要多方向探索、证据核验和持续补研究的复杂问题。

系统由一个 Research Manager 维护全局研究状态。Manager 根据研究计划动态 fork 多个能力相同、上下文隔离的 Research Agent。子 Agent 分别研究不同范围，将来源、原文证据、原子 Claim、冲突和未解决问题合并回 Evidence Graph。Manager 再根据证据缺口和 Research Completion Score 决定继续 fork，还是停止并生成带引用的研究报告。

```text
Research Question
      ↓
Research Manager
      ↓
Research Plan / Fork Controller
      ↓
同质 Research Agent A ─┐
同质 Research Agent B ─┼→ Evidence Merge → Evidence Graph
同质 Research Agent C ─┘                       ↓
                              Gap Analysis / RCS
                                  ↓         ↓
                              next forks   report
```

这里的 fork 不是普通的“把任务放进 AgentPool”：每个 fork 都应具有独立身份、父子血缘、上下文快照、执行预算、局部状态和生命周期。Worker 同质，研究任务和研究范围可以不同。

完整定义见 [目标架构](docs/ARCHITECTURE.md)，迁移步骤见 [实施计划](docs/IMPLEMENTATION_PLAN.md)。

## 核心设计

### Research Manager 控制面

- 生成和维护研究计划；
- 决定哪些方向需要 fork；
- 管理并发、递归深度、时间、token 和工具预算；
- 合并子 Agent 的结构化研究贡献；
- 根据 Evidence Graph 缺口和 RCS 决定继续或停止。

### 同质 Research Agent 执行面

所有 Worker 使用同一种 Research Agent 蓝图，都具备搜索、阅读、分析、比较、验证和证据提取能力。

`discover`、`compare`、`verify`、`fill_gap`、`challenge` 是研究模式，不是固定角色。子 Agent 之间隔离消息、工具状态和 scratchpad，只通过明确的 Evidence/Contribution 契约共享成果。

### Evidence-first 研究主干

```text
SourceDocument → EvidenceSpan → Claim → EvidenceRelation
```

- EvidenceSpan 必须能够定位回来源原文；
- Claim 是原子、可判真的研究主张；
- Embedding 只召回候选关系；
- SUPPORTS、CONTRADICTS、EXTENDS 需要进一步验证；
- 最终报告消费经过验证和预算筛选的 EvidencePackage，而不是所有 Agent 对话。

### 图驱动 Research Loop

每轮贡献合并后，系统检查：

- 主题覆盖是否充分；
- 是否缺少独立来源；
- 是否存在未解决矛盾；
- 最近一轮是否仍有有效信息增量；
- 继续研究的预期收益是否高于成本。

缺口可以由 Gap Analyzer 或子 Agent 以 ForkProposal 提出，但只能由中央 Fork Controller 审批和创建新 fork。

## 当前实现状态

当前仓库已经具备可运行的 Deep Research 主链路，并完成了部分 PaperPilot 能力：

- Planner DAG 与分层并发执行；
- 同质 Researcher Worker 池；
- 网页、论文、文件、计算和代码工具；
- SQLite 共享记忆与会话隔离；
- Evidence 提取、存储和 Evidence Graph；
- Gap Analysis 与动态追加研究任务；
- 报告证据索引、关系展示和图谱；
- FastAPI + SSE Web UI；
- Obsidian Vault 自动/手动导出；
- Red-Blue 对抗优化和评测工具。

但当前“动态 fork”仍主要表现为向 DAG 追加 SubTask，再从 AgentPool 获取临时 Worker。独立 Agent 身份、上下文隔离、fork 血缘、持久生命周期、结构化 Contribution、受控递归和完整 RCS 尚待实现。因此当前状态应称为：

> Evidence-centric Deep Research 原型 + 动态 Fork 迁移中的执行框架。

准确进度见 [ROADMAP](docs/ROADMAP.md)。

## 当前运行流程

```text
Web 澄清研究问题
→ Planner 生成 DAG
→ Orchestrator 分层调度 Researcher
→ Researcher 调用搜索/论文/网页等工具
→ Memory 写入中间结果
→ Evidence Extractor 提取证据
→ Evidence Graph 建立关系
→ Gap Analyzer 追加补研究任务
→ Summarizer 生成报告
→ ChatStore 持久化
→ Web 展示并导出 Obsidian Vault
```

该流程将在实施计划中逐步迁移为 `Research Manager → Fork Controller → ResearchContribution → Evidence Merge → RCS`。

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

Windows PowerShell 激活环境：

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

## 仓库结构

```text
deepresearch-agent/
├── configs/               # 模型、Agent、工具和运行预算配置
├── docs/
│   ├── ARCHITECTURE.md     # 目标架构事实来源
│   ├── IMPLEMENTATION_PLAN.md
│   └── ROADMAP.md
├── src/
│   ├── core/              # 初始化和研究入口
│   ├── orchestrator/      # 当前状态机、调度和 Research Loop
│   ├── planner/           # 研究任务 DAG
│   ├── agents/            # Researcher、Gap Analyzer、Summarizer
│   ├── evidence/          # 证据、关系图和 Obsidian 导出
│   ├── memory/            # 会话与长期记忆
│   ├── compressor/        # 上下文压缩
│   ├── adversarial/       # Red-Blue 报告优化
│   ├── evolution/         # 实验性自进化模块，尚未接入主流程
│   ├── models/            # 模型路由
│   └── tools/             # 搜索、阅读、计算等工具
├── web/                   # FastAPI、SSE 和单页前端
├── evaluation/            # ResearchBench、HotpotQA 和评测指标
├── scripts/               # CLI、评测、消融和维护脚本
└── tests/
```

## 文档导航

- [目标架构](docs/ARCHITECTURE.md)：系统边界、组件、数据契约和主流程；
- [实施计划](docs/IMPLEMENTATION_PLAN.md)：从当前代码迁移到目标架构的具体阶段；
- [路线图](docs/ROADMAP.md)：当前完成度和下一阶段优先级；
- [阶段 0 基线](docs/STAGE_0_BASELINE.md)：重构起点、提交和验证结果；
- [原始产品设计入口](docs/PaperPilot_Development_Plan.md)；
- [基于现有 DeepResearch 的改造入口](docs/PaperPilot_DeepResearch_Based_Development_Plan.md)。

## 非目标与边界

- 项目不采用多个固定人格或固定专家角色作为核心机制；
- 对象池复用不等同于 Agent fork；
- 生成了报告不等于完成了可验证研究；
- 自进化模块目前是实验性能力，不是在线主流程的一部分；
- 本地 Web 默认按单用户工具设计，外网部署前需要补充鉴权、任务队列和安全边界。

## License

MIT

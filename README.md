<div align="center">

# 🔬 PaperPilot

### 统一对话 × 递归 Deep Research × LLM Wiki × Obsidian

把一次性研究，沉淀为可追溯、可问答、可继续生长的长期知识。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![LangGraph](https://img.shields.io/badge/Orchestration-LangGraph-1f6feb.svg)](https://www.langchain.com/langgraph)
[![Tests](https://img.shields.io/badge/tests-pytest-blue.svg)](#-测试)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

---

## 📖 项目介绍

大多数研究 Agent 在交付报告后就结束了：下一次提问仍要从头搜索，历史证据散落在会话中，也很难在自己熟悉的知识管理工具里继续整理。

**PaperPilot** 是一个基于 LangGraph 的个人深度研究系统。统一对话入口先区分普通聊天、当前 Memory 问答、快速联网和深度研究；研究任务由用户确认计划后，再由同质 Research Agent 递归拆解问题、并行检索和汇聚证据。结果写入长期 Markdown Memory，并通过 **LLM Wiki** 与 **Obsidian** 进入后续问答、笔记和继续研究流程。

```text
统一入口：消息 → 聊天 / Memory / Quick Web / 深度研究提案
一次研究：Brief 确认 → 递归研究 → 带来源的报告
长期积累：报告 → 用户确认整理为 Wiki → Memory 问答 → 新笔记 / 继续研究
```

## ✨ 四项核心能力

| 能力 | PaperPilot 做什么 | 解决的问题 |
|---|---|---|
| 💬 **统一对话编排** | 自动路由普通聊天、Memory 问答、最多三来源的快速联网和深度研究提案，并保留显式模式覆盖 | 每句话都误入昂贵研究流程 |
| 🔎 **递归 Deep Research** | 同一种 Research Agent 按需 fork，搜索网页、论文和本地资料，汇聚可追溯证据 | 复杂问题难以一次检索完整 |
| 🧠 **LLM Wiki** | 将报告整理为持续更新、逐条引用 Evidence 的 Wiki 专题页，支持 Wiki 优先问答与基于旧知识继续研究 | 多份研究难以融合为当前知识 |
| 🗂️ **Obsidian 原生工作流** | 使用 Markdown、frontmatter 和 WikiLink 落盘，可直接在 Obsidian 中阅读、编辑和浏览双链 | 知识被锁在聊天界面或专有数据库中 |

### 1. 统一对话编排

- Web 默认入口会区分普通聊天、当前 Memory 问答、快速联网和深度研究提案；
- 用户也可以显式指定模式，跳过自动意图判断；
- 普通聊天、Memory 问答和快速联网不会启动 Research AgentGraph；
- CLI 与 Rubric 评测保持为独立研究入口，不依赖 Web 对话编排层。

### 2. 递归 Deep Research

- 用户可以修改并确认 Research Brief，确认前不会启动正式研究；
- 未选择 Memory 也能先审阅 Brief；确认启动时自动创建并绑定 managed Memory；
- 根 Agent、子 Agent 和孙 Agent 运行同一个 AgentGraph，只在身份、深度和预算上不同；
- Agent 根据证据缺口决定继续调用工具、停止或 fork，整棵执行树共享递归、线程、工具和时间预算；
- 搜索、论文、网页、文件和计算结果最终汇聚为带来源的 Markdown 报告；

### 3. LLM Wiki：让研究成为长期记忆

PaperPilot 的 LLM Wiki 不是另一个 Markdown 编辑器，而是建立在可追溯 Research Memory 之上的知识层：

- 一个 Vault 可以包含多个稳定的 `memory_id`，Memory 不依赖某次 session 或 thread；
- 报告、Wiki、证据、来源、笔记和导入资料通过 WikiLink 形成知识网络；
- 已完成报告可以由用户手动创建或更新为 Wiki 专题页，不自动触发第二次模型整理；
- Wiki 整理只调用一次模型；模型输出结构化 Claim，路径、frontmatter 和 WikiLink 由程序生成；
- 用户预览后才保存；保存时重新校验报告哈希、页面哈希和 Evidence ID；
- Memory 问答优先检索 Wiki，尚未建立或没有命中 Wiki 时兼容检索原有报告、Evidence 与笔记；
- 可以把回答整理为笔记，或导入 PDF、文本和网页；所有写入都先预览、再确认；
- 新研究会读取已有结论与知识缺口，在同一 Memory 中继续扩展；
- SQLite FTS5、可选本地语义召回和 WikiLink 邻居组成混合检索，索引可随时从 Markdown 重建。

#### LLM Wiki 三层

```text
Raw / Source：原始附件、工具 Artifact 和来源定位，不由 Wiki 生成过程改写
Evidence / Report：可定位的原子证据与单次研究快照
Wiki：用户触发、LLM 辅助维护的当前专题知识
```

Schema/Governance 横跨三层，约束页面身份、路径、引用和写入边界。Report 保留历史，Wiki 持续更新；两者共享 Evidence，但研究完成时不会自动修改 Wiki。

### 4. Obsidian 原生，而不是自建编辑器

PaperPilot 负责研究、检索、引用和受控写入；Obsidian 负责阅读、手工编辑、backlinks 与知识图谱。Vault 中的 Markdown 是唯一知识真相源，PaperPilot 不要求安装 Obsidian 插件，也不会改写 `.obsidian/`。

```text
memory/
└── Memories/
    └── M-.../
        ├── Home.md
        ├── reports/
        ├── evidence/
        ├── sources/
        ├── wiki/
        ├── notes/
        ├── imports/
        └── attachments/
```

## 🔄 完整使用链路

```mermaid
flowchart LR
    A[Web 统一对话入口] --> B{Conversation Orchestrator}
    B -->|普通聊天| C1[直接回答]
    B -->|快速联网| C2[最多 3 个来源]
    B -->|Memory 问答| C3[当前 Memory 检索]
    B -->|深度研究| C[修改并确认计划]
    C --> C4[未绑定则自动创建 Memory]
    CLI[CLI: run_repl / run_single] --> D[Research Runtime]
    EVAL[Rubric Eval] --> D
    C4 --> D
    D --> D1[Research Agent 递归研究]
    D1 --> E[汇聚证据与来源]
    E --> F[生成 Markdown 报告]
    F --> G[Obsidian 阅读与双链]
    F --> K[用户选择新建或更新 Wiki]
    K --> L[一次模型生成结构化草稿]
    L --> M[校验 Evidence 并预览保存]
    M --> H[Wiki 优先的 Memory 问答]
    F --> H
    H --> I[确认保存笔记]
    H --> J[基于知识缺口继续研究]
    J --> D
```

这条链路的重点不是“生成一篇报告”，而是让报告进入一个可以持续检索、验证和扩展的个人研究空间。

## 🚀 快速开始

推荐 Python 3.11。

### 1. 安装

```bash
git clone https://github.com/boluodaixue/paperpilot.git
cd paperpilot

python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.template .env
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.template .env
```

### 2. 配置环境和 API

编辑项目根目录的 `.env`。按默认的 [模型配置](configs/default.yaml)完成网页研究前，把以下两个占位值替换为真实 Key：

| 用途 | `.env` 变量 | 默认选择 |
|---|---|---|
| 研究模型 | `DEEPSEEK_API_KEY` | `configs/default.yaml` 中的 `model.backend_mapping.research: deepseek` |
| 网页搜索 | `TAVILY_API_KEY` | `SEARCH_BACKEND=tavily` |

部分服务的 Key 申请入口和完整变量示例见 [`.env.template`](.env.template)。`your_..._key_here` 只是占位值，不能直接用于调用 API。模型名、Base URL 和搜索源的默认值已写在模板中；使用默认 DeepSeek 与 Tavily 时通常只需替换上述两个 Key。

想使用其他模型时，在 `.env` 中填写对应的 `OPENAI_*`、`MIMO_*` 或 `VLLM_*` 连接信息，并将 `configs/default.yaml` 的 `model.backend_mapping.research` 改为 `openai`、`mimo` 或 `vllm`。本地 vLLM 还需要先启动与所填 `VLLM_BASE_URL` 对应的服务。`DEFAULT_LLM_BACKEND` 只影响没有显式指定后端的调用；正常研究流程会优先采用 `model.backend_mapping.research`。运行带模型评分的评测时，可另行调整 `model.backend_mapping.judge`。

其他 API 均为可选：`METASO_API_KEY`、`EXA_API_KEY`、`BOCHA_API_KEY`、`SERPAPI_KEY` 用作网页搜索备用源；`SEMANTIC_SCHOLAR_API_KEY` 和 `OPENALEX_API_KEY` 可用于论文检索；`LANGFUSE_PUBLIC_KEY` 与 `LANGFUSE_SECRET_KEY` 仅在开启 `LANGFUSE_TRACING=true` 时用于追踪。`OPENALEX_EMAIL` 是可选联系邮箱，并非 API Key。不使用这些服务时，保持相应变量注释即可。

程序从当前工作目录依次读取 `.env`、`.env.local`，后者的同名变量优先。两者都被 Git 忽略；如需本机覆盖，可另外创建 `.env.local`。启动命令应在项目根目录执行。

网页搜索默认使用 `Tavily → 秘塔 → Exa → 博查 → SerpAPI` 回退链，只调用已配置 Key 的备用源，无需部署 SearXNG 或其他常驻服务。Quick Web 最多打开三个可读来源；没有获得可用文档时返回证据不足，不会转入深度研究或写入 Memory。

论文检索把 `ARXIV_READER_BACKEND` 作为首选项，并在失败或空结果时自动在 arXiv、Semantic Scholar 和 OpenAlex 之间回退。裸 arXiv ID 会先按 arXiv 标识符查询，不会再误拼为 OpenAlex Work ID。外部 HTTPS 工具统一使用平台信任库与 Mozilla CA；网页返回 403 时，只会尝试同一发布方可验证的官方 PDF，不关闭证书校验或绕过访问控制。

### 3. 启动 Web

```bash
python web/run.py
```

打开 <http://127.0.0.1:8000>。默认输入框会自动区分普通对话、当前 Memory
问答、快速联网和深度研究提案；也可以显式选择“只查当前 Memory”“联网查一下”
或“深度研究”。深度研究执行和 Memory 写入仍需确认。

### 4. 整理报告到 Wiki

在 Web 中完成一份绑定到 managed Memory 的研究后：

1. 点击报告下方的“整理到 Wiki”；
2. 选择“新建 Wiki 页面”或一个已有页面；
3. 生成并检查完整 Markdown 预览；
4. 点击“保存到 Wiki”。

系统会自动沿报告中的 WikiLink 读取相关 Evidence。模型不能自行创建 Evidence ID；报告或目标 Wiki 在预览后发生变化时，保存会被拒绝，避免覆盖 Obsidian 中的并发编辑。

### 5. 在 Obsidian 中打开

把 `configs/default.yaml` 中 `research.vault_root` 指向的目录作为 Obsidian Vault 打开。默认目录是项目下的 `memory/`。

## 💻 命令行使用（可选）

除 Web 界面外，也可以从终端发起研究。先在 Web 中创建 Memory，再将下面的 `M-your-memory` 替换为实际 ID；运行后按提示确认研究提案。

```bash
python scripts/run_single.py --memory-id M-your-memory --query "分析 AI Agent Memory 的演进、评测方法与关键证据"
```

交互式终端支持连续使用，输入 `new-memory 专题名称` 可创建并选择 Memory：

```bash
python scripts/run_repl.py
```

## 📊 Benchmark 与评测

先完成模型与搜索服务配置；真实评测会调用模型和检索服务，可能产生费用。以下命令在项目根目录执行，结果默认保存到 `outputs/evaluation/`。

### 公开 benchmark

- **HotpotQA**：多跳问答评测，运行 `python scripts/run_eval.py --benchmark hotpotqa --num-questions 2`。
- **DeepResearch Bench II**：基于公开任务与 Rubric 完成 10 题子集评测，得分 **42.70%**。详见 [实验说明](experiments/drbench2/README.md)、[评测结果与口径](experiments/drbench2/user-multi-holdout-20260906/当前10题口径.md)及 [官方榜单](https://agentresearchlab.com/benchmarks/deepresearch-bench-ii/index.html#leaderboard)。

本项目采用 10 题子集与 DeepSeek Judge，结果为自定义评测成绩，不与官方全量榜单直接比较。

## 🏗️ 架构概览

```mermaid
flowchart TB
    WEB[Web] --> ORCH[Conversation Orchestrator]
    ORCH --> CHAT[普通聊天]
    ORCH --> MQA[Memory 问答]
    ORCH --> QUICK[Quick Web]
    ORCH --> PROPOSAL[确认后的深度研究]
    PROPOSAL --> RT[Research Runtime]
    CLI[CLI: run_repl / run_single] --> RT
    EVAL[Rubric Eval] --> RT
    RT --> WF[LangGraph Research Workflow]
    WF --> CP[(SQLite Checkpointer)]
    WF --> AG[Homogeneous Research AgentGraph]
    AG -->|按需 fork| AG
    AG --> TOOLS[Search / Paper / Web / File / Compute]
    QUICK --> TOOLS
    WF --> WRITER[Durable Queue + Single Vault Writer]
    UI --> WIKI[Report → Wiki Preview]
    WIKI --> WRITER
    WRITER --> VAULT[(Markdown Vault)]
    VAULT --> OBS[Obsidian]
    VAULT --> RETRIEVAL[FTS5 / Semantic / WikiLink Retrieval]
    RETRIEVAL --> WF
    RETRIEVAL --> UI
```

几个关键设计：

- **工作流可恢复**：LangGraph checkpoint 持久化研究阶段和 interrupt，服务重启后可继续等待中的流程；
- **写入可恢复**：持久队列与单一 Vault Writer 串行提交，通过 staging、journal、内容哈希和原子发布处理崩溃与重复请求；
- **知识不锁定**：SQLite 检索数据只是可重建索引，不能反向覆盖 Markdown；
- **人在回路中**：研究计划、Wiki 更新、保存笔记、导入资料和 legacy 迁移均需要用户确认。

更完整的设计说明见 [架构文档](docs/ARCHITECTURE.md)。

## 🧭 项目演进

| 阶段 | 主要变化 |
|---|---|
| DeepResearch Agent 原型 | 建立规划、检索、Memory、报告与评测基础 |
| PaperPilot 研究闭环 | 加入证据层、证据图、Web UI、动态 fork 和 Obsidian 导出探索 |
| LangGraph 主线重构 | 收敛为同质 Research AgentGraph，补齐确认、递归边界与 checkpoint 恢复 |
| LLM Wiki + Obsidian | 完成长期 Memory、受控笔记与导入、崩溃一致写入、全文与混合检索 |
| 研究充分性与终止机制 | 根据研究要求、已有证据与下一步价值决定继续、重规划或停止，分别记录研究状态、停止原因与报告状态 |
| 三层研究上下文 | 加入 L1–L3 上下文管理、语义摘要与原始资料受控回读，控制长任务上下文规模 |
| 统一对话 | 接通普通聊天、Memory 问答、快速联网与研究提案，确认研究时可自动创建 Memory |
| 用户驱动 Wiki | 增加 Report → Wiki 单次模型整理、Claim 级 Evidence 引用、预览保存与 Wiki 优先问答 |
| Benchmark 评测 | 完成 DeepResearch Bench II 公开基准子集测试，发布评测适配代码与结果汇总 |

## 🧪 测试

```bash
python -m pip install -e ".[dev]"
pytest -q
```

测试覆盖研究流程与预算、故障恢复、知识库读写与检索，以及网页和命令行入口；结果以当前版本的实际运行输出为准。

## 🛠️ 技术栈

| 层级 | 技术 |
|---|---|
| Agent 工作流 | LangGraph |
| Web | FastAPI + SSE |
| 模型 | DeepSeek / OpenAI / MiMo / vLLM |
| 知识存储 | Markdown + frontmatter + WikiLink |
| 工作流持久化 | SQLite Checkpointer |
| 检索 | SQLite FTS5 + sentence-transformers + WikiLink |
| 可观测性 | Langfuse |
| 知识管理 | Obsidian |

## 📁 项目结构

```text
paperpilot/
├── configs/          # 模型、工具、Runtime 与检索配置
├── docs/             # 当前架构与路线图
├── evaluation/       # 评测题集、评分与结果汇总
├── scripts/          # CLI、评测和模型准备工具
├── src/conversation/ # 对话路由、Quick Answer、PriorEvidence 产品适配
├── src/research/     # Workflow、AgentGraph、Memory、Wiki、Writer、Retrieval
├── src/tools/        # 搜索、论文、网页、文件与计算工具
├── tests/            # 确定性测试和故障注入
└── web/              # FastAPI + 本地 Web UI
```

## 📄 License

[MIT](LICENSE)

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
→ 可选 Red/Blue 单次报告后处理（默认关闭）
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
├── Memories/
│   └── M-.../
│       ├── Home.md
│       ├── reports/
│       ├── evidence/
│       ├── sources/
│       ├── notes/
│       ├── imports/
│       └── attachments/
├── reports/       # 旧根目录只读兼容识别
├── evidence/
└── sources/
```

每个长期 Memory 使用稳定 `memory_id` 定位。报告通过完整 Vault 相对 WikiLink 链接证据，证据链接论文、网页或文件来源；导入资料的原始内容、可定位提取和整理笔记分别进入 `attachments/`、`imports/` 和 `notes/`。关系只由 Markdown WikiLink 和 Obsidian backlinks 表达，不建设独立 Evidence Graph。

Research FileReader 默认不可用。每次运行只授权当前 managed Memory（受控调用方可另行绑定本次上传目录），模型只看到虚拟相对路径；绝对路径、路径逃逸、symlink/junction/reparse、读取竞态、非法类型和超限内容都会被拒绝。

### 简单完成判断

当前不实现 RCS。Agent 根据任务完成情况、关键结论的来源、未解决问题、信息增量和硬预算判断继续或停止。RCS 只有在未来有真实评测数据时才可能作为可插拔辅助能力加入。

## 当前仓库状态

N0–N6 已完成。CLI、Web 和评测统一通过 `src/research/runtime.py` 进入同一个 Research Workflow；旧 Orchestrator、Planner DAG、AgentPool、独立 Summarizer、Evidence Store/Graph 和旧实验体系已经退出代码库。

目前已经具备：

- 搜索、论文、网页、文件、计算等研究工具；
- 模型与配置适配；
- Langfuse tracing；
- LangGraph 最小单线程入口、checkpointer 和线程身份；
- 新的单个同质 Research AgentGraph：根/子共用同一图，支持工具循环、结构化结果、来源证据、硬停止和线程隔离；
- N2 根工作流：研究说明、用户反复修改/确认、checkpoint 恢复、最终 Markdown 报告和单一 Memory Store；
- N3 一级同质并行 fork：三种触发条件、依赖和预算门槛、父子上下文/实例/身份隔离、并行汇聚与部分失败保留；
- N4 有界递归：根→子→孙、祖先去重、总线程/工具/时间/token/重试限制、共享 saver 下的取消与恢复；
- N5 生产入口：CLI/Web 用户确认与恢复、SSE 事件回放、结构化评测结果和架构回归测试。
- N6 可选报告审查：原报告持久化后的单次 Red/Blue、确定性动作重放、结构保护与失败降级，默认关闭。
- W0–W6 LLM Wiki + Obsidian：长期多 Memory、Obsidian 打开、记忆辅助继续研究、当前 Memory 问答与受控笔记、PDF/文本/显式 URL 的受控导入，以及固定会话 Memory、legacy 只读迁移和离线评测。

N6 已完成验收：关键专项与回归为 `65 passed, 1 warning`，全量回归为 `160 passed, 1 warning`；全量中的 warning 是既有 `StarletteDeprecationWarning`。

### 可选报告审查

N6 只处理已经成功持久化的最终 Markdown 报告，由 `research.report_review.enabled` 控制且默认关闭。开启时，系统复用本次运行的同一个 policy 做一次 Red 审查和一次 Blue 修订；这两次调用不暴露工具、不 fork，也不创建新的 LangGraph 线程。

- Red 只输出事实性、逻辑一致性、引用质量三类结构化问题；
- Blue 只允许 `ADD`、`DELETE`、`MODIFY`、`VERIFY` 四种动作；
- 根工作流按顺序确定性重放 Blue 动作，重放结果必须与 Blue 返回的完整 Markdown 一致；
- YAML frontmatter、WikiLink target 与次数、URL 的值与次数以及 manifest 必须保持不变；WikiLink alias 可调整；
- 任一生成、解析、修订、校验或写回失败，都交付原始已持久化报告。

该能力不是新的 Research Agent 角色、RCS 或评分引擎，也不引入 claim-evidence 新模型或第二套存储。详细边界见 [N6 实施记录](docs/N6_OPTIONAL_REPORT_REVIEW.md)。

LLM Wiki + Obsidian 主线已完成：Obsidian 负责 Markdown 阅读、编辑和双链，PaperPilot 负责多 Memory、检索对话、受控写入和继续研究。默认使用一个 Vault 和多个长期 Memory，不自建复杂阅读器。W0–W6 均已按既定计划单独验收和提交。

- [LLM Wiki + Obsidian 目标架构](docs/LLM_WIKI_OBSIDIAN_ARCHITECTURE.md)
- [LLM Wiki + Obsidian 实施计划](docs/LLM_WIKI_OBSIDIAN_IMPLEMENTATION_PLAN.md)
- [W0 实施记录](docs/W0_MEMORY_VAULT_CONTRACT.md)
- [W1 实施记录](docs/W1_MULTI_MEMORY_PERSISTENCE.md)
- [W2 实施记录](docs/W2_OBSIDIAN_MINIMAL_INTEGRATION.md)
- [W3 实施记录](docs/W3_CONTINUE_RESEARCH_FROM_MEMORY.md)
- [W4 实施记录](docs/W4_MEMORY_QA_CONTROLLED_NOTES.md)
- [W5 实施记录](docs/W5_CONTROLLED_IMPORTS.md)
- [W6 实施记录](docs/W6_STABILIZATION_MIGRATION_AND_ENTRY.md)
- [S0 实施记录](docs/S0_FILE_READER_SANDBOX.md)
- [S0–S5 生产化与检索升级计划](docs/S_PRODUCTION_HARDENING_AND_RETRIEVAL_PLAN.md)

W6 专项为 `39 passed, 1 warning`，原 N1–N6 回归为 `160 passed, 1 warning`，N1–N6 + W0–W5 前序集合为 `411 passed, 1 warning`，包含 W6 的仓库全量回归为 `450 passed, 1 warning`；固定离线 `memory_wiki` 评测为 `5/5 passed`。pytest warning 为既有 `StarletteDeprecationWarning`。

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

### 首次连接 Obsidian

1. 在 Obsidian 中选择“打开本地文件夹作为仓库”，手工打开 `configs/default.yaml` 中 `research.vault_root` 指向的目录；默认是项目下的 `memory/`。
2. 如需按 Vault 名称生成链接，可在 `research` 下显式配置 `vault_name`，其值必须与 Obsidian 显示的 Vault 名称一致；省略时 PaperPilot 使用绝对路径 URI。
3. Web 顶部可以选择或新建 Memory，再用“在 Obsidian 中打开”定位其 `Home.md`。PaperPilot 不安装或检测 Obsidian，也不写 `.obsidian/`；未安装 Obsidian 不影响研究和 Markdown 持久化。

Web 还可以显式切换到“Memory 问答”，回答只使用当前 Memory 并提供可在 Obsidian 中打开的引用。选择“保存回答为笔记”后，PaperPilot 会先展示完整 Markdown 提案；只有用户确认后才新建笔记并受控更新对应 `Home.md`，外部编辑冲突不会被静默覆盖。

Web 的“导入资料”支持用户明确选择的 PDF/UTF-8 文本、直接粘贴文本和显式 URL。PaperPilot 先生成 Import/Note 完整预览，用户确认后才把原始附件、可定位提取结果和整理笔记成组写入当前 Memory。URL 仅在生成预览时读取，且经过公网地址、重定向、大小、超时和内容类型校验。

W6 会把每个 Web 会话永久绑定到第一次显式使用的 `memory_id`，不从历史报告推断或自动切换。既有根目录 `reports/evidence/sources` 以只读 `M-legacy` 暴露，可问答但不能研究、保存或导入；“迁移既有 Memory”会先展示 Home 和每篇转换后 Markdown 的完整预览，确认后原子发布一个 managed 副本，根文件和旧 Chat 指针保持不变，当前会话也不会自动切换。

### 单次研究

```bash
python scripts/run_single.py --memory-id M-your-memory --query "分析 AI Agent Memory 的演进、评测方法与关键证据"
```

W6 要求单次研究显式提供已有、可写的 `--memory-id M-...`；缺省值和 `M-legacy` 都不会进入写入流程。PaperPilot 会从该 Memory 的最新 Markdown 中确定性筛选相关旧笔记，并在 Research Brief 中列出命中文件、已知信息和新研究空白；确认后新报告仍写回同一 Memory。研究完成后 CLI 会输出 Vault、Memory `Home.md`、Obsidian URI 和报告路径。自动化场景可以显式增加 `--yes`；默认流程一定先展示研究说明并等待确认。

### 交互式研究

```bash
python scripts/run_repl.py
```

REPL 使用 `memories`、`use M-...` 或 `new-memory <title>` 明确选择 Memory；`ask`、`import-file`、`import-text`、`import-url` 和普通研究问题都复用同一 Runtime。`migrate-legacy <target-id> <title>` 会显示完整迁移预览并等待确认。

固定离线 Memory 闭环评测：

```bash
python scripts/run_eval.py --benchmark memory_wiki
```

### 测试

```bash
pytest -q
```

## 仓库结构

```text
deepresearch-agent/
├── configs/       # 当前模型、工具和运行配置
├── docs/          # 现行设计、实施计划、路线图和历史记录
├── src/research/  # 唯一 Research Workflow、同质 AgentGraph 与 Markdown Memory
├── web/           # 新 Workflow 的本地 Web 入口
├── evaluation/    # 评测
├── scripts/       # CLI 与维护脚本
└── tests/         # 自动化测试
```

## 文档

- [文档索引](docs/README.md)
- [目标架构](docs/ARCHITECTURE.md)
- [实施计划](docs/IMPLEMENTATION_PLAN.md)
- [路线图](docs/ROADMAP.md)
- [N1 实施记录](docs/N1_HOMOGENEOUS_AGENT_GRAPH.md)
- [N2 实施记录](docs/N2_CONFIRMATION_AND_MEMORY.md)
- [N3 实施记录](docs/N3_HOMOGENEOUS_PARALLEL_FORK.md)
- [N4 实施记录](docs/N4_RECURSION_LIMITS_AND_RECOVERY.md)
- [N5 实施记录](docs/N5_ENTRY_MIGRATION_AND_LEGACY_CLEANUP.md)
- [N6 实施记录](docs/N6_OPTIONAL_REPORT_REVIEW.md)
- [W0 实施记录](docs/W0_MEMORY_VAULT_CONTRACT.md)
- [W1 实施记录](docs/W1_MULTI_MEMORY_PERSISTENCE.md)
- [W2 实施记录](docs/W2_OBSIDIAN_MINIMAL_INTEGRATION.md)
- [W3 实施记录](docs/W3_CONTINUE_RESEARCH_FROM_MEMORY.md)
- [W4 实施记录](docs/W4_MEMORY_QA_CONTROLLED_NOTES.md)
- [W5 实施记录](docs/W5_CONTROLLED_IMPORTS.md)
- [W6 实施记录](docs/W6_STABILIZATION_MIGRATION_AND_ENTRY.md)

## 明确不做

- 不设置不同类型的根 Agent、Manager Agent、Planner Agent 或 Summarizer Agent；
- 不建设 Evidence Graph、证据边或图数据库；
- 不把运行 checkpoint 和持久知识混为一套存储；
- 不在当前路线实现 RCS；
- 不允许超过两层的递归 fork；
- 不为迁就旧实现而复制领域模型、服务或 Repository。

## License

MIT

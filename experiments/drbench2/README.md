# DeepResearch Bench II 公开子集评测

> 当前主汇报口径为排除题 25 和题 42 后的自选 10 题子集，按 75% 信息召回、17% 分析、8% 呈现加权得到 **42.70%**。完整定义见 [当前 10 题口径](user-multi-holdout-20260906/当前10题口径.md)，12 题原始汇总见 [最终结果](user-multi-holdout-20260906/最终结果.md)。

本目录是独立实验适配层，使用 `9e81efe` 的只读代码快照，不修改主项目或开发分支。

仓库公开独立适配/计分代码、冻结配置、题目选择和精简运行汇总。原始报告、逐次模型响应、数据库、日志、检查点、上游数据副本及只读源码快照体积较大或可能包含运行上下文，仅保存在本地，由本目录 `.gitignore` 排除。

## 固定输入

- 上游：<https://github.com/SawyerCooper/DeepResearchBench2>
- 提交：`440bdc33728438d317ca7860809942b5a1e40256`
- 原始任务 132 道，中英文各 66 道、22 个主题；选择 6 个最大主题，每主题中英文各一道。
- 哈希种子和选择规则在 `selection.json`，具体题目见 [题目清单](题目清单.md)。
- 原固定方案包含调试题 25、12 及 10 道 holdout；最终主口径排除题 25、42，保留 12、16、11、49、69、72、57、54、87、86。
- 研究只读取 `research_inputs` 的原始 prompt 和禁止来源说明；评分端读取 `judge_inputs`。
- 上游一个任务没有 blocked 字段，保持官方空值语义，不擅自补造参考来源。

## 评分口径

从官方代码 AST 提取 PROMPT_TEMPLATE，不执行下载的评测代码。保留官方任务、Rubric 文本和三值评分规则。
严格检查每项返回、文字匹配、唯一性、分数、理由和支撑片段字段；每批最多两次尝试。失败不计成 0 分或完整成功。
聚合采用官方代码的条目通过率：score=1 的数量除以全部条目数，0 和 -1 均留在分母，另外报告 blocked_rate。

目前 Judge 使用项目已配置的模型（运行记录中注明），不是官方 Gemini Judge；还采用每批 12 项而非官方默认 50 项。
因此所有结果必须标为 **开发子集试评，非官方榜单分数**。

## 调试配置与对照边界

当前 v3 两组相同模型、工具、总预算上限；single 硬限制 fork depth=0、总线程=1，multi 上限 6。
调试限额：300k estimated tokens、研究 240 秒、Root 合成额外 120 秒、总工具调用 48。
Root 输出预算 32k，Child 初始租约 40k，单请求超时 120 秒。不同于产品 700k 基线，不能把本轮成绩宣传为产品最终效果。
历史 v1/v2 使用 120k、180+90 秒、3 个线程上限和 45 秒请求超时；不足以保证初始 4—5 方向的分工，也出现请求超时。
v1 另有包装器位置参数错误，该轮作为适配失败保存；v2 修复检索后用于诊断。不同配置结果不可合并成对照均分。
两组调试阶段独立生成计划；并发启动也可能竞争服务端容量，故本轮不用于架构优劣推断。
正式对照前需固定公共计划或明确将计划方差纳入端到端设计，并采用配对交替运行与必要重复。

工具统一为 acquire_evidence、file_reader、calculator、notepad。禁用任意代码执行和宿主目录读取；允许文件根仅为当前运行 Artifact 目录。
研究走新 Headless Core，因此本实验本身也是新入口适配检查，不代表现有 Web/CLI 已全部迁移。

## 禁止来源与局限

研究提示保留官方禁止参考文章规则。搜索候选和网页结果按已知 URL/标题过滤；aiohttp 请求及重定向做 URL 检查。
同时过滤已知基准答案地址；对未知镜像、换标题转载、API 服务端内部获取无法提供绝对保障。应审查 guard 日志与最终引用。
网络 guard 是适配器局部运行保护，不应作为生产网络层的通用安全实现复用。
引用导出只将已知 Evidence 的标记映射到真实 HTTP(S) 来源，未知标记保留为 unresolved，不捏造链接。

## 复现说明

适配器绑定 PaperPilot 源码提交 `9e81efed413b86152cfcff372e379b350bfa9e38` 与 DeepResearch Bench II 上游提交 `440bdc33728438d317ca7860809942b5a1e40256`。完整在线重跑会调用模型和检索服务并产生费用；先按 [完整实验方案](formal-v1/完整实验方案.md) 准备对应源码与公开上游数据，再执行预检和批处理入口：

```powershell
.\.venv\Scripts\python.exe -B experiments/drbench2/prepare.py
.\.venv\Scripts\python.exe -B -m unittest discover -s experiments/drbench2 -p "test_*adapter.py" -q
.\.venv\Scripts\python.exe -B experiments/drbench2/user-multi-holdout-20260906/preflight_holdout.py
.\.venv\Scripts\python.exe -B experiments/drbench2/user-multi-holdout-20260906/holdout_multi_runner.py
```

研究目录不覆盖；重试需使用新的 `--run` 名称。未配置凭据时应先在项目私有环境配置，不向终端输出 Key。
现有 source snapshot 可用 Git `archive 9e81efe` 重新构建，上游文件校验值见 upstream_manifest.json。

## 每次运行的产物

- run.json：运行、报告、研究、评分分别记状态。
- plan.json：只有公开任务生成的研究计划。
- result.json / report.md：原始结构化结果及可读报告。
- model_calls.json：每次调用耗时、供应商返回 Token 统计；无真实账单时费用为空。
- adapter_snapshot.py：后续新运行保存执行时的适配器；本次早期调试仅记录哈希，变更原因见本轮结论。
- blackboard.sqlite / checkpoint.sqlite：该次隔离的协作及恢复状态。
- source_guard.json：禁止来源拦截记录。
- judge_batches / judge_progress.json / judge.json：原始评分响应、阶段结果、完整汇总。

## 结果边界

先确认有报告、合法引用可导出、单 Agent thread_count=1、所有 Rubric 条目都有有效评分，并明确任何超时/回退。
如果只得到占位回答或 fallback，就定位失败原因，不以调用成功代替端到端通过。
当前公开结果是自选 10 题子集与项目 DeepSeek Judge 的自定义评测，不是官方排行榜成绩，也不能代替全量 132 题结果。原 35 道自建 ResearchBench 与该公开 Benchmark 相互独立。

Windows 文本换行会在读写时规范化，因此官方模板完整性校验使用原文件字节，而非读取后的字符串重新编码。
报告还做独立交付检查：工具协议文本、过短正文、fallback、无证据及未知引用不能因为核心返回 valid 就视为可用报告。
“过短”在本适配器中暂定为少于 500 字符，是开发检查规则，不是官方基准评分标准。

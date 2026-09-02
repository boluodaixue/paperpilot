# Research Agent 底座设计与状态（2026-09-03）

## 冻结点与分支边界

- 纯净同质递归分支：`codex/homogeneous-recursive-fork`
- 架构提交：`17c382a`（`feat: rebuild homogeneous recursive research baseline from main`）
- 直接父提交：`main@7ecebd6`
- 原混合历史存档：`archive/homogeneous-recursive-on-supervisor@9ce3351`
- 纯 Supervisor 分支：`codex/supervisor-worker-v2@510ea9d`
- 本分支历史不包含 Supervisor 提交 `510ea9d`；底座代码以一个新提交直接应用在 `main` 之上。
- 为保持已经验证的运行行为和测试兼容性，文件树保留了非默认的 Supervisor/固定计划实验实现；它们是复制进来的源码，不是本分支祖先，也不接入当前默认生产链路。

当前窗口只负责冻结和记录底座，不修复无效 Fork，也不重新运行科技题评测。后续修改与三次评测必须在新的独立任务中进行。

## 当前生产设计

1. Dynamic Planner 根据 Research Brief 现场生成约 4–5 个研究方向，不读取固定问题计划。
2. Root 把外部证据方向交给同质 Child；Child 可以在预算允许时继续 Fork Grandchild。
3. Blackboard 只记录 Plan、Assignment、Agent 状态、Coverage gap、Query/Source fingerprint、队列和等待事件，用于方向协调、查漏补缺和避免重复研究；动态生产运行不把 Evidence 写入 Blackboard。
4. Child 自主搜索、判断材料价值，并返回只覆盖自己方向的简短 `research_memo`，使用真实 `[[EVIDENCE:id]]` 标记。
5. Root 汇总 Child memo、识别缺漏并直接生成最终 Markdown 报告。动态生产路径不经过 Evidence Selector、Claim inventory、Semantic Verifier、独立 Composer 或语义 Citation Audit。
6. 运行时只对已知 Evidence 标记做确定性引用渲染；未知标记被移除但正文保留，不伪造引用。
7. `configs/default.yaml` 的默认研究架构为 `legacy`，即上述同质递归 Agent Graph；Supervisor/固定计划路径只作为非默认实验对照保留。

## 当前模型与容量参数

- Research Agent 温度：`0.3`
- Judge 温度：`0.1`
- Child 单次输出：`4096 tokens`
- Root 输入上限：`60000 chars`
- Root 单次最终输出：`32768 tokens`
- Root 累计最终输出预算：`50000 tokens`
- Root 最终格式：直接 Markdown，不再把全文放入 JSON。
- 模型适配器保留 API `finish_reason`；若 Root 因 `length` 停止，则从报告尾部继续生成并拼接，直到自然结束或累计最终输出预算耗尽。
- 真实运行必须完整加载 `D:\Claude\deepresearch-agent\.env`（`override=True`），研究与 Judge 均使用其中配置的火山方舟 DeepSeek v4 Flash；不要使用系统环境中的 DeepSeek 配置。

## 当前有效结果

### Root 输出修复的同 checkpoint 对照

来源研究状态：`outputs/evaluation/researchbench-root-agent-tech-two-r6-temp03/checkpoint.sqlite`。该对照只重放 Root，没有重新规划、搜索或运行 Child。

- tech_001：同一批 4 个 Child、328 条 Evidence；旧 JSON/4k Root 报告 Judge `3.4`，直接 Markdown Root 重放后 Judge `8.2`；报告 9,505 字符、16 个引用来源、`output_status=valid`、0 次续写。
- tech_002：同一批 5 个 Child、201 条 Evidence；旧 JSON/4k Root 报告 Judge `6.0`，直接 Markdown Root 重放后 Judge `6.2`；报告 10,560 字符、13 个引用来源、`output_status=valid`、0 次续写。
- 平均 Judge：`7.2`。
- 结果：`outputs/evaluation/researchbench-root-agent-tech-two-r7-root-replay/ResearchBench_RootReplay_60K_DirectMarkdown_20260903_035535.json`

### 历史参照边界

- r4 动态 Root 直写科技题：tech_001 `7.6`、tech_002 `6.8`，平均 `7.2`。
- 财经题 `8.8` 使用不同题目，不作为这两道科技题的分数基线。
- 新建 checkpoint 后得到的 tech_001 `5.0` 属于重新规划与重新搜索的独立轨迹，不能用于判断 55k/60k Root 输入修改本身。

## 测试状态

- `17c382a` 的文件树与此前已测试架构提交 `9fa07c1` 的文件树哈希完全一致：`00a0e7633729d43dab0493cae468032162d4ed57`。
- 该文件树此前的受影响专项测试为 69 条通过。
- 纯净重建后再次全量回归：1,095 passed、3 skipped；同一个与本次架构无关的 60ms Vault lease 心跳时序用例首次因调度抖动失败，隔离复跑为 1 passed。
- 纯净重建没有改变代码内容，只改变提交拓扑；本地回归结果与冻结前一致。

## 已知剩余问题：无效 Fork

现有调度在派发子任务时只要求分得的 token 数大于 0，没有确认该额度足以完成一次完整研究循环。已观察到 Grandchild 创建并占用名额后，只完成一次控制判断，在 7–11 秒内结束，产生 0 查询、0 Evidence、0 memo；父 Child 又已把该方向视为委派，从而形成覆盖缺口。

下一步只增加派发前资源预检：必须确认候选 Child/Grandchild 获得的时间、token 和工具额度足以完成“控制判断 → 搜索/阅读 → 方向 memo”。若不足，不创建 Assignment、不占 Agent 名额，任务保留给当前 Agent 本地研究。该修改不得增加领域规则、Evidence Selector、Verifier、Composer 或新的语义检查机制。

## 新窗口执行任务

1. 从包含本文件的最新提交开始，不修改已经冻结的 Root/Child/Blackboard 职责边界，也不调整分支历史。
2. 实现无效 Fork 的派发前预算预检，并增加确定性测试：预算不足时不启动子 Agent、父 Agent保留任务；预算充足时保持现有 Fork 行为。
3. 完成相关回归和全量测试。
4. 使用原始 `.env`，以当前参数全新、顺序运行一次 `tech_001` 和一次 `tech_002`，使用独立 checkpoint 并完成 Judge。
5. 再使用另一个全新 checkpoint 单独运行第二次 `tech_001` 并完成 Judge。
6. 对比两次 tech_001 的动态计划、Child/Grandchild 分工、零产出 Agent、查询/来源成功率、Evidence、Child memo、Root 输入、`finish_reason`、续写次数、报告长度、引用来源数和 Judge 四维分数。
7. 先报告数据和原因；除无效 Fork 外，不根据单轮分数继续修改架构。

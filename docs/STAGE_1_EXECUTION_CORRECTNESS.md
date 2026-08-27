# 阶段 1：执行正确性（实施计划中的 Phase 0）

日期：2026-08-27

## 目标

在引入 fork 领域实体和 Fork Controller 前，修复现有执行路径中会造成跨 Worker 状态污染、资源泄漏、错误成功状态和失真评测的问题，为后续同质子 Agent 动态 fork 建立可靠基线。

## 已完成

### Policy 调用隔离

- `VLLMPolicy` 的 tools、messages 和 `was_truncated` 改为单次调用状态；
- Policy 支持从模板创建调用级隔离实例；
- `ModelRouter` 缓存无运行态污染的 Policy 模板；
- 三 Worker 并发测试覆盖 tools、messages 和截断状态互不影响。

### Agent 生命周期

- `AgentPool` 按精确 Agent 类型回收，不再混淆 search、analyze 和 verify；
- 重复释放保持安全，不重复进入空闲池；
- synthesize Agent 的获取、使用和释放纳入完整生命周期；
- 一次 Research Run 结束后不会遗留活跃 Agent。

### 超时降级

- 全局超时且已有成功结果时，进入真实的降级合成；
- 降级路径跳过 Red-Blue 对抗流程，避免超时后继续扩大开销；
- 全局超时且没有成功结果时明确失败，不生成伪成功报告。

### 检索与工具容错

- `Researcher` 的 SEARCH 模式必须达到有效来源门槛后才能返回成功；
- 工具调用失败支持配置化重试；
- 重试耗尽后使用确定的固定 fallback，避免不可预测地切换执行策略。

### 评测修复

- HotpotQA 从生成报告中提取短答案；
- 不再将报告标题误当作模型答案参与评测。

## Langfuse 边界

Langfuse 继续作为旁路 tracing：用于观察研究、Agent、工具、检索和 LLM 调用，但其配置缺失、SDK 错误或上报失败不得改变业务执行结果。

本阶段沿用 [阶段 0.5 Langfuse 基线](STAGE_0_5_LANGFUSE.md)，没有引入第二套 tracing provider，也没有验证真实 Langfuse 云端上报。

## 验收结果

专项联合测试：

```text
24 passed
```

全量测试：

```text
173 passed
```

验收覆盖：

- 三 Worker 并发运行时 tools、messages 和截断状态隔离；
- AgentPool 精确回收、重复释放和 synthesize 生命周期；
- 有结果与无结果两种全局超时路径；
- SEARCH 有效来源门槛；
- 工具重试配置与固定 fallback；
- HotpotQA 短答案提取。

测试均使用本地可控输入验证；本阶段没有声称真实联网模型调用或 Langfuse 云端链路已经验证。

## 明确未做

阶段 1 没有实现：

- `ForkSpec`；
- `AgentFork`；
- `ForkRepository`；
- `ForkController`。

现有并发执行仍是 DAG 调度与 AgentPool Worker 复用，不应描述为已经完成真正的动态 Agent fork。

## 下一阶段

下一阶段是“阶段 2：Fork 领域模型（实施计划 Phase 1）”。范围包括 fork 数据契约、生命周期、SQLite Repository、幂等写入、重启恢复，以及现有 `SubTask`/`AgentResult` 的兼容适配。

`ForkController` 将在 fork 领域模型稳定后进入后续实现，不在本阶段完成范围内。

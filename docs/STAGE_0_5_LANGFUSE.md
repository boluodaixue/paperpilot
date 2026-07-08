# 阶段 0.5：Langfuse 可观测性基线

日期：2026-08-27

## 为什么在动态 Fork 前完成

ResearchRun、AgentFork、工具调用、Evidence Merge 和 RCS 都需要共享同一条可追踪父子链。如果在 ForkController 完成后才接入 tracing，就必须反向修改每个生命周期边界，并且难以验证并发上下文是否串线。

因此项目在阶段 1 正确性重构前，先把旧 tracing provider 替换为 Langfuse，后续新增组件只需要沿用统一适配层。

## 技术选择

- Langfuse Python SDK v4；
- OpenTelemetry 上下文传播；
- Langfuse OpenAI drop-in client 记录 LLM generation；
- `observe` 支持 Agent、Tool、Chain 和 Retriever observation；
- `propagate_attributes` 传播 session、tag 和 fork metadata；
- SDK 异常、配置缺失或网络失败不得影响研究结果。

参考官方文档：

- [Langfuse Python SDK](https://langfuse.com/docs/observability/sdk/overview)
- [Instrumentation](https://langfuse.com/docs/observability/sdk/instrumentation)
- [OpenAI Python Integration](https://langfuse.com/integrations/model-providers/openai-py)
- [Python v3 → v4](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4)

## 实现

`src/utils/tracing.py` 是唯一 provider 适配层，业务代码继续使用：

```text
trace_agent
trace_tool
trace_chain
trace_retriever
trace_block
trace_context
```

新增：

- `create_openai_client`：启用时选择 Langfuse OpenAI client；
- `flush_tracing`：供 CLI 短进程在返回前发送后台队列；
- `shutdown_tracing`：供应用关闭时释放 SDK；
- metadata/tag 清洗；
- 配置不完整和 SDK 异常时的无追踪降级。

当前 trace 层级：

```text
research.run                       chain
└── orchestrator.run               chain
    ├── planner.generate_plan      chain
    ├── researcher.run             agent
    │   ├── OpenAI completion      generation
    │   └── tool.<name>            tool
    ├── memory.put/query           retriever
    ├── compressor.compress        chain
    ├── summarizer.run             agent
    └── adversarial_loop.run       chain
```

ForkController 实现后，每个 `AgentFork` 将成为 `research.run` 下的 agent observation，并传播：

```text
runid
forkid
parentforkid
plannodeid
attempt
```

## 配置

```dotenv
LANGFUSE_TRACING=false
LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key
LANGFUSE_SECRET_KEY=sk-lf-your-secret-key
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=development
LANGFUSE_SAMPLE_RATE=1.0
LANGFUSE_OBSERVE_DECORATOR_IO_CAPTURE_ENABLED=false
```

只有开关为 `true` 且公私钥齐全时才启用。

普通函数默认不捕获完整输入输出，避免上传大型上下文、模块实例或内部数据。LLM prompt 和 completion 由 Langfuse OpenAI 集成记录；如果部署场景不允许上传研究内容，应保持 tracing 关闭或使用符合数据策略的自托管实例。

## 依赖迁移

- 删除 `langsmith` 依赖声明；
- 从项目虚拟环境卸载 LangSmith；
- 增加 `langfuse>=4.0.0,<5.0.0`；
- 当前验证版本为 Langfuse `4.14.5`。

## 验证

专项测试：

```text
10 passed
```

全量回归：

```text
142 passed
```

验证覆盖：

- tracing 关闭时为零业务侵入的 no-op；
- 请求开启但缺少密钥时自动禁用；
- Agent/Chain observation 类型映射；
- 同步和异步装饰器；
- session、tag 和 metadata 传播；
- 业务异常不会被 tracing 上下文吞掉；
- trace block 输出和状态更新；
- Langfuse OpenAI drop-in client 选择；
- 移除 LangSmith 后全项目测试通过。

## 下一阶段

进入阶段 1“执行正确性”：Policy 调用状态隔离、Agent 生命周期、超时降级、工具重试、配置生效和评测修复。

Langfuse 本阶段只提供稳定 tracing 基础，不在此阶段引入 ForkController、线上评测、Prompt Management 或复杂 Dashboard。

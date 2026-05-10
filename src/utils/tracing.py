"""
LangSmith 追踪集成模块

为 DeepResearch Agent 提供可观测性支持，无需引入 LangChain/LangGraph 依赖。
核心能力：
  1. LLM 调用自动追踪（通过 wrap_openai 包装 VLLMPolicy 的 client）
  2. Agent 流程手动埋点（通过 @traceable 装饰关键方法）
  3. 环境变量控制开关（LANGSMITH_TRACING=true/false）

用法：
  1. 在 .env 中配置 LangSmith 环境变量
  2. 系统会自动检测并开启追踪
  3. 登录 https://smith.langchain.com 查看 trace 树

设计原则：
  - 零侵入：业务代码不感知追踪存在
  - 可开关：通过环境变量一键启用/禁用
  - 低成本：禁用时不创建任何 LangSmith 对象
"""
from __future__ import annotations

import functools
import logging
import os
from typing import Any, Callable, TypeVar


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环境检测
# ---------------------------------------------------------------------------


def is_tracing_enabled() -> bool:
    """检查 LangSmith 追踪是否启用。"""
    from .env_config import get_env
    return get_env("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# OpenAI Client 包装（自动追踪所有 LLM 调用）
# ---------------------------------------------------------------------------


def maybe_wrap_openai_client(client: Any) -> Any:
    """包装 OpenAI 客户端以启用 LangSmith 自动 LLM 追踪。

    Args:
        client: 原始的 openai.OpenAI 实例。

    Returns:
        包装后的 client（追踪开启时）或原始 client（追踪关闭时）。
    """
    if not is_tracing_enabled():
        return client
    try:
        from langsmith.wrappers import wrap_openai
        return wrap_openai(client, chat_name="ChatOpenAI")
    except Exception as e:
        logger.warning(f"wrap_openai 失败，回退到原始 client: {e}")
        return client


# ---------------------------------------------------------------------------
# 兼容装饰器（支持 sync / async / class method）
# ---------------------------------------------------------------------------


def traceable(
    run_type: str = "chain",
    name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Callable:
    """兼容装饰器：如果 LangSmith 开启则使用 @traceable，否则为无操作装饰器。

    支持同步函数、异步函数、类方法。

    Args:
        run_type: LangSmith run 类型。常用值：
                  "chain"   — 通用流程块
                  "llm"     — LLM 调用（wrap_openai 已自动处理，一般不需要手动标）
                  "tool"    — 工具调用
                  "agent"   — Agent 执行
                  "retriever" — 检索操作
        name: 在 LangSmith UI 中显示的名称。为 None 时使用函数名。
        tags: 标签列表，用于筛选和分组。
        metadata: 附加元数据字典。

    用法示例：
        @traceable(run_type="agent", tags=["m5", "red"])
        async def attack(self, report): ...
    """
    def decorator(func: Callable) -> Callable:
        # 如果追踪未开启，直接返回原函数（零开销）
        if not is_tracing_enabled():
            return func

        try:
            from langsmith import traceable as _ls_traceable
            # 使用 LangSmith 原生装饰器
            return _ls_traceable(
                run_type=run_type,
                name=name or func.__name__,
                tags=tags or [],
                metadata=metadata or {},
            )(func)
        except Exception as e:
            logger.warning(f"[LangSmith] traceable 装饰器应用失败 ({func.__name__}): {e}")
            return func

    return decorator


# ---------------------------------------------------------------------------
# 手动追踪上下文（用于不便装饰器的场景）
# ---------------------------------------------------------------------------


def trace_block(
    name: str,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    tags: list[str] | None = None,
):
    """上下文管理器：手动包裹一段代码块。

    用法示例：
        with trace_block("adversarial_loop", run_type="chain", inputs={"query": q}) as run:
            report = await loop.run(report)
            run.add_output({"score": report.final_score})
    """
    if not is_tracing_enabled():
        # 返回一个 dummy context manager
        class _DummyRun:
            def add_output(self, outputs: dict) -> None:
                pass
        from contextlib import contextmanager
        @contextmanager
        def _dummy():
            yield _DummyRun()
        return _dummy()

    try:
        from langsmith.run_helpers import trace
        return trace(name=name, run_type=run_type, inputs=inputs or {}, tags=tags or [])
    except Exception as e:
        logger.warning(f"[LangSmith] trace_block 创建失败: {e}")
        from contextlib import contextmanager
        @contextmanager
        def _dummy():
            class _DummyRun:
                def add_output(self, outputs: dict) -> None:
                    pass
            yield _DummyRun()
        return _dummy()


# ---------------------------------------------------------------------------
# 快捷装饰器（按场景预配置）
# ---------------------------------------------------------------------------


def trace_agent(name: str | None = None, tags: list[str] | None = None):
    """Agent 执行追踪（run_type="chain"，LangSmith 不支持 "agent"）。"""
    return traceable(run_type="chain", name=name, tags=tags)


def trace_tool(name: str | None = None, tags: list[str] | None = None):
    """工具调用追踪（run_type="tool"）。"""
    return traceable(run_type="tool", name=name, tags=tags)


def trace_chain(name: str | None = None, tags: list[str] | None = None):
    """通用流程追踪（run_type="chain"）。"""
    return traceable(run_type="chain", name=name, tags=tags)


def trace_retriever(name: str | None = None, tags: list[str] | None = None):
    """检索操作追踪（run_type="retriever"）。"""
    return traceable(run_type="retriever", name=name, tags=tags)

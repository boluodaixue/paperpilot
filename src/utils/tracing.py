"""Langfuse 可观测性适配层。

业务模块只依赖本文件提供的稳定接口，不直接依赖 Langfuse SDK：

- ``trace_agent`` / ``trace_tool`` / ``trace_chain`` / ``trace_retriever``
- ``trace_block``：手动 observation 上下文
- ``trace_context``：传播 run/session/fork 等关联属性
- ``create_openai_client``：按开关选择 Langfuse OpenAI drop-in client
- ``flush_tracing`` / ``shutdown_tracing``：短进程发送与资源释放

设计目标：

1. Langfuse 未启用、配置不完整或 SDK 异常时，研究主流程保持可运行；
2. 默认不捕获普通函数的输入输出，避免把大型上下文和内部对象上传；
3. LLM 调用由 Langfuse 官方 OpenAI drop-in client 记录；
4. 基于 OpenTelemetry 自动保持嵌套 observation 的父子关系；
5. 为后续 ResearchRun / AgentFork 预留 session、tag 和 metadata 传播接口。
"""
from __future__ import annotations

import functools
import inspect
import logging
import re
from contextlib import ExitStack, contextmanager
from typing import Any, Callable

from .env_config import get_env, get_env_bool


logger = logging.getLogger(__name__)

_TRUE_VALUES = {"true", "1", "yes"}
_STATUS_WARNING_EMITTED = False


def _tracing_requested() -> bool:
    """返回用户是否显式请求开启 Langfuse tracing。"""
    return (get_env("LANGFUSE_TRACING", "") or "").lower() in _TRUE_VALUES


def is_tracing_enabled() -> bool:
    """仅在开关开启且公私钥齐全时启用 Langfuse。"""
    global _STATUS_WARNING_EMITTED

    if not _tracing_requested():
        return False

    missing = [
        key
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
        if not get_env(key)
    ]
    if missing:
        if not _STATUS_WARNING_EMITTED:
            logger.warning(
                "Langfuse tracing 已请求，但缺少配置：%s；本次运行禁用 tracing",
                ", ".join(missing),
            )
            _STATUS_WARNING_EMITTED = True
        return False
    return True


def _capture_io_enabled() -> bool:
    """普通 observation 是否捕获函数入参和返回值，默认关闭。"""
    return get_env_bool("LANGFUSE_OBSERVE_DECORATOR_IO_CAPTURE_ENABLED", False)


def _observation_type(run_type: str) -> str:
    """把旧的通用 run_type 映射为 Langfuse observation type。"""
    mapping = {
        "agent": "agent",
        "chain": "chain",
        "tool": "tool",
        "retriever": "retriever",
        "llm": "generation",
        "generation": "generation",
        "embedding": "embedding",
        "evaluator": "evaluator",
        "guardrail": "guardrail",
        "span": "span",
    }
    return mapping.get((run_type or "span").lower(), "span")


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    """清洗为 Langfuse 可传播的短字符串 metadata。"""
    if not metadata:
        return None
    result: dict[str, str] = {}
    for raw_key, raw_value in metadata.items():
        key = re.sub(r"[^A-Za-z0-9]", "", str(raw_key))[:100]
        if not key:
            continue
        result[key] = str(raw_value)[:200]
    return result or None


def _safe_tags(tags: list[str] | None) -> list[str] | None:
    if not tags:
        return None
    cleaned = [str(tag)[:200] for tag in tags if str(tag).strip()]
    return cleaned or None


def create_openai_client(*, base_url: str, api_key: str) -> Any:
    """创建 OpenAI 兼容客户端；启用时使用 Langfuse 官方 drop-in。"""
    if is_tracing_enabled():
        try:
            from langfuse.openai import OpenAI as LangfuseOpenAI

            return LangfuseOpenAI(base_url=base_url, api_key=api_key)
        except Exception as exc:
            logger.warning("Langfuse OpenAI client 初始化失败，回退到原始客户端: %s", exc)

    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key)


def traceable(
    run_type: str = "chain",
    name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    flush_on_exit: bool = False,
) -> Callable:
    """可选 Langfuse 装饰器，兼容同步和异步函数。"""

    def decorator(func: Callable) -> Callable:
        if not is_tracing_enabled():
            return func

        try:
            from langfuse import observe, propagate_attributes
        except Exception as exc:
            logger.warning("Langfuse observe 加载失败（%s）: %s", func.__name__, exc)
            return func

        observation_kwargs = {
            "name": name or func.__name__,
            "as_type": _observation_type(run_type),
            "capture_input": _capture_io_enabled(),
            "capture_output": _capture_io_enabled(),
        }
        propagated = {
            "tags": _safe_tags(tags),
            "metadata": _safe_metadata(metadata),
        }
        propagated = {key: value for key, value in propagated.items() if value is not None}

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                with propagate_attributes(**propagated):
                    return await func(*args, **kwargs)

            observed_async = observe(**observation_kwargs)(async_wrapper)
            if not flush_on_exit:
                return observed_async

            @functools.wraps(func)
            async def async_flush_wrapper(*args, **kwargs):
                try:
                    return await observed_async(*args, **kwargs)
                finally:
                    # 外层 wrapper 保证 observation 已结束后再发送队列。
                    flush_tracing()

            return async_flush_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            with propagate_attributes(**propagated):
                return func(*args, **kwargs)

        observed_sync = observe(**observation_kwargs)(sync_wrapper)
        if not flush_on_exit:
            return observed_sync

        @functools.wraps(func)
        def sync_flush_wrapper(*args, **kwargs):
            try:
                return observed_sync(*args, **kwargs)
            finally:
                flush_tracing()

        return sync_flush_wrapper

    return decorator


class _DummyRun:
    def add_output(self, outputs: dict[str, Any]) -> None:
        return None

    def add_metadata(self, metadata: dict[str, Any]) -> None:
        return None

    def set_error(self, message: str) -> None:
        return None


class _LangfuseRun:
    """保留旧 trace_block 调用方式的轻量适配器。"""

    def __init__(self, observation: Any) -> None:
        self._observation = observation

    def add_output(self, outputs: dict[str, Any]) -> None:
        try:
            self._observation.update(output=outputs)
        except Exception as exc:
            logger.warning("Langfuse output 更新失败: %s", exc)

    def add_metadata(self, metadata: dict[str, Any]) -> None:
        try:
            self._observation.update(metadata=metadata)
        except Exception as exc:
            logger.warning("Langfuse metadata 更新失败: %s", exc)

    def set_error(self, message: str) -> None:
        try:
            self._observation.update(level="ERROR", status_message=str(message)[:1000])
        except Exception as exc:
            logger.warning("Langfuse error 状态更新失败: %s", exc)


@contextmanager
def trace_context(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
):
    """向当前 observation 及其子节点传播研究运行关联信息。"""
    if not is_tracing_enabled():
        yield
        return

    try:
        from langfuse import propagate_attributes
        kwargs = {
            "session_id": session_id,
            "user_id": user_id,
            "trace_name": trace_name,
            "tags": _safe_tags(tags),
            "metadata": _safe_metadata(metadata),
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        propagation = propagate_attributes(**kwargs)
    except Exception as exc:
        logger.warning("Langfuse context 传播失败，继续执行主流程: %s", exc)
        yield
        return

    with propagation:
        yield


def trace_block(
    name: str,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    tags: list[str] | None = None,
):
    """手动创建一个 Langfuse observation 上下文。"""
    if not is_tracing_enabled():
        @contextmanager
        def dummy_context():
            yield _DummyRun()

        return dummy_context()

    @contextmanager
    def langfuse_context():
        stack = ExitStack()
        try:
            from langfuse import get_client, propagate_attributes

            client = get_client()
            observation = stack.enter_context(client.start_as_current_observation(
                    name=name,
                    as_type=_observation_type(run_type),
                    input=inputs if _capture_io_enabled() else None,
                ))
            stack.enter_context(propagate_attributes(tags=_safe_tags(tags)))
        except Exception as exc:
            stack.close()
            logger.warning("Langfuse observation 创建失败，继续执行主流程: %s", exc)
            yield _DummyRun()
            return

        with stack:
            yield _LangfuseRun(observation)

    return langfuse_context()


def flush_tracing() -> None:
    """发送后台队列中的 tracing 数据；CLI 等短进程结束前调用。"""
    if not is_tracing_enabled():
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception as exc:
        logger.warning("Langfuse flush 失败，不影响研究结果: %s", exc)


def shutdown_tracing() -> None:
    """关闭 Langfuse 客户端；仅在应用进程真正退出时调用。"""
    if not is_tracing_enabled():
        return
    try:
        from langfuse import get_client

        get_client().shutdown()
    except Exception as exc:
        logger.warning("Langfuse shutdown 失败: %s", exc)


def trace_agent(name: str | None = None, tags: list[str] | None = None):
    return traceable(run_type="agent", name=name, tags=tags)


def trace_tool(name: str | None = None, tags: list[str] | None = None):
    return traceable(run_type="tool", name=name, tags=tags)


def trace_chain(
    name: str | None = None,
    tags: list[str] | None = None,
    *,
    flush_on_exit: bool = False,
):
    return traceable(
        run_type="chain",
        name=name,
        tags=tags,
        flush_on_exit=flush_on_exit,
    )


def trace_retriever(name: str | None = None, tags: list[str] | None = None):
    return traceable(run_type="retriever", name=name, tags=tags)

"""Langfuse tracing 适配层测试：禁用降级、类型映射、上下文传播和 client 选择。"""
from __future__ import annotations

import asyncio
import functools
from contextlib import contextmanager

import langfuse
import langfuse.openai
import pytest

from src.utils import tracing


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_OBSERVE_DECORATOR_IO_CAPTURE_ENABLED", "false")
    tracing._STATUS_WARNING_EMITTED = False


def test_tracing_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "false")

    def original(value):
        return value + 1

    decorated = tracing.trace_chain(name="disabled")(original)
    assert decorated is original
    assert decorated(1) == 2


def test_requested_tracing_requires_credentials(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    tracing._STATUS_WARNING_EMITTED = False
    assert tracing.is_tracing_enabled() is False


def test_agent_decorator_maps_type_and_propagates_attributes(monkeypatch):
    _enable(monkeypatch)
    observed: dict = {}
    propagated: list[dict] = []

    def fake_observe(**kwargs):
        observed.update(kwargs)

        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **inner_kwargs):
                return func(*args, **inner_kwargs)

            return wrapper

        return decorator

    @contextmanager
    def fake_propagate(**kwargs):
        propagated.append(kwargs)
        yield

    monkeypatch.setattr(langfuse, "observe", fake_observe)
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    @tracing.trace_agent(
        name="research-agent",
        tags=["agent", "fork"],
    )
    def run(value):
        return value * 2

    assert run(3) == 6
    assert observed == {
        "name": "research-agent",
        "as_type": "agent",
        "capture_input": False,
        "capture_output": False,
    }
    assert propagated == [{"tags": ["agent", "fork"]}]


@pytest.mark.asyncio
async def test_async_decorator_preserves_async_execution(monkeypatch):
    _enable(monkeypatch)

    def fake_observe(**kwargs):
        return lambda func: func

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "observe", fake_observe)
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    @tracing.trace_chain(name="async-chain")
    async def run():
        await asyncio.sleep(0)
        return "ok"

    assert await run() == "ok"


def test_trace_context_propagates_run_attributes(monkeypatch):
    _enable(monkeypatch)
    propagated: list[dict] = []

    @contextmanager
    def fake_propagate(**kwargs):
        propagated.append(kwargs)
        yield

    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    with tracing.trace_context(
        session_id="session-1",
        trace_name="paperpilot.research",
        tags=["run"],
        metadata={"fork_id": "fork-1", "depth": 2},
    ):
        pass

    assert propagated == [{
        "session_id": "session-1",
        "trace_name": "paperpilot.research",
        "tags": ["run"],
        "metadata": {"forkid": "fork-1", "depth": "2"},
    }]


def test_trace_context_does_not_swallow_business_exception(monkeypatch):
    _enable(monkeypatch)

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    with pytest.raises(ValueError, match="business failure"):
        with tracing.trace_context(session_id="s"):
            raise ValueError("business failure")


def test_trace_block_updates_observation(monkeypatch):
    _enable(monkeypatch)
    updates: list[dict] = []
    started: list[dict] = []

    class FakeObservation:
        def update(self, **kwargs):
            updates.append(kwargs)

    class FakeClient:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            started.append(kwargs)
            yield FakeObservation()

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "get_client", lambda: FakeClient())
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    with tracing.trace_block("merge", run_type="chain", inputs={"large": "hidden"}, tags=["evidence"]) as run:
        run.add_output({"added": 3})
        run.add_metadata({"round": 1})

    assert started == [{"name": "merge", "as_type": "chain", "input": None}]
    assert updates == [
        {"output": {"added": 3}},
        {"metadata": {"round": 1}},
    ]


def test_create_openai_client_uses_langfuse_drop_in(monkeypatch):
    _enable(monkeypatch)
    sentinel = object()
    calls: list[dict] = []

    def fake_openai(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(langfuse.openai, "OpenAI", fake_openai)
    client = tracing.create_openai_client(base_url="https://example.test/v1", api_key="key")

    assert client is sentinel
    assert calls == [{"base_url": "https://example.test/v1", "api_key": "key"}]


def test_observation_update_failure_does_not_break_business_flow():
    class BrokenObservation:
        def update(self, **kwargs):
            raise RuntimeError("telemetry unavailable")

    run = tracing._LangfuseRun(BrokenObservation())
    run.add_output({"ok": True})
    run.add_metadata({"status": "done"})
    run.set_error("failure")


@pytest.mark.asyncio
async def test_root_chain_flushes_after_observation_finishes(monkeypatch):
    _enable(monkeypatch)
    events: list[str] = []

    def fake_observe(**kwargs):
        def decorator(func):
            @functools.wraps(func)
            async def wrapper(*args, **inner_kwargs):
                events.append("observation-start")
                try:
                    return await func(*args, **inner_kwargs)
                finally:
                    events.append("observation-end")

            return wrapper

        return decorator

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "observe", fake_observe)
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)
    monkeypatch.setattr(tracing, "flush_tracing", lambda: events.append("flush"))

    @tracing.trace_chain(name="root", flush_on_exit=True)
    async def run():
        events.append("business")
        return "ok"

    assert await run() == "ok"
    assert events == ["observation-start", "business", "observation-end", "flush"]

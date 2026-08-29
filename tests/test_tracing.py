"""Langfuse tracing 适配层测试：禁用降级、类型映射、上下文传播和 client 选择。"""
from __future__ import annotations

import asyncio
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
    started: list[dict] = []
    propagated: list[dict] = []
    updates: list[dict] = []

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
        propagated.append(kwargs)
        yield

    monkeypatch.setattr(langfuse, "get_client", lambda: FakeClient())
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    @tracing.trace_agent(
        name="research-agent",
        tags=["agent", "fork"],
    )
    def run(value):
        return value * 2

    assert run(3) == 6
    assert started == [{
        "name": "research-agent",
        "as_type": "agent",
        "input": None,
    }]
    assert propagated == [{"tags": ["agent", "fork"]}]
    assert updates == []


@pytest.mark.asyncio
async def test_async_decorator_preserves_async_execution(monkeypatch):
    _enable(monkeypatch)

    class FakeClient:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            yield object()

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "get_client", lambda: FakeClient())
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    @tracing.trace_chain(name="async-chain")
    async def run():
        await asyncio.sleep(0)
        return "ok"

    assert await run() == "ok"


def test_trace_context_propagates_canonical_thread_attributes(monkeypatch):
    _enable(monkeypatch)
    propagated: list[dict] = []

    @contextmanager
    def fake_propagate(**kwargs):
        propagated.append(kwargs)
        yield

    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    with tracing.trace_context(
        session_id="root-1",
        trace_name="paperpilot.research",
        tags=["run"],
        metadata={
            "thread_id": "root-1",
            "parent_thread_id": None,
            "root_thread_id": "root-1",
            "depth": 0,
        },
    ):
        pass

    assert propagated == [{
        "session_id": "root-1",
        "trace_name": "paperpilot.research",
        "tags": ["run"],
        "metadata": {
            "thread_id": "root-1",
            "root_thread_id": "root-1",
            "depth": "0",
        },
    }]


def test_trace_context_enter_failure_does_not_skip_business(monkeypatch):
    _enable(monkeypatch)
    calls = 0

    class BrokenPropagation:
        def __enter__(self):
            raise RuntimeError("telemetry enter failed")

        def __exit__(self, exc_type, exc, traceback):
            raise RuntimeError("telemetry cleanup failed")

    monkeypatch.setattr(langfuse, "propagate_attributes", lambda **kwargs: BrokenPropagation())

    with tracing.trace_context(metadata={"thread_id": "root-1"}):
        calls += 1

    assert calls == 1


def test_traceable_construction_failures_run_business_once(monkeypatch):
    _enable(monkeypatch)
    calls = 0

    def broken_propagation(**kwargs):
        raise RuntimeError("telemetry construction failed")

    def broken_client():
        raise RuntimeError("telemetry client failed")

    monkeypatch.setattr(langfuse, "propagate_attributes", broken_propagation)
    monkeypatch.setattr(langfuse, "get_client", broken_client)

    @tracing.trace_chain(name="broken-construction", tags=["langgraph"])
    def run():
        nonlocal calls
        calls += 1
        return "ok"

    assert run() == "ok"
    assert calls == 1


def test_traceable_enter_failures_run_business_once(monkeypatch):
    _enable(monkeypatch)
    calls = 0

    class BrokenContext:
        def __enter__(self):
            raise RuntimeError("telemetry enter failed")

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeClient:
        def start_as_current_observation(self, **kwargs):
            return BrokenContext()

    monkeypatch.setattr(langfuse, "propagate_attributes", lambda **kwargs: BrokenContext())
    monkeypatch.setattr(langfuse, "get_client", lambda: FakeClient())

    @tracing.trace_chain(name="broken-enter", tags=["langgraph"])
    def run():
        nonlocal calls
        calls += 1
        return "ok"

    assert run() == "ok"
    assert calls == 1


def test_traceable_exit_failures_run_business_once(monkeypatch):
    _enable(monkeypatch)
    calls = 0

    class FakeObservation:
        def update(self, **kwargs):
            pass

    class BrokenExitContext:
        def __init__(self, value=None):
            self.value = value

        def __enter__(self):
            return self.value

        def __exit__(self, exc_type, exc, traceback):
            raise RuntimeError("telemetry exit failed")

    class FakeClient:
        def start_as_current_observation(self, **kwargs):
            return BrokenExitContext(FakeObservation())

    monkeypatch.setattr(
        langfuse,
        "propagate_attributes",
        lambda **kwargs: BrokenExitContext(),
    )
    monkeypatch.setattr(langfuse, "get_client", lambda: FakeClient())

    @tracing.trace_chain(name="broken-exit", tags=["langgraph"])
    def run():
        nonlocal calls
        calls += 1
        return "ok"

    assert run() == "ok"
    assert calls == 1


def test_traceable_exit_failure_preserves_original_business_exception(monkeypatch):
    _enable(monkeypatch)
    business_error = ValueError("business failure")
    calls = 0

    class FakeObservation:
        def update(self, **kwargs):
            pass

    class BrokenExitContext:
        def __init__(self, value=None):
            self.value = value

        def __enter__(self):
            return self.value

        def __exit__(self, exc_type, exc, traceback):
            raise RuntimeError("telemetry exit failed")

    class FakeClient:
        def start_as_current_observation(self, **kwargs):
            return BrokenExitContext(FakeObservation())

    monkeypatch.setattr(
        langfuse,
        "propagate_attributes",
        lambda **kwargs: BrokenExitContext(),
    )
    monkeypatch.setattr(langfuse, "get_client", lambda: FakeClient())

    @tracing.trace_chain(name="business-error", tags=["langgraph"])
    def run():
        nonlocal calls
        calls += 1
        raise business_error

    with pytest.raises(ValueError) as exc_info:
        run()

    assert exc_info.value is business_error
    assert calls == 1


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

    class FakeClient:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            events.append("observation-start")
            try:
                yield object()
            finally:
                events.append("observation-end")

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "get_client", lambda: FakeClient())
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)
    monkeypatch.setattr(tracing, "flush_tracing", lambda: events.append("flush"))

    @tracing.trace_chain(name="root", flush_on_exit=True)
    async def run():
        events.append("business")
        return "ok"

    assert await run() == "ok"
    assert events == ["observation-start", "business", "observation-end", "flush"]


def test_sdk_flush_failure_does_not_change_business_result(monkeypatch):
    _enable(monkeypatch)
    calls = 0

    class FakeClient:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            yield object()

        def flush(self):
            raise RuntimeError("telemetry flush failed")

    @contextmanager
    def fake_propagate(**kwargs):
        yield

    client = FakeClient()
    monkeypatch.setattr(langfuse, "get_client", lambda: client)
    monkeypatch.setattr(langfuse, "propagate_attributes", fake_propagate)

    @tracing.trace_chain(name="root", flush_on_exit=True)
    def run():
        nonlocal calls
        calls += 1
        return "ok"

    assert run() == "ok"
    assert calls == 1

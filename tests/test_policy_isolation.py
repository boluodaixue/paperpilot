"""Policy 调用与 Agent fork 隔离回归测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

from src.models.model_router import ModelRouter
from src.models.vllm_policy import VLLMPolicy


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        message = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )


def _policy() -> tuple[VLLMPolicy, _Recorder]:
    policy = VLLMPolicy()
    recorder = _Recorder()
    policy.client = recorder
    return policy, recorder


def _tools(name: str) -> list[dict]:
    return [{"type": "function", "function": {"name": name, "parameters": {}}}]


def test_forks_have_independent_identity_and_share_client():
    template = VLLMPolicy(max_input_chars=55000)
    recorder = _Recorder()
    template.client = recorder
    template.set_tools(_tools("default"))

    forks = [template.fork() for _ in range(3)]

    assert len({id(policy) for policy in forks}) == 3
    assert all(policy.client is recorder for policy in forks)
    assert all(policy.max_input_chars == 55000 for policy in forks)
    forks[0].tools[0]["function"]["name"] = "changed"
    assert forks[1].tools[0]["function"]["name"] == "default"
    assert template.tools[0]["function"]["name"] == "default"


def test_model_router_cache_is_only_a_template(monkeypatch):
    ModelRouter.clear_cache()
    monkeypatch.setattr(
        ModelRouter,
        "_load_backend_config",
        staticmethod(lambda _name: {"model_name": "test-model"}),
    )

    first = ModelRouter.create_backend("test")
    second = ModelRouter.create_backend("test")

    assert first is not second
    assert first.client is second.client
    ModelRouter.clear_cache()


def test_interleaved_explicit_tools_do_not_cross_contaminate():
    template, recorder = _policy()
    policies = [template.fork() for _ in range(3)]
    tool_sets = [_tools(f"tool_{index}") for index in range(3)]
    originals = deepcopy(tool_sets)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda item: item[0]([{"role": "user", "content": "hi"}], tools=item[1]),
                zip(policies, tool_sets),
            )
        )

    observed = {call["tools"][0]["function"]["name"] for call in recorder.calls}
    assert observed == {"tool_0", "tool_1", "tool_2"}
    assert all(result["was_truncated"] is False for result in results)
    assert tool_sets == originals
    assert all(policy.tools is None for policy in policies)


def test_truncation_is_reported_per_call_and_does_not_leak():
    policy, _ = _policy()
    long_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "x" * 40000},
    ]

    truncated = policy(long_messages)
    clean = policy([{"role": "user", "content": "short"}])

    assert truncated["was_truncated"] is True
    assert clean["was_truncated"] is False
    assert clean["finish_reason"] == "stop"
    assert policy.was_truncated is False


def test_configured_input_limit_allows_root_synthesis_payload():
    policy = VLLMPolicy(max_input_chars=55000)
    recorder = _Recorder()
    policy.client = recorder

    result = policy([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "x" * 47300},
    ])

    assert result["was_truncated"] is False
    assert len(recorder.calls[0]["messages"][1]["content"]) == 47300


def test_call_does_not_mutate_input_messages_or_tools():
    policy, recorder = _policy()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "old", "arguments": "{}"}}]},
        {"role": "tool", "content": "y" * 40000, "tool_call_id": "call-1", "name": "old"},
    ]
    tools = _tools("search")
    original_messages = deepcopy(messages)
    original_tools = deepcopy(tools)

    policy(messages, tools=tools)

    assert messages == original_messages
    assert tools == original_tools
    assert recorder.calls[0]["messages"] is not messages
    assert recorder.calls[0]["tools"] is not tools

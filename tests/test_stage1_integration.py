"""Stage 1 cross-module acceptance tests for concurrent worker isolation."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from src.agents.researcher import ResearcherAgent
from src.models.vllm_policy import VLLMPolicy
from src.orchestrator.agent_pool import AgentPool
from src.orchestrator.schemas import TaskType


class _ConcurrentRecordingClient:
    """OpenAI-compatible client that records three overlapping local calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._barrier = Barrier(3)
        self._lock = Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self._barrier.wait(timeout=5)
        with self._lock:
            self.calls.append(deepcopy(kwargs))
        message = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool(name: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object"}},
        }
    ]


@pytest.mark.asyncio
async def test_three_concurrent_workers_isolate_policy_calls_and_pool_lifecycle(monkeypatch):
    recorder = _ConcurrentRecordingClient()
    monkeypatch.setattr(
        "src.utils.tracing.create_openai_client",
        lambda **_kwargs: recorder,
    )
    template = VLLMPolicy(model_name="local-test-model")
    pool = AgentPool(policy_factory=template.fork, max_idle=3)

    agents = await asyncio.gather(
        *(pool.get_agent(TaskType.SEARCH) for _ in range(3))
    )
    policies = [agent.policy for agent in agents]

    assert all(isinstance(agent, ResearcherAgent) for agent in agents)
    assert len({id(agent) for agent in agents}) == 3
    assert len({id(policy) for policy in policies}) == 3
    assert all(policy.client is recorder for policy in policies)

    messages = [
        [{"role": "user", "content": "worker-0 short message"}],
        [{"role": "user", "content": "worker-1 " + "x" * 40_000}],
        [{"role": "user", "content": "worker-2 short message"}],
    ]
    tools = [_tool(f"worker_{index}_tool") for index in range(3)]
    original_messages = deepcopy(messages)
    original_tools = deepcopy(tools)

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = await asyncio.gather(
            *(
                loop.run_in_executor(
                    executor,
                    partial(policy, worker_messages, tools=worker_tools),
                )
                for policy, worker_messages, worker_tools in zip(policies, messages, tools)
            )
        )

    calls_by_tool = {
        call["tools"][0]["function"]["name"]: call
        for call in recorder.calls
    }
    assert set(calls_by_tool) == {"worker_0_tool", "worker_1_tool", "worker_2_tool"}
    assert calls_by_tool["worker_0_tool"]["messages"] == original_messages[0]
    assert calls_by_tool["worker_2_tool"]["messages"] == original_messages[2]
    truncated_content = calls_by_tool["worker_1_tool"]["messages"][-1]["content"]
    assert truncated_content.startswith("worker-1 ")
    assert truncated_content.endswith("[CONTENT_TRUNCATED]")
    assert len(truncated_content) < len(original_messages[1][0]["content"])
    assert messages == original_messages
    assert tools == original_tools
    assert [result["was_truncated"] for result in results] == [False, True, False]
    assert [policy.was_truncated for policy in policies] == [False, True, False]

    contaminated_agent = agents[1]
    clean_agent_ids = {id(agents[0]), id(agents[2])}
    await asyncio.gather(*(pool.release_agent(agent) for agent in agents))

    stats = pool.get_stats()[TaskType.SEARCH.value]
    assert stats == {"idle": 2, "active": 0, "created": 3, "degraded": 1}

    recycled = await pool.get_agent(TaskType.SEARCH)
    assert id(recycled) in clean_agent_ids
    assert recycled is not contaminated_agent
    assert recycled.policy.was_truncated is False
    await pool.release_agent(recycled)

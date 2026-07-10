from __future__ import annotations

from collections import deque

import pytest

from src.agents.researcher import ResearcherAgent
from src.core.runner import load_config
from src.orchestrator.agent_pool import AgentPool
from src.orchestrator.schemas import AgentStatus, SubTask, TaskType


def _tool_call(name: str, arguments: str = '{"query": "test", "top_n": 4}') -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class SequencePolicy:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def __call__(self, messages, *, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        return self.responses.popleft()


class LegacyPolicy:
    def __init__(self, response):
        self.response = response
        self.called = False

    def __call__(self, messages):
        self.called = True
        return self.response


class FakeTool:
    def __init__(self, name: str, results):
        self.name = name
        self.results = deque(results)
        self.calls = []

    def get_openai_tool_schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def _task(task_type=TaskType.SEARCH):
    return SubTask(task_id="t1", task_type=task_type, description="research test")


def _valid_web_result():
    return {
        "results": [
            {"title": "Source", "url": "https://example.com", "snippet": "Evidence"}
        ]
    }


def _valid_paper_result():
    return {
        "papers": [
            {"id": "1234.5678", "title": "Paper", "summary": "Evidence"}
        ]
    }


@pytest.mark.asyncio
async def test_search_cannot_succeed_when_model_never_calls_tool():
    policy = SequencePolicy([
        {"content": "answer without evidence", "tool_calls": []},
        {"content": "still no evidence", "tool_calls": []},
    ])
    tool = FakeTool("web_search", [])
    agent = ResearcherAgent("r", policy, [tool], max_turns=2)

    result = await agent.run(_task(), {})

    assert result.status == AgentStatus.FAILED
    assert len(policy.calls) == 2
    assert all(call["tools"] for call in policy.calls)
    assert "No valid source" in policy.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_empty_search_result_cannot_succeed():
    policy = SequencePolicy([
        {"content": "", "tool_calls": [_tool_call("web_search")]},
        {"content": "summary of nothing", "tool_calls": []},
    ])
    tool = FakeTool("web_search", [{"results": []}])
    agent = ResearcherAgent("r", policy, [tool], max_turns=2)

    result = await agent.run(_task(), {})

    assert result.status == AgentStatus.FAILED


@pytest.mark.asyncio
async def test_transient_tool_error_retries_and_succeeds():
    policy = SequencePolicy([
        {"content": "", "tool_calls": [_tool_call("web_search")]},
        {"content": "grounded answer Confidence: 0.9", "tool_calls": []},
    ])
    tool = FakeTool("web_search", [RuntimeError("temporary"), _valid_web_result()])
    agent = ResearcherAgent(
        "r", policy, [tool], max_turns=2,
        tool_max_attempts=2, tool_retry_delay_seconds=0,
    )

    result = await agent.run(_task(), {})

    assert result.status == AgentStatus.SUCCESS
    attempts = [item for item in result.trajectory if item.get("role") == "tool"]
    assert [item["attempt"] for item in attempts] == [1, 2]
    assert len(tool.calls) == 2


@pytest.mark.asyncio
async def test_failed_primary_uses_first_available_fallback_with_mapped_args():
    policy = SequencePolicy([
        {"content": "", "tool_calls": [_tool_call("web_search")]},
        {"content": "paper-backed answer", "tool_calls": []},
    ])
    web = FakeTool("web_search", [{"error": "down"}, {"error": "still down"}])
    arxiv = FakeTool("arxiv_reader", [_valid_paper_result()])
    agent = ResearcherAgent(
        "r", policy, [web, arxiv], max_turns=2,
        tool_max_attempts=2, tool_retry_delay_seconds=0,
        tool_fallbacks={"web_search": ["missing", "arxiv_reader"]},
    )

    result = await agent.run(_task(), {})

    assert result.status == AgentStatus.SUCCESS
    assert arxiv.calls == [{"query": "test", "max_results": 4}]
    fallback = [item for item in result.trajectory if item.get("fallback_from")]
    assert fallback[0]["requested_tool"] == "web_search"
    assert fallback[0]["actual_tool"] == "arxiv_reader"
    assert fallback[0]["attempt"] == 1


@pytest.mark.asyncio
async def test_tool_failure_is_written_back_and_later_round_can_recover():
    policy = SequencePolicy([
        {"content": "", "tool_calls": [_tool_call("web_search")]},
        {"content": "trying again", "tool_calls": [_tool_call("web_search")]},
        {"content": "recovered answer", "tool_calls": []},
    ])
    web = FakeTool("web_search", [{"error": "down"}, _valid_web_result()])
    agent = ResearcherAgent(
        "r", policy, [web], max_turns=3,
        tool_max_attempts=1, tool_retry_delay_seconds=0,
    )

    result = await agent.run(_task(), {})

    assert result.status == AgentStatus.SUCCESS
    assert len(policy.calls) == 3
    second_messages = policy.calls[1]["messages"]
    assert any(message["role"] == "tool" and "down" in message["content"] for message in second_messages)


@pytest.mark.asyncio
async def test_analyze_keeps_pure_analysis_success_and_explicit_empty_tools():
    policy = SequencePolicy([{"content": "analysis Confidence: 0.8", "tool_calls": []}])
    agent = ResearcherAgent("r", policy, [], max_turns=1)

    result = await agent.run(_task(TaskType.ANALYZE), {})

    assert result.status == AgentStatus.SUCCESS
    assert policy.calls[0]["tools"] == []


@pytest.mark.asyncio
async def test_direct_analysis_disables_available_tools_for_that_call():
    policy = SequencePolicy([{"content": "private-context analysis", "tool_calls": []}])
    tool = FakeTool("web_search", [])
    task = SubTask(
        task_id="private",
        task_type=TaskType.ANALYZE,
        description="分析我的朋友是什么样的人",
    )
    agent = ResearcherAgent("r", policy, [tool], max_turns=1)

    result = await agent.run(task, {"query": "请分析我的朋友"})

    assert result.status == AgentStatus.SUCCESS
    assert policy.calls[0]["tools"] == []


@pytest.mark.asyncio
async def test_legacy_policy_without_tools_parameter_remains_supported():
    policy = LegacyPolicy({"content": "analysis", "tool_calls": []})
    agent = ResearcherAgent("r", policy, [], max_turns=1)

    result = await agent.run(_task(TaskType.ANALYZE), {})

    assert result.status == AgentStatus.SUCCESS
    assert policy.called


@pytest.mark.asyncio
async def test_policy_internal_type_error_is_not_mistaken_for_legacy_signature():
    class BrokenPolicy:
        def __init__(self):
            self.calls = 0

        def __call__(self, messages, *, tools=None):
            self.calls += 1
            raise TypeError("internal bug")

    policy = BrokenPolicy()
    agent = ResearcherAgent("r", policy, [], max_turns=1)

    with pytest.raises(TypeError, match="internal bug"):
        await agent.run(_task(TaskType.ANALYZE), {})

    assert policy.calls == 1


@pytest.mark.asyncio
async def test_agent_pool_applies_researcher_kwargs_only_to_researchers():
    kwargs = {
        "tool_max_attempts": 7,
        "tool_retry_delay_seconds": 0.0,
        "tool_fallbacks": {"web_search": ["arxiv_reader"]},
    }
    pool = AgentPool(lambda: LegacyPolicy({}), researcher_kwargs=kwargs)

    agent = await pool.get_agent(TaskType.SEARCH)

    assert agent.tool_max_attempts == 7
    assert agent.tool_fallbacks == kwargs["tool_fallbacks"]


def test_default_execution_config_is_loadable():
    execution = load_config()["tools"]["execution"]

    assert execution["max_attempts"] == 2
    assert execution["retry_delay_seconds"] == 0.25
    assert execution["fallbacks"] == {
        "web_search": ["arxiv_reader"],
        "arxiv_reader": ["web_search"],
        "browser": ["web_search"],
    }

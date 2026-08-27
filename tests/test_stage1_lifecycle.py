from __future__ import annotations

import time

import pytest

from src.agents.researcher import ResearcherAgent
from src.agents.summarizer import SummarizerAgent
from src.orchestrator.agent_pool import AgentPool
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.schemas import (
    AgentResult,
    AgentStatus,
    OrchestratorState,
    ResearchReport,
    RunConfig,
    TaskType,
)


class StubPolicy:
    tools = None

    def __call__(self, messages):
        return {"content": "report\nOverall Confidence: 0.8"}


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", [TaskType.SEARCH, TaskType.ANALYZE, TaskType.VERIFY])
async def test_pool_releases_researchers_to_original_type(task_type):
    pool = AgentPool(StubPolicy)
    agent = await pool.get_agent(task_type)
    assert isinstance(agent, ResearcherAgent)

    await pool.release_agent(agent)

    stats = pool.get_stats()
    assert stats[task_type.value]["active"] == 0
    assert stats[task_type.value]["idle"] == 1
    if task_type != TaskType.SEARCH:
        assert stats.get("search", {}).get("idle", 0) == 0


@pytest.mark.asyncio
async def test_pool_double_release_is_safe():
    pool = AgentPool(StubPolicy)
    agent = await pool.get_agent(TaskType.ANALYZE)

    await pool.release_agent(agent)
    await pool.release_agent(agent)

    assert pool.get_stats()["analyze"] == {
        "idle": 1,
        "active": 0,
        "created": 1,
        "degraded": 0,
    }


@pytest.mark.asyncio
async def test_synthesis_borrows_summarizer_and_does_not_leak():
    policy = StubPolicy()
    pool = AgentPool(StubPolicy)
    orch = Orchestrator(
        planner=None,
        agent_pool=pool,
        summarizer_policy=policy,
    )
    orch._query = "q"
    orch._config = RunConfig(enable_adversarial=False)
    orch._results = [
        AgentResult("t1", AgentStatus.SUCCESS, output="finding", confidence=0.8)
    ]

    assert await orch._do_synthesizing() == OrchestratorState.DONE

    stats = pool.get_stats()
    assert stats["synthesize"]["active"] == 0
    assert stats["synthesize"]["idle"] == 1
    assert isinstance(pool._idle["synthesize"][0], SummarizerAgent)
    assert "analyze" not in stats


class PlannerShouldNotRun:
    def generate_plan(self, query, memory):
        raise AssertionError("planner should not run in timeout test")


def _timed_out_orchestrator(*, results, enable_adversarial=True):
    pool = AgentPool(StubPolicy)
    orch = Orchestrator(
        planner=PlannerShouldNotRun(),
        agent_pool=pool,
        summarizer_policy=StubPolicy(),
    )
    orch._results = list(results)
    original_idle = orch._on_idle

    async def seed_results_then_enter_collecting():
        orch._results = list(results)
        orch._start_time = time.monotonic() - 10
        return OrchestratorState.COLLECTING

    orch._state_handlers[OrchestratorState.IDLE] = seed_results_then_enter_collecting
    orch._config = RunConfig(global_timeout_seconds=1, enable_adversarial=enable_adversarial)
    return orch, pool, original_idle


@pytest.mark.asyncio
async def test_global_timeout_with_success_runs_degraded_synthesis_once():
    results = [AgentResult("t1", AgentStatus.SUCCESS, output="finding", confidence=0.8)]
    orch, pool, _ = _timed_out_orchestrator(results=results)
    synth_calls = 0
    original_synth = orch._do_synthesizing

    async def counted_synthesis():
        nonlocal synth_calls
        synth_calls += 1
        return await original_synth()

    orch._state_handlers[OrchestratorState.SYNTHESIZING] = counted_synthesis
    report = await orch.run("q", orch._config)

    assert orch._current_state == OrchestratorState.DONE
    assert synth_calls == 1
    assert "因全局超时而降级" in report.content
    assert "结果可能不完整" in report.content
    assert pool.get_stats()["synthesize"]["active"] == 0
    assert orch._adversarial_count == 0


@pytest.mark.asyncio
async def test_global_timeout_without_success_fails_without_synthesis():
    results = [AgentResult("t1", AgentStatus.FAILED, output="failed")]
    orch, pool, _ = _timed_out_orchestrator(results=results)

    report = await orch.run("q", orch._config)

    assert orch._current_state == OrchestratorState.FAILED
    assert "global timeout" in report.content
    assert "synthesize" not in pool.get_stats()

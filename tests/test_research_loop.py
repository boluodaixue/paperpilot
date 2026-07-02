"""Research Loop 集成测试：证据按子任务打标 + Gap 分析循环 + 动态 fork。"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from src.evidence.schemas import Evidence
from src.evidence.store import EvidenceStore
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.schemas import (
    AgentResult,
    AgentStatus,
    OrchestratorState,
    RunConfig,
    SubTask,
    TaskType,
)
from src.planner.dag import DAG


class HashEmbedder:
    """确定性伪向量嵌入（避免加载真实模型）。"""

    dim = 384

    def encode(self, text: str) -> list:
        h = hashlib.md5(str(text).encode("utf-8")).digest()
        v = np.frombuffer(h, dtype=np.uint8).astype(np.float32) / 255.0
        return np.resize(v, self.dim).tolist()


class StubPolicy:
    def __call__(self, messages):
        return {"content": "{}"}


def _mk_orch(tmp_path, config=None):
    import time

    store = EvidenceStore(
        db_path=str(tmp_path / "e.db"), embedder=HashEmbedder(), session_id="loop-test"
    )
    orch = Orchestrator(
        planner=None, agent_pool=None, evidence_store=store, summarizer_policy=StubPolicy()
    )
    orch._query = "测试研究问题"
    orch._start_time = time.monotonic()  # 避免 _is_global_timeout 误判
    orch._config = config or RunConfig(
        enable_research_loop=True, max_research_rounds=3, max_total_tasks=10,
        max_gap_tasks_per_round=2,
    )
    orch._dag = DAG()
    return orch, store


def _add_evidence(store: EvidenceStore, topic: str, n: int) -> None:
    for i in range(n):
        ev = Evidence(
            evidence_id="", query="q", paper_id=f"{topic}-{i}", paper_title="t",
            source_url="", claim=f"claim {topic} {i}", evidence_text="e",
            confidence=0.9, topic=topic, session_id=store.session_id,
        )
        store.put(ev)


def _task(description: str, hints=None) -> SubTask:
    return SubTask(
        task_id="x", task_type=TaskType.SEARCH, description=description,
        search_hints=hints or [],
    )


# ---------------------------------------------------------------------------
# 证据按子任务主题打标
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evidence_topic_uses_subtask(tmp_path):
    store = EvidenceStore(
        db_path=str(tmp_path / "e.db"), embedder=HashEmbedder(), session_id="t"
    )

    class StubExtractor:
        max_papers_per_result = 3

        async def extract_from_paper(self, paper, query):
            return [{"claim": f"c {paper['id']}", "evidence_text": "e", "confidence": 0.9}]

        async def fetch_papers(self, query, max_papers):
            return []

    orch = Orchestrator(
        planner=None, agent_pool=None,
        evidence_extractor=StubExtractor(), evidence_store=store,
    )
    orch._query = "主问题"
    orch._task_map = {"t1": _task("子主题一的研究", ["hint1"])}
    orch._results = [
        AgentResult(
            task_id="t1", status=AgentStatus.SUCCESS, output="ok", confidence=0.8,
            trajectory=[{"role": "tool", "result": {
                "papers": [{"id": "p1", "title": "P", "summary": "abstract", "pdf_url": "u"}]
            }}],
        )
    ]
    await orch._extract_evidence()
    evs = store.get_all()
    assert len(evs) == 1
    assert evs[0].topic == "子主题一的研究"[:50]


# ---------------------------------------------------------------------------
# Gap 分析状态机
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collecting_enters_gap_analysis(tmp_path):
    store = EvidenceStore(
        db_path=str(tmp_path / "e.db"), embedder=HashEmbedder(), session_id="t"
    )
    orch = Orchestrator(planner=None, agent_pool=None, evidence_store=store)
    orch._config = RunConfig(enable_research_loop=True)
    orch._results = [AgentResult(task_id="t1", status=AgentStatus.SUCCESS, output="x", confidence=0.8)]
    assert await orch._do_collecting() == OrchestratorState.GAP_ANALYSIS


@pytest.mark.asyncio
async def test_gap_analysis_complete_goes_synthesizing(tmp_path, monkeypatch):
    orch, store = _mk_orch(tmp_path)
    _add_evidence(store, "arch", 5)
    _add_evidence(store, "eval", 5)
    orch._task_map = {"t1": _task("arch")}

    import src.agents.gap_analyzer as ga

    monkeypatch.setattr(ga, "run_gap_analysis",
                        lambda *a, **k: ga.GapPlan(decision="complete", reason="充分"))
    assert await orch._do_gap_analysis() == OrchestratorState.SYNTHESIZING


@pytest.mark.asyncio
async def test_gap_analysis_continue_forks_tasks(tmp_path, monkeypatch):
    orch, store = _mk_orch(tmp_path)
    _add_evidence(store, "arch", 1)  # 不足
    orch._task_map = {"t1": _task("arch")}

    import src.agents.gap_analyzer as ga

    monkeypatch.setattr(ga, "run_gap_analysis",
                        lambda *a, **k: ga.GapPlan(
                            decision="continue", gaps=[{"topic": "arch", "reason": "少"}],
                            new_tasks=[ga.GapTask(description="补搜arch", search_hints=["arch"])],
                            reason=""))
    assert await orch._do_gap_analysis() == OrchestratorState.DISPATCHING
    assert orch._research_rounds == 1
    assert orch._dag.has_node("gap_0_1")
    assert "gap_0_1" in orch._task_map
    assert orch._task_map["gap_0_1"].description == "补搜arch"


@pytest.mark.asyncio
async def test_gap_analysis_respects_total_task_budget(tmp_path, monkeypatch):
    """任务总量预算：即使分析器要补 2 个，也被截到配额。"""
    orch, store = _mk_orch(
        tmp_path,
        RunConfig(enable_research_loop=True, max_research_rounds=3,
                  max_total_tasks=3, max_gap_tasks_per_round=5),
    )
    orch._task_map = {"t1": _task("a"), "t2": _task("b")}  # 已 2 个，配额剩 1
    orch._dag.add_node("t1")
    orch._dag.add_node("t2")

    import src.agents.gap_analyzer as ga

    monkeypatch.setattr(ga, "run_gap_analysis",
                        lambda *a, **k: ga.GapPlan(
                            decision="continue", gaps=[], reason="",
                            new_tasks=[ga.GapTask(description=f"t{i}") for i in range(3)]))
    state = await orch._do_gap_analysis()
    assert state == OrchestratorState.DISPATCHING
    gap_ids = [tid for tid in orch._task_map if tid.startswith("gap_")]
    assert len(gap_ids) == 1  # 只补 1 个（预算内）


@pytest.mark.asyncio
async def test_gap_analysis_saturation_stops(tmp_path, monkeypatch):
    orch, store = _mk_orch(tmp_path)
    orch._research_rounds = 1
    orch._evidence_count_prev = 10
    orch._evidence_count_cum = 10  # delta = 0 → 饱和
    assert await orch._do_gap_analysis() == OrchestratorState.SYNTHESIZING


@pytest.mark.asyncio
async def test_gap_analysis_max_rounds_stops(tmp_path):
    orch, store = _mk_orch(
        tmp_path, RunConfig(enable_research_loop=True, max_research_rounds=2)
    )
    orch._research_rounds = 2
    assert await orch._do_gap_analysis() == OrchestratorState.SYNTHESIZING


# ---------------------------------------------------------------------------
# DISPATCHING：累积结果 + 跳过已完成任务（动态补派的基础）
# ---------------------------------------------------------------------------

class FakeAgent:
    async def run(self, task, context):
        return AgentResult(
            task_id=task.task_id, status=AgentStatus.SUCCESS,
            output=f"out {task.task_id}", confidence=0.8,
        )


class FakePool:
    async def get_agent(self, task_type):
        return FakeAgent()

    async def release_agent(self, agent):
        pass


@pytest.mark.asyncio
async def test_dispatch_accumulates_and_skips_done():
    orch = Orchestrator(planner=None, agent_pool=FakePool(), evidence_store=None)
    orch._config = RunConfig(max_concurrent=2)
    orch._dag = DAG()
    orch._dag.add_node("t1")
    orch._dag.add_node("t2")
    orch._task_map = {
        "t1": SubTask(task_id="t1", task_type=TaskType.SEARCH, description="a"),
        "t2": SubTask(task_id="t2", task_type=TaskType.SEARCH, description="b"),
    }
    assert await orch._do_dispatching() == OrchestratorState.COLLECTING
    assert len(orch._results) == 2
    assert orch._done_task_ids == {"t1", "t2"}

    # 第二轮：新增 gap 任务，只跑新增
    orch._dag.add_node("g1")
    orch._task_map["g1"] = SubTask(task_id="g1", task_type=TaskType.SEARCH, description="c")
    assert await orch._do_dispatching() == OrchestratorState.COLLECTING
    assert len(orch._results) == 3  # 累积
    assert orch._done_task_ids == {"t1", "t2", "g1"}
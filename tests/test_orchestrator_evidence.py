"""Orchestrator 证据提取钩子集成测试（无需 API key）。"""

from __future__ import annotations

import pytest

from src.evidence.graph import EvidenceGraph
from src.evidence.store import EvidenceStore
from src.memory.embedder import Embedder
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.schemas import AgentResult, AgentStatus


class StubExtractor:
    """返回罐头 claims 的桩提取器。"""

    max_papers_per_result = 3

    async def extract_from_paper(self, paper, query):
        return [
            {"claim": f"Claim from {paper['id']}", "evidence_text": "quote", "confidence": 0.9},
            {"claim": "Second claim", "evidence_text": "quote2", "confidence": 0.7},
        ]

    async def fetch_papers(self, query, max_papers):
        return []


def _result(tid: str, status: AgentStatus, papers: list[dict]) -> AgentResult:
    trajectory = [{"role": "tool", "result": {"papers": papers}}] if papers else []
    return AgentResult(task_id=tid, status=status, output="ok" if status == AgentStatus.SUCCESS else "", trajectory=trajectory)


def _paper(pid: str) -> dict:
    return {"id": pid, "title": f"Paper {pid}", "summary": "abstract", "pdf_url": f"http://x/{pid}"}


@pytest.mark.asyncio
async def test_extract_evidence_hook(tmp_path):
    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="hook-test")
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=StubExtractor(),
        evidence_store=store,
    )
    orch._query = "hook test query"
    orch._results = [
        _result("t1", AgentStatus.SUCCESS, [_paper("p1"), _paper("p2")]),
        _result("t2", AgentStatus.FAILED, [_paper("p3")]),  # 失败结果跳过
        _result("t3", AgentStatus.SUCCESS, [_paper("p1")]),  # p1 重复，被 _seen_papers 跳过
    ]

    await orch._extract_evidence()

    all_ev = store.get_all()
    assert len(all_ev) == 4  # 2 篇去重论文 × 2 claims
    assert {e.paper_id for e in all_ev} == {"p1", "p2"}
    assert all(e.topic == "hook test query" for e in all_ev)
    assert [e.evidence_id for e in all_ev] == ["E-1", "E-2", "E-3", "E-4"]
    assert all(e.session_id == "hook-test" for e in all_ev)


@pytest.mark.asyncio
async def test_extract_evidence_no_papers(tmp_path):
    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="hook-test")
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=StubExtractor(),
        evidence_store=store,
    )
    orch._query = "q"
    orch._results = [_result("t1", AgentStatus.SUCCESS, [])]
    await orch._extract_evidence()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_extract_evidence_extractor_raises(tmp_path):
    """提取器抛异常时主流程不受影响。"""

    class BoomExtractor:
        max_papers_per_result = 3

        async def extract_from_paper(self, paper, query):
            raise RuntimeError("boom")

    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="hook-test")
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=BoomExtractor(),
        evidence_store=store,
    )
    orch._query = "q"
    orch._results = [_result("t1", AgentStatus.SUCCESS, [_paper("p1")])]
    # 不应抛出
    await orch._extract_evidence()
    assert store.count() == 0


@pytest.mark.asyncio
async def test_extract_evidence_builds_graph(tmp_path):
    """证据提取同时构建 Evidence Graph 的结构边。"""

    class StubExtractor2:
        max_papers_per_result = 3

        async def extract_from_paper(self, paper, query):
            return [{"claim": f"Claim from {paper['id']}", "evidence_text": "quote", "confidence": 0.9}]

    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="graph-hook")
    graph = EvidenceGraph(
        db_path=str(tmp_path / "e.db"),
        session_id=store.session_id,
    )
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=StubExtractor2(),
        evidence_store=store,
        evidence_graph=graph,
    )
    orch._query = "graph hook query"
    orch._results = [
        _result("t1", AgentStatus.SUCCESS, [_paper("p1"), _paper("p2")]),
    ]
    await orch._extract_evidence()

    stats = graph.graph_stats()
    assert stats["nodes"]["evidence"] == 2
    assert stats["nodes"]["paper"] == 2
    assert stats["edges"]["SOURCED_FROM"] == 2
    assert stats["edges"]["ANSWERS"] == 2


@pytest.mark.asyncio
async def test_extract_evidence_graph_raises_does_not_break(tmp_path):
    """graph.add_evidence 抛异常时主流程不受影响。"""

    class BoomGraph:
        def add_evidence(self, ev, evidence_id=None):
            raise RuntimeError("graph boom")

    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="hook-test")
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=StubExtractor(),
        evidence_store=store,
        evidence_graph=BoomGraph(),
    )
    orch._query = "q"
    orch._results = [_result("t1", AgentStatus.SUCCESS, [_paper("p1")])]
    await orch._extract_evidence()
    # 证据本身照常入库，图异常被吞掉
    assert store.count() == 2


@pytest.mark.asyncio
async def test_extract_evidence_fetches_papers_when_trajectory_empty(tmp_path):
    """轨迹无论文时，按子任务主题补取论文再提取证据。"""

    class FetchExtractor(StubExtractor):
        def __init__(self):
            self.fetched_queries = []

        async def fetch_papers(self, query, max_papers):
            self.fetched_queries.append(query)
            return [_paper("p99"), _paper("p100")]

    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="hook-test")
    extractor = FetchExtractor()
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=extractor,
        evidence_store=store,
    )
    orch._query = "fallback query"
    orch._task_map = {"t1": type("T", (), {"description": "find memory papers", "search_hints": []})()}
    orch._results = [_result("t1", AgentStatus.SUCCESS, [])]  # 空轨迹
    await orch._extract_evidence()
    # 补取的主题应来自任务描述
    assert extractor.fetched_queries == ["find memory papers"]
    # 补取到的 2 篇论文各产出 2 条 claim
    assert store.count() == 4

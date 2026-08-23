"""Orchestrator 证据提取钩子集成测试（无需 API key）。"""

from __future__ import annotations

import pytest

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

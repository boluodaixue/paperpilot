"""Web 进度回调测试：Orchestrator 结构化事件发射（无需 API key）。"""

from __future__ import annotations

import pytest

from src.evidence.store import EvidenceStore
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.schemas import AgentResult, AgentStatus


class RecordingCallback:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, ev: dict) -> None:
        self.events.append(ev)


class StubExtractor:
    max_papers_per_result = 3

    async def extract_from_paper(self, paper, query):
        return [
            {"claim": f"Claim from {paper['id']}", "evidence_text": "quote", "confidence": 0.9},
        ]

    async def fetch_papers(self, query, max_papers):
        return []


def _result(tid: str, papers: list[dict]) -> AgentResult:
    trajectory = [{"role": "tool", "result": {"papers": papers}}] if papers else []
    return AgentResult(
        task_id=tid, status=AgentStatus.SUCCESS, output="ok", trajectory=trajectory
    )


def _paper(pid: str) -> dict:
    return {"id": pid, "title": f"Paper {pid}", "summary": "abstract", "pdf_url": f"http://x/{pid}"}


@pytest.mark.asyncio
async def test_extract_evidence_emits_progress(tmp_path):
    """证据入库时发射 evidence 事件。"""
    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="cb-test")
    cb = RecordingCallback()
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=StubExtractor(),
        evidence_store=store,
        progress_callback=cb,
    )
    orch._query = "q"
    orch._results = [_result("t1", [_paper("p1")])]

    await orch._extract_evidence()

    ev_events = [e for e in cb.events if e["type"] == "evidence"]
    assert len(ev_events) == 1
    assert ev_events[0]["evidence_id"] == "E-1"
    assert ev_events[0]["paper_id"] == "p1"
    assert ev_events[0]["total"] == 1


@pytest.mark.asyncio
async def test_set_progress_callback_overrides(tmp_path):
    """set_progress_callback 动态挂载后能收到事件。"""
    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="cb-test2")
    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_extractor=StubExtractor(),
        evidence_store=store,
    )
    cb = RecordingCallback()
    orch.set_progress_callback(cb)
    orch._query = "q"
    orch._results = [_result("t1", [_paper("p1")])]

    await orch._extract_evidence()
    assert any(e["type"] == "evidence" for e in cb.events)


def test_emit_progress_swallows_callback_errors(tmp_path):
    """回调抛异常不影响主流程。"""
    from src.evidence.store import EvidenceStore

    store = EvidenceStore(db_path=str(tmp_path / "e.db"), session_id="cb-test3")

    def boom(ev):
        raise RuntimeError("cb down")

    orch = Orchestrator(
        planner=None,
        agent_pool=None,
        evidence_store=store,
        progress_callback=boom,
    )
    # 不应抛出
    orch._emit_progress({"type": "test"})
    orch._emit_progress({"type": "test"})
    # 无回调时也安全
    orch.set_progress_callback(None)
    orch._emit_progress({"type": "test"})


def test_emit_progress_calls_callback():
    cb = RecordingCallback()
    orch = Orchestrator(planner=None, agent_pool=None, progress_callback=cb)
    orch._emit_progress({"type": "state", "state": "PLANNING"})
    assert cb.events == [{"type": "state", "state": "PLANNING"}]

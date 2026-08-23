"""Summarizer 证据注入与报告证据索引渲染测试（无需 API key）。"""

from __future__ import annotations

import pytest

from src.agents.summarizer import SummarizerAgent
from src.core.runner import _format_report
from src.evidence.schemas import Evidence
from src.orchestrator.schemas import AgentResult, AgentStatus, ResearchReport, SubTask, TaskType


class ReportPolicy:
    """模拟 summarizer 的 policy：返回罐头报告内容。"""

    tools = None

    def __init__(self, content: str) -> None:
        self.content = content

    def __call__(self, messages):
        return {"content": self.content}


def _evidence(eid: str, claim: str, title: str = "Paper X", conf: float = 0.9) -> Evidence:
    return Evidence(
        evidence_id=eid,
        query="q",
        paper_id="p1",
        paper_title=title,
        source_url="https://arxiv.org/pdf/p1",
        claim=claim,
        evidence_text="excerpt",
        confidence=conf,
        topic="q",
    )


def _result(tid: str = "t1") -> AgentResult:
    return AgentResult(
        task_id=tid,
        status=AgentStatus.SUCCESS,
        output="Some sub-result content.",
        trajectory=[{"role": "tool", "result": {"papers": [{"id": "p1", "pdf_url": "https://arxiv.org/pdf/p1", "title": "Paper X"}]}}],
        confidence=0.8,
    )


def test_build_synthesis_prompt_injects_evidence():
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(""))
    evidence = [_evidence("E-1", "Claim one", title="Paper A"), _evidence("E-2", "Claim two", title="Paper B")]
    prompt = agent._build_synthesis_prompt("test query", [_result()], evidence)
    assert "# Evidence Items" in prompt
    assert "[E-1] Claim one — (Paper A)" in prompt
    assert "[E-2] Claim two — (Paper B)" in prompt
    assert "cite it inline as [E-<id>]" in prompt


def test_build_synthesis_prompt_no_evidence():
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(""))
    prompt = agent._build_synthesis_prompt("test query", [_result()], [])
    assert "# Evidence Items" not in prompt


@pytest.mark.asyncio
async def test_run_populates_report_evidence():
    content = (
        "# Report\n\n"
        "Some statement supported by evidence [E-1].\n\n"
        "Overall Confidence: 0.85"
    )
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(content))
    evidence = [_evidence("E-1", "Claim one"), _evidence("E-2", "Claim two")]
    task = SubTask(task_id="synthesize_final", task_type=TaskType.ANALYZE, description="synth")
    result = await agent.run(task, {"query": "q", "results": [_result()], "evidence": evidence})

    assert result.status == AgentStatus.SUCCESS
    report = result.output
    assert isinstance(report, ResearchReport)
    assert len(report.evidence) == 2
    assert report.evidence[0] == {
        "evidence_id": "E-1",
        "claim": "Claim one",
        "paper_title": "Paper X",
        "source_url": "https://arxiv.org/pdf/p1",
        "confidence": 0.9,
    }
    # 置信度 = LLM 自评 × 成功率开根
    assert report.confidence == 0.85


def test_format_report_renders_evidence_index():
    report = ResearchReport(
        query="q",
        content="Statement one [E-1] and statement two [E-2] and one more.",
        confidence=0.8,
        evidence=[
            {"evidence_id": "E-1", "claim": "Claim one", "paper_title": "Paper A",
             "source_url": "https://arxiv.org/pdf/a", "confidence": 0.9},
            {"evidence_id": "E-2", "claim": "Claim two", "paper_title": "Paper B",
             "source_url": "https://arxiv.org/pdf/b", "confidence": 0.7},
        ],
    )
    out = _format_report(report, elapsed=1.0)
    assert "## 证据索引" in out
    assert "- [E-1] Claim one — *Paper A* ([https://arxiv.org/pdf/a](https://arxiv.org/pdf/a)) 置信度: 0.90 (已引用)" in out
    assert "E-2" in out and "已引用" in out
    assert "未引用" not in out  # 两条都出现在正文中


def test_format_report_marks_uncited():
    report = ResearchReport(
        query="q",
        content="Only one marker [E-1].",
        confidence=0.8,
        evidence=[
            {"evidence_id": "E-1", "claim": "Cited", "paper_title": "A", "source_url": "", "confidence": 0.9},
            {"evidence_id": "E-2", "claim": "Not cited", "paper_title": "B", "source_url": "", "confidence": 0.5},
        ],
    )
    out = _format_report(report, elapsed=1.0)
    assert "(已引用)" in out
    assert "(未引用)" in out


def test_format_report_without_evidence_no_crash():
    report = ResearchReport(query="q", content="no evidence", confidence=0.5)
    out = _format_report(report, elapsed=1.0)
    assert "证据索引" not in out

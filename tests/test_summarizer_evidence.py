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
    # 无证据时不给出引用指令，避免模型编造 [E-x]
    assert "cite it inline as [E-<id>]" not in prompt
    assert "Known Contradictions" not in prompt


def _relation(eid1: str, eid2: str, relation: str = "CONTRADICTS") -> dict:
    return {
        "session_id": "s",
        "source_id": eid1,
        "target_id": eid2,
        "relation": relation,
        "weight": 0.87,
        "source_claim": f"Claim of {eid1}",
        "target_claim": f"Claim of {eid2}",
        "source_paper": "Paper A",
        "target_paper": "Paper B",
    }


def test_build_synthesis_prompt_injects_relations():
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(""))
    relations = [_relation("E-1", "E-2")]
    supports = [_relation("E-1", "E-3", relation="SUPPORTS")]
    extends = [_relation("E-1", "E-4", relation="EXTENDS")]
    prompt = agent._build_synthesis_prompt(
        "test query", [_result()], [], relations, supports, extends
    )
    assert "# Known Contradictions" in prompt
    assert "[E-1] vs [E-2] (weight 0.87)" in prompt
    assert "# Supporting Relations" in prompt
    assert "SUPPORTS" in prompt
    assert "# Related Extensions" in prompt
    assert "EXTENDS" in prompt
    assert "Explicitly address every item in Known Contradictions" in prompt


def test_build_synthesis_prompt_skips_empty_relations():
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(""))
    prompt = agent._build_synthesis_prompt("test query", [_result()], [], [], [])
    assert "# Known Contradictions" not in prompt
    assert "# Supporting Relations" not in prompt
    assert "# Related Extensions" not in prompt


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


@pytest.mark.asyncio
async def test_run_carries_evidence_relations():
    content = (
        "# Report\n\n"
        "We resolve the contradiction: the newer evidence is better supported. [E-1]\n\n"
        "Overall Confidence: 0.8"
    )
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(content))
    relations = [_relation("E-1", "E-2")]
    task = SubTask(task_id="synthesize_final", task_type=TaskType.ANALYZE, description="synth")
    result = await agent.run(
        task,
        {
            "query": "q",
            "results": [_result()],
            "evidence": [],
            "evidence_relations": relations,
            "evidence_relations_supports": [],
        },
    )
    assert result.status == AgentStatus.SUCCESS
    report = result.output
    assert len(report.evidence_relations) == 1
    assert report.evidence_relations[0]["source_id"] == "E-1"
    assert report.evidence_relations[0]["relation"] == "CONTRADICTS"


@pytest.mark.asyncio
async def test_run_carries_supports_into_report():
    """SUPPORTS 关系也要写入 report.evidence_relations（供 _format_report 渲染支持节）。"""

    content = (
        "# Report\n\n"
        "The supporting relation was verified. [E-1]\n\n"
        "Overall Confidence: 0.8"
    )
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(content))
    supports = [_relation("E-1", "E-3", relation="SUPPORTS")]
    task = SubTask(task_id="synthesize_final", task_type=TaskType.ANALYZE, description="synth")
    result = await agent.run(
        task,
        {
            "query": "q",
            "results": [_result()],
            "evidence": [],
            "evidence_relations": [],
            "evidence_relations_supports": supports,
        },
    )
    assert result.status == AgentStatus.SUCCESS
    report = result.output
    assert len(report.evidence_relations) == 1
    assert report.evidence_relations[0]["relation"] == "SUPPORTS"


@pytest.mark.asyncio
async def test_run_carries_extends_into_report():
    """EXTENDS 关系也要写入 report.evidence_relations。"""

    content = (
        "# Report\n\n"
        "The extension relation was noted. [E-1]\n\n"
        "Overall Confidence: 0.8"
    )
    agent = SummarizerAgent(name="summarizer", policy=ReportPolicy(content))
    extends = [_relation("E-1", "E-4", relation="EXTENDS")]
    task = SubTask(task_id="synthesize_final", task_type=TaskType.ANALYZE, description="synth")
    result = await agent.run(
        task,
        {
            "query": "q",
            "results": [_result()],
            "evidence": [],
            "evidence_relations": [],
            "evidence_relations_supports": [],
            "evidence_relations_extends": extends,
        },
    )
    assert result.status == AgentStatus.SUCCESS
    report = result.output
    assert len(report.evidence_relations) == 1
    assert report.evidence_relations[0]["relation"] == "EXTENDS"


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


def test_format_report_renders_relations():
    report = ResearchReport(
        query="q",
        content="Statement [E-1].",
        confidence=0.8,
        evidence_relations=[
            {
                "source_id": "E-1",
                "target_id": "E-2",
                "relation": "CONTRADICTS",
                "weight": 0.87,
                "source_claim": "Model A is faster",
                "target_claim": "Model B is slower",
                "source_paper": "Paper A",
                "target_paper": "Paper B",
            },
            {
                "source_id": "E-1",
                "target_id": "E-3",
                "relation": "SUPPORTS",
                "weight": 0.8,
                "source_claim": "Claim one",
                "target_claim": "Claim three",
                "source_paper": "Paper A",
                "target_paper": "Paper C",
            },
            {
                "source_id": "E-1",
                "target_id": "E-4",
                "relation": "EXTENDS",
                "weight": 0.7,
                "source_claim": "Claim one",
                "target_claim": "Claim four",
                "source_paper": "Paper A",
                "target_paper": "Paper D",
            },
        ],
    )
    out = _format_report(report, elapsed=1.0)
    assert "## 证据关系" in out
    assert "### 矛盾 (1)" in out
    assert "### 支持 (1)" in out
    assert "### 扩展 (1)" in out
    assert "[E-1] vs [E-2] (weight 0.87)" in out
    assert "*Paper A* vs *Paper B*" in out
    assert "[E-1] SUPPORTS [E-3]" in out
    assert "[E-1] EXTENDS [E-4]" in out

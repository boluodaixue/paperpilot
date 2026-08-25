"""EvidenceExtractor 与论文收割函数的单元测试（无需 API key）。"""

from __future__ import annotations

import pytest

from src.evidence.extractor import (
    EvidenceExtractor,
    _parse_claims_json,
    extract_papers_from_result,
)
from src.orchestrator.schemas import AgentResult, AgentStatus


class FakePolicy:
    """罐头 LLM policy：返回预设内容。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.tools = None
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        return {"content": self.content}


def _paper(pid: str, title: str = "Paper", summary: str = "Abstract text") -> dict:
    return {"id": pid, "title": title, "summary": summary, "pdf_url": f"https://arxiv.org/pdf/{pid}"}


# ---------------------------------------------------------------------------
# _parse_claims_json
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    text = '{"claims": [{"claim": "A", "evidence_text": "a", "confidence": 0.9}]}'
    claims = _parse_claims_json(text)
    assert len(claims) == 1
    assert claims[0]["claim"] == "A"
    assert claims[0]["confidence"] == 0.9


def test_parse_fenced_json():
    text = '```json\n{"claims": [{"claim": "B", "evidence_text": "b", "confidence": 0.8}]}\n```'
    claims = _parse_claims_json(text)
    assert len(claims) == 1
    assert claims[0]["claim"] == "B"


def test_parse_json_with_surrounding_text():
    text = 'Sure! Here is the result:\n{"claims": [{"claim": "C", "evidence_text": "c", "confidence": 0.7}]}\nHope this helps.'
    claims = _parse_claims_json(text)
    assert len(claims) == 1
    assert claims[0]["claim"] == "C"


def test_parse_regression_scan():
    # 完全畸形但包含 claim/evidence_text 字段的文本
    text = 'Here are claims: "claim": "D", "evidence_text": "d" and "claim": "E", "evidence_text": "e"'
    claims = _parse_claims_json(text)
    assert len(claims) == 2


def test_parse_empty_and_invalid():
    assert _parse_claims_json("") == []
    assert _parse_claims_json("no json here at all") == []
    assert _parse_claims_json("{}") == []


def test_parse_strips_empty_fields():
    text = '{"claims": [{"claim": "", "evidence_text": "x"}, {"claim": "OK", "evidence_text": ""}, {"claim": "Good", "evidence_text": "y", "confidence": "high"}]}'
    claims = _parse_claims_json(text)
    assert len(claims) == 3
    # 空字段被 normalize 为 ""
    assert claims[0]["claim"] == ""
    # confidence 非数值兜底为 0.5
    assert claims[2]["confidence"] == 0.5


# ---------------------------------------------------------------------------
# extract_papers_from_result
# ---------------------------------------------------------------------------

def _trajectory_result():
    trajectory = [
        # 普通 web_search 结果，无 papers，应忽略
        {"role": "tool", "result": {"results": [{"url": "https://example.com", "title": "x"}]}},
        # arxiv 结果，两篇论文
        {"role": "tool", "result": {"papers": [_paper("2101.00001", "One"), _paper("2101.00002", "Two")]}},
        # 报错的工具调用，应跳过
        {"role": "tool", "result": {"error": "rate limited"}},
        # 非 dict 的 step，应跳过
        ["not", "a", "dict"],
    ]
    return AgentResult(task_id="t1", status=AgentStatus.SUCCESS, output="summary", trajectory=trajectory)


def test_harvest_papers_from_trajectory():
    papers = extract_papers_from_result(_trajectory_result(), max_papers=10)
    assert [p["id"] for p in papers] == ["2101.00001", "2101.00002"]


def test_harvest_respects_max_papers():
    papers = extract_papers_from_result(_trajectory_result(), max_papers=1)
    assert len(papers) == 1
    assert papers[0]["id"] == "2101.00001"


def test_harvest_dedups_repeated_papers():
    trajectory = [
        {"role": "tool", "result": {"papers": [_paper("2101.00001"), _paper("2101.00001")]}},
    ]
    result = AgentResult(task_id="t", status=AgentStatus.SUCCESS, output="", trajectory=trajectory)
    papers = extract_papers_from_result(result, max_papers=10)
    assert len(papers) == 1


def test_harvest_no_papers():
    result = AgentResult(task_id="t", status=AgentStatus.SUCCESS, output="", trajectory=[])
    assert extract_papers_from_result(result) == []


# ---------------------------------------------------------------------------
# EvidenceExtractor.extract_from_paper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_from_paper_success():
    policy = FakePolicy('{"claims": [{"claim": "X works", "evidence_text": "X improves accuracy", "confidence": 0.9}]}')
    extractor = EvidenceExtractor(policy=policy, max_claims_per_paper=5)
    claims = await extractor.extract_from_paper(_paper("2101.00001", "X Paper", "X improves accuracy."), "test query")
    assert len(claims) == 1
    assert claims[0]["claim"] == "X works"
    assert claims[0]["evidence_text"] == "X improves accuracy"


@pytest.mark.asyncio
async def test_extract_from_paper_empty_abstract():
    policy = FakePolicy("whatever")
    extractor = EvidenceExtractor(policy=policy)
    claims = await extractor.extract_from_paper({"id": "p1", "title": "No abstract"}, "q")
    assert claims == []
    assert policy.calls == 0  # 空摘要不调用 LLM


@pytest.mark.asyncio
async def test_extract_from_paper_policy_error_is_swallowed():
    class BoomPolicy:
        def __call__(self, messages):
            raise RuntimeError("api down")

    extractor = EvidenceExtractor(policy=BoomPolicy())
    claims = await extractor.extract_from_paper(_paper("p1", "X", "abstract text"), "q")
    assert claims == []


@pytest.mark.asyncio
async def test_extract_respects_max_claims():
    policy = FakePolicy(
        '{"claims": [{"claim": "c1", "evidence_text": "e1"}, {"claim": "c2", "evidence_text": "e2"}, {"claim": "c3", "evidence_text": "e3"}]}'
    )
    extractor = EvidenceExtractor(policy=policy, max_claims_per_paper=2)
    claims = await extractor.extract_from_paper(_paper("p1", "X", "abstract"), "q")
    assert len(claims) == 2


# ---------------------------------------------------------------------------
# EvidenceExtractor.fetch_papers（论文补取）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_papers_with_fetcher():
    async def fetcher(query, max_results):
        return [_paper("p1", "One"), _paper("p2", "Two"), {"id": "", "title": "no id"}]

    extractor = EvidenceExtractor(policy=FakePolicy("{}"), paper_fetcher=fetcher)
    papers = await extractor.fetch_papers("some query", 3)
    assert [p["id"] for p in papers] == ["p1", "p2"]  # 无 id 的被过滤


@pytest.mark.asyncio
async def test_fetch_papers_without_fetcher():
    extractor = EvidenceExtractor(policy=FakePolicy("{}"))
    assert await extractor.fetch_papers("q") == []


@pytest.mark.asyncio
async def test_fetch_papers_fetcher_raises():
    async def boom(query, max_results):
        raise RuntimeError("network down")

    extractor = EvidenceExtractor(policy=FakePolicy("{}"), paper_fetcher=boom)
    assert await extractor.fetch_papers("q") == []

from __future__ import annotations

import ssl

import aiohttp
import pytest

from src.research.agent_graph import _extract_evidence
from src.research.models import NextResearchAction
from src.tools.arxiv_reader import ArxivReaderTool
from src.tools.browser import BrowserTool
from src.tools.http_client import trusted_ssl_context
from src.tools.web_search import WebSearchTool


def test_trusted_ssl_context_keeps_certificate_verification_enabled() -> None:
    context = trusted_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_web_search_skips_unconfigured_optional_fallbacks() -> None:
    tool = WebSearchTool(backend="tavily", api_key="tvly-real-key")
    tool.fallback_backends = ("metaso", "exa", "bocha")
    tool.metaso_key = "metaso-real-key"
    tool.exa_key = None
    tool.bocha_key = "your_bocha_key_here"

    assert tool._backend_order() == ("tavily", "metaso")


def test_retired_bing_backend_is_not_supported() -> None:
    tool = WebSearchTool(backend="bing")

    assert "bing" not in tool._supported_backends
    assert tool._backend_order()[0] == "tavily"


@pytest.mark.asyncio
async def test_web_search_falls_back_from_tavily_to_metaso(monkeypatch) -> None:
    tool = WebSearchTool(backend="tavily", api_key="tvly-real-key")
    tool.fallback_backends = ("metaso", "exa", "bocha")
    tool.metaso_key = "metaso-real-key"
    tool.exa_key = None
    tool.bocha_key = None
    calls: list[str] = []

    async def failed_tavily(*args, **kwargs):
        calls.append("tavily")
        return {"results": [], "error": "Tavily network error: timeout"}

    async def working_metaso(*args, **kwargs):
        calls.append("metaso")
        return {
            "source": "metaso",
            "results": [
                {
                    "title": "中文资料",
                    "url": "https://example.cn/source",
                    "snippet": "可引用内容",
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr(tool, "_tavily_execute", failed_tavily)
    monkeypatch.setattr(tool, "_metaso_execute", working_metaso)

    result = await tool.execute("测试问题")

    assert calls == ["tavily", "metaso"]
    assert result["source"] == "metaso"
    assert result["fallback_used"] is True
    assert result["backends_tried"] == ["tavily", "metaso"]
    assert result["backend_errors"] == [{"backend": "tavily", "error": "Tavily network error: timeout"}]


class _FakeSearchResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, **kwargs):
        return self.payload


class _FakeSearchSession:
    def __init__(self, response: _FakeSearchResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, endpoint: str, **kwargs):
        self.calls.append({"endpoint": endpoint, **kwargs})
        return self.response


@pytest.mark.asyncio
async def test_tavily_adapter_parses_and_deduplicates_results(monkeypatch) -> None:
    tool = WebSearchTool(backend="tavily", api_key="tvly-real-key")
    session = _FakeSearchSession(
        _FakeSearchResponse(
            200,
            {
                "results": [
                    {
                        "title": "Source",
                        "url": "https://example.com/article",
                        "content": "Evidence",
                    },
                    {
                        "title": "Duplicate",
                        "url": "https://www.example.com/article/",
                        "content": "Duplicate evidence",
                    },
                ]
            },
        )
    )
    monkeypatch.setattr(tool, "_get_session", lambda: session)

    result = await tool._tavily_execute("question", 5)

    assert result["source"] == "tavily"
    assert result["total"] == 1
    assert result["results"][0]["snippet"] == "Evidence"
    assert session.calls[0]["json"]["search_depth"] == "basic"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer tvly-real-key"


@pytest.mark.asyncio
async def test_exa_adapter_uses_highlights_as_snippet(monkeypatch) -> None:
    tool = WebSearchTool(backend="exa", api_key="exa-real-key")
    session = _FakeSearchSession(
        _FakeSearchResponse(
            200,
            {
                "results": [
                    {
                        "title": "Semantic result",
                        "url": "https://example.org/paper",
                        "highlights": ["first", "second"],
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(tool, "_get_session", lambda: session)

    result = await tool._exa_execute("question", 3)

    assert result["source"] == "exa"
    assert result["results"][0]["snippet"] == "first second"
    assert session.calls[0]["headers"]["x-api-key"] == "exa-real-key"


@pytest.mark.asyncio
async def test_metaso_adapter_uses_current_search_api(monkeypatch) -> None:
    tool = WebSearchTool(backend="metaso", api_key="mk-real-key")
    session = _FakeSearchSession(
        _FakeSearchResponse(
            200,
            {
                "webpages": [
                    {
                        "title": "中文结果",
                        "link": "https://example.cn/current-api",
                        "snippet": "当前秘塔搜索接口返回的摘要",
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(tool, "_get_session", lambda: session)

    result = await tool._metaso_execute("测试问题", 3)

    assert tool.metaso_endpoint == "https://metaso.cn/api/v1/search"
    assert result["source"] == "metaso"
    assert result["results"][0]["url"] == "https://example.cn/current-api"
    payload = session.calls[0]["json"]
    assert payload["q"] == "测试问题"
    assert payload["scope"] == "webpage"
    assert payload["size"] == "3"
    assert "page" not in payload


def test_arxiv_identifiers_are_normalized_and_routed_to_arxiv_first() -> None:
    tool = ArxivReaderTool(backend="openalex")

    assert tool._normalize_paper_id("https://arxiv.org/pdf/2412.15115.pdf") == "2412.15115"
    assert tool._backend_order("2412.15115") == (
        "arxiv",
        "semantic_scholar",
        "openalex",
    )


@pytest.mark.asyncio
async def test_academic_reader_falls_back_after_backend_failure(monkeypatch) -> None:
    tool = ArxivReaderTool(backend="arxiv")
    calls: list[str] = []

    async def failed_arxiv(*args, **kwargs):
        calls.append("arxiv")
        return {"source": "arxiv_api", "papers": [], "error": "TLS failure"}

    async def working_semantic_scholar(*args, **kwargs):
        calls.append("semantic_scholar")
        return {
            "source": "semantic_scholar",
            "papers": [{"id": "paper-1", "title": "Recovered paper"}],
        }

    async def unexpected_openalex(*args, **kwargs):
        calls.append("openalex")
        raise AssertionError("OpenAlex should not run after a successful fallback")

    monkeypatch.setattr(tool, "_arxiv_execute", failed_arxiv)
    monkeypatch.setattr(
        tool,
        "_semantic_scholar_execute",
        working_semantic_scholar,
    )
    monkeypatch.setattr(tool, "_openalex_execute", unexpected_openalex)

    result = await tool.execute(paper_id="2412.15115")

    assert calls == ["arxiv", "semantic_scholar"]
    assert result["source"] == "semantic_scholar"
    assert result["fallback_used"] is True
    assert result["backends_tried"] == ["arxiv", "semantic_scholar"]


@pytest.mark.asyncio
async def test_academic_reader_preserves_all_backend_errors(monkeypatch) -> None:
    tool = ArxivReaderTool(backend="openalex")

    async def failed_backend(*args, **kwargs):
        return {"papers": [], "error": "service offline"}

    monkeypatch.setattr(tool, "_arxiv_execute", failed_backend)
    monkeypatch.setattr(tool, "_semantic_scholar_execute", failed_backend)
    monkeypatch.setattr(tool, "_openalex_execute", failed_backend)

    result = await tool.execute(query="missing paper")

    assert result["source"] == "academic_fallback"
    assert result["error"].startswith("All academic metadata backends unavailable:")
    assert len(result["backend_errors"]) == 3


@pytest.mark.parametrize(
    ("paper_id", "expected"),
    [
        ("W4405655184", "W4405655184"),
        ("https://openalex.org/W4405655184", "W4405655184"),
        ("https://doi.org/10.48550/arXiv.2412.15115", "doi:10.48550/arXiv.2412.15115"),
        ("2412.15115", None),
    ],
)
def test_openalex_only_uses_supported_singleton_ids(paper_id, expected) -> None:
    assert ArxivReaderTool._openalex_lookup_id(paper_id) == expected


def test_browser_only_generates_same_publisher_safe_alternatives() -> None:
    assert BrowserTool._alternative_urls("https://openai.com/index/gpt-4o-system-card/") == (
        "https://cdn.openai.com/gpt-4o-system-card.pdf",
    )
    assert BrowserTool._alternative_urls("https://openai.com/index/hello-gpt-4o/") == (
        "https://cdn.openai.com/gpt-4o-system-card.pdf",
    )
    assert BrowserTool._alternative_urls("https://example.com/private") == ()


@pytest.mark.asyncio
async def test_browser_uses_official_pdf_after_403(monkeypatch) -> None:
    tool = BrowserTool()
    original = "https://openai.com/index/gpt-4o-system-card/"
    alternative = "https://cdn.openai.com/gpt-4o-system-card.pdf"

    async def fake_fetch(url: str) -> str:
        if url == original:
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            )
        assert url == alternative
        return "<main>Official system card evidence with enough content.</main>"

    monkeypatch.setattr(tool, "_fetch", fake_fetch)

    result = await tool.execute(original)

    assert result.startswith(f"[ALTERNATIVE_SOURCE: {alternative}]")
    assert "Official system card evidence" in result


def test_alternative_browser_evidence_uses_actual_source_url() -> None:
    alternative = "https://cdn.openai.com/gpt-4o-system-card.pdf"
    result = (
        f"[ALTERNATIVE_SOURCE: {alternative}]\n"
        "[ORIGINAL_SOURCE_BLOCKED: https://openai.com/index/gpt-4o-system-card/]\n\n"
        "Official source content."
    )
    evidence = _extract_evidence(
        "browser",
        {"url": "https://openai.com/index/gpt-4o-system-card/"},
        result,
        action=NextResearchAction(
            requirement_id="req-1",
            strategy="read official source",
            query="GPT-4o System Card",
            expected_value="official evidence",
            expected_improvement="support requirement",
            action_id="action-1",
        ),
        artifact_id="artifact-1",
    )

    assert len(evidence) == 1
    assert evidence[0].source_ref == alternative
    assert "alternative source" in evidence[0].limitations.lower()

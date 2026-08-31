"""Full-content Browser extraction without search-snippet evidence."""

from __future__ import annotations

import aiohttp
import pytest

from src.research.agent_graph import _extract_evidence
from src.research.models import NextResearchAction
from src.tools.browser import BrowserTool, FetchedContent
from src.tools.content_extraction import ContentExtractionConfig
from src.tools.content_extraction import (
    UnsupportedScannedPdfError,
    _docling_pipeline_options,
    _extract_docling_pages,
    _needs_docling,
    _pypdf_is_sufficient,
    _pdf_document_from_pages,
)


@pytest.mark.asyncio
async def test_structured_html_keeps_markdown_and_ranks_late_relevant_section(
    monkeypatch,
) -> None:
    filler = "General background sentence. " * 500
    html = f"""
    <html><head><title>Bond Principles</title></head><body>
      <nav>Home Products Pricing Contact</nav>
      <main>
        <h1>Bond Principles</h1>
        <h2>Introduction</h2><p>{filler}</p>
        <h2>KPI and SPT selection</h2>
        <p>Issuers <strong>must</strong> disclose material KPIs and calibrated SPTs.</p>
        <p>See the <a href="https://example.com/rules">official rules</a>.</p>
      </main>
    </body></html>
    """
    tool = BrowserTool(
        extraction_config=ContentExtractionConfig(
            mode="structured",
            max_blocks=3,
            max_output_chars=5_000,
        )
    )

    async def fake_fetch(url: str) -> FetchedContent:
        return FetchedContent(
            final_url=url,
            content_type="text/html",
            payload=html.encode(),
            charset="utf-8",
        )

    monkeypatch.setattr(tool, "_fetch_payload", fake_fetch)

    result = await tool.execute(
        "https://example.com/principles",
        query="KPI SPT disclosure",
    )

    assert isinstance(result, dict)
    assert result["format"] == "html"
    assert result["extractor"] == "beautifulsoup+markdownify"
    assert result["blocks"][0]["heading"] == "KPI and SPT selection"
    assert result["blocks"][0]["locator"].startswith("section:kpi-and-spt-selection")
    assert "**must**" in result["blocks"][0]["text"]
    assert "[official rules](https://example.com/rules)" in result["blocks"][0]["text"]
    assert "Home Products Pricing" not in str(result)


@pytest.mark.asyncio
async def test_structured_html_selects_relevant_content_container_over_site_shell(
    monkeypatch,
) -> None:
    shell = " ".join(f'<a href="/site-{index}">Market directory item {index}</a>' for index in range(120))
    html = f"""
    <html><head><title>SLB Principles</title></head><body>
      <div class="site-shell">{shell}</div>
      <div class="pge">
        <div class="item show">
          <p>SLB issuers select material KPIs and calibrate ambitious SPTs.</p>
          <p>Reporting and independent verification protect investors.</p>
        </div>
      </div>
    </body></html>
    """
    tool = BrowserTool(extraction_config=ContentExtractionConfig(mode="structured"))

    async def fake_fetch(url: str) -> FetchedContent:
        return FetchedContent(url, "text/html", html.encode())

    monkeypatch.setattr(tool, "_fetch_payload", fake_fetch)

    result = await tool.execute(
        "https://example.com/slbp",
        query="KPI SPT reporting verification investor protection",
    )

    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["locator"] == "section:slb-principles"
    assert "material KPIs" in result["blocks"][0]["text"]
    assert "Market directory item" not in str(result)


@pytest.mark.asyncio
async def test_legacy_browser_result_remains_a_string(monkeypatch) -> None:
    tool = BrowserTool(extraction_config=ContentExtractionConfig(mode="legacy"))

    async def fake_fetch(url: str) -> str:
        return "<main><h1>Legacy page</h1><p>Legacy extraction content.</p></main>"

    monkeypatch.setattr(tool, "_fetch", fake_fetch)

    result = await tool.execute("https://example.com/legacy")

    assert isinstance(result, str)
    assert "Legacy extraction content" in result


def test_browser_schema_accepts_question_for_relevance_ranking() -> None:
    schema = BrowserTool().get_openai_tool_schema()

    assert "query" in schema["function"]["parameters"]["properties"]


def test_pdf_pages_keep_page_locator_and_rank_relevant_page() -> None:
    document = _pdf_document_from_pages(
        [
            "Introduction\n" + "General bond background. " * 80,
            "KPI Selection\nMaterial KPIs and calibrated SPTs must be disclosed.",
            "Verification\nAn external reviewer verifies performance.",
        ],
        url="https://example.com/principles.pdf",
        query="KPI SPT disclosure",
        max_blocks=2,
        max_output_chars=2_000,
        extractor="pypdf",
    )

    payload = document.to_dict()

    assert payload["blocks"][0]["locator"] == "page:2"
    assert payload["blocks"][0]["heading"] == "KPI Selection"
    assert payload["extractor"] == "pypdf"


def test_textless_pdf_pages_are_explicitly_unsupported() -> None:
    with pytest.raises(UnsupportedScannedPdfError, match="OCR"):
        _pdf_document_from_pages(
            ["", "  "],
            url="https://example.com/scan.pdf",
            query="target",
            max_blocks=2,
            max_output_chars=2_000,
            extractor="pypdf",
        )


def test_docling_options_disable_ocr_remote_services_and_image_enrichment() -> None:
    pytest.importorskip("docling")
    options = _docling_pipeline_options()

    assert options.do_ocr is False
    assert options.do_table_structure is False
    assert options.enable_remote_services is False
    assert options.allow_external_plugins is False
    assert options.generate_page_images is False
    assert options.generate_picture_images is False

    table_options = _docling_pipeline_options(table_structure=True)
    assert table_options.do_table_structure is True


def test_docling_requires_explicit_table_intent_and_insufficient_pypdf_text() -> None:
    complex_lines = [f"Column A   Column B   {index}" for index in range(30)]
    complex_pages = ["\n".join(complex_lines)]

    assert _needs_docling(complex_pages, "compare bond disclosure rules") is False
    assert _pypdf_is_sufficient(complex_pages, "table of unrelated missing terms") is False
    assert _needs_docling(complex_pages, "table of unrelated missing terms") is True

    sufficient_pages = [
        "KPI SPT reporting table\n" + "KPI SPT reporting requirements. " * 160,
        "Verification table\n" + "External verification and reporting. " * 100,
    ]
    assert _pypdf_is_sufficient(sufficient_pages, "KPI SPT reporting table") is True
    assert _needs_docling(sufficient_pages, "KPI SPT reporting table") is False


def test_docling_timeout_opens_process_wide_circuit_breaker(monkeypatch) -> None:
    import src.tools.content_extraction as extraction

    calls = 0

    def timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(extraction._DOCLING_RUNTIME, "extract", timeout)
    monkeypatch.setattr(extraction, "_DOCLING_CIRCUIT_OPEN", False)

    with pytest.raises(TimeoutError):
        _extract_docling_pages(b"pdf", table_structure=True, timeout_seconds=0.01)
    with pytest.raises(RuntimeError, match="circuit breaker"):
        _extract_docling_pages(b"pdf", table_structure=True, timeout_seconds=0.01)
    assert calls == 1


@pytest.mark.asyncio
async def test_tavily_extract_is_only_a_low_quality_html_fallback(monkeypatch) -> None:
    tool = BrowserTool(
        extraction_config=ContentExtractionConfig(
            mode="structured",
            tavily_extract_fallback=True,
        )
    )
    calls: list[str] = []

    async def fake_fetch(url: str) -> FetchedContent:
        return FetchedContent(
            final_url=url,
            content_type="text/html",
            payload=b"<html><main><h1>Thin page</h1><p>Only a tiny introduction is available.</p></main></html>",
        )

    async def fake_tavily(url: str):
        calls.append(url)
        return (
            "Full source",
            "# Full source\n\n## KPI and SPT\n\n" + "KPI SPT disclosure requirements. " * 150,
        )

    monkeypatch.setattr(tool, "_fetch_payload", fake_fetch)
    monkeypatch.setattr(tool, "_tavily_extract", fake_tavily)

    result = await tool.execute("https://example.com/thin", query="KPI SPT disclosure")

    assert calls == ["https://example.com/thin"]
    assert result["extractor"] == "tavily-extract"
    assert "fallback used" in result["warnings"][0]


@pytest.mark.asyncio
async def test_tavily_extract_recovers_when_local_html_has_no_content(monkeypatch) -> None:
    tool = BrowserTool(
        extraction_config=ContentExtractionConfig(
            mode="structured",
            tavily_extract_fallback=True,
        )
    )

    async def fake_fetch(url: str) -> FetchedContent:
        return FetchedContent(
            final_url=url,
            content_type="text/html",
            payload=b"<html><body><div>Sign in</div></body></html>",
        )

    async def fake_tavily(url: str):
        return "Recovered", "# Recovered\n\nFull source evidence from fallback."

    monkeypatch.setattr(tool, "_fetch_payload", fake_fetch)
    monkeypatch.setattr(tool, "_tavily_extract", fake_tavily)

    result = await tool.execute("https://example.com/blocked", query="source evidence")

    assert result["status"] == "ok"
    assert result["extractor"] == "tavily-extract"
    assert result["blocks"][0]["locator"] == "section:recovered"


def test_structured_browser_blocks_become_separate_locatable_evidence() -> None:
    result = {
        "status": "ok",
        "url": "https://example.com/principles.pdf",
        "title": "Bond Principles",
        "format": "pdf",
        "extractor": "pypdf",
        "quality_score": 0.9,
        "warnings": [],
        "blocks": [
            {
                "locator": "page:4",
                "heading": "KPI Selection",
                "text": "KPIs must be material to the issuer's strategy.",
                "relevance_score": 5.0,
            },
            {
                "locator": "page:7",
                "heading": "Verification",
                "text": "Independent verification is recommended.",
                "relevance_score": 2.0,
            },
        ],
    }
    action = NextResearchAction(
        requirement_id="question-1",
        strategy="read primary source",
        query="KPI SPT disclosure verification",
        expected_value="primary evidence",
        expected_improvement="answer the core question",
        action_id="action-1",
    )

    evidence = _extract_evidence(
        "browser",
        {"url": "https://example.com/principles.pdf", "query": action.query},
        result,
        action=action,
        artifact_id="artifact-1",
    )

    assert [item.locator for item in evidence] == ["page:4", "page:7"]
    assert all(item.source_ref == result["url"] for item in evidence)
    assert all(item.source_type == "paper" for item in evidence)
    assert all(item.requirement_id == "question-1" for item in evidence)
    assert all(item.action_id == "action-1" for item in evidence)
    assert all(item.artifact_id == "artifact-1" for item in evidence)


@pytest.mark.asyncio
async def test_structured_browser_keeps_actual_official_alternative_url(monkeypatch) -> None:
    tool = BrowserTool(extraction_config=ContentExtractionConfig(mode="structured"))
    original = "https://openai.com/index/gpt-4o-system-card/"
    alternative = "https://cdn.openai.com/gpt-4o-system-card.pdf"

    async def fake_fetch(url: str) -> FetchedContent:
        if url == original:
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
            )
        assert url == alternative
        return FetchedContent(
            final_url=alternative,
            content_type="application/pdf",
            payload=b"test-pdf-payload",
        )

    async def fake_extract(fetched, *, original_url: str, query: str):
        assert fetched.final_url == alternative
        assert original_url == original
        assert query == "official benchmark"
        return {
            "status": "ok",
            "url": alternative,
            "original_url": original,
            "title": "Official alternative",
            "format": "pdf",
            "extractor": "pypdf",
            "blocks": [{"locator": "page:1", "heading": "Official", "text": "Evidence"}],
            "warnings": [],
        }

    monkeypatch.setattr(tool, "_fetch_payload", fake_fetch)
    monkeypatch.setattr(tool, "_extract_structured_payload", fake_extract)

    result = await tool.execute(original, query="official benchmark")

    assert result["url"] == alternative
    assert result["original_url"] == original
    assert "alternative source" in result["warnings"][-1].lower()

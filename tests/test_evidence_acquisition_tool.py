"""Deterministic discovery-to-document acquisition contracts."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from src.research.agent_graph import _extract_evidence
from src.research.models import NextResearchAction
from src.research.runtime import build_research_tools
from src.tools import EvidenceAcquisitionTool, canonicalize_source_url
from src.tools.evidence_acquisition import inferred_primary_domains


class FixedSearch:
    name = "web_search"

    async def execute(self, query: str, top_n: int = 3) -> dict[str, Any]:
        del query, top_n
        return {
            "source": "any-configured-backend",
            "backends_tried": ["any-configured-backend"],
            "results": [
                {
                    "title": "Blog summary",
                    "url": "https://example.com/summary?utm_source=test",
                    "snippet": "A secondary summary.",
                },
                {
                    "title": "Official Green Bond Principles PDF",
                    "url": "https://authority.gov/green-bond-principles.pdf",
                    "snippet": "Official principles and reporting requirements.",
                },
                {
                    "title": "Duplicate official result",
                    "url": "https://authority.gov/green-bond-principles.pdf#page=3",
                    "snippet": "The same official document.",
                },
                {
                    "title": "Independent standards report",
                    "url": "https://standards.org/report",
                    "snippet": "Independent standards comparison.",
                },
            ],
        }

    def __deepcopy__(self, memo):
        del memo
        return FixedSearch()


class StructuredBrowser:
    calls: list[str] = []

    async def execute(self, *, url: str, max_chars: int, query: str) -> dict[str, Any]:
        del max_chars, query
        type(self).calls.append(url)
        is_pdf = url.endswith(".pdf")
        return {
            "url": url,
            "title": "Opened source",
            "format": "pdf" if is_pdf else "html",
            "extractor": "pypdf" if is_pdf else "beautifulsoup+markdownify",
            "warnings": [],
            "blocks": [
                {
                    "heading": "Core requirements",
                    "locator": "page:3" if is_pdf else "section:core-requirements",
                    "text": "Full source text supporting the assigned requirement.",
                    "relevance_score": 10.0,
                }
            ],
        }

    def __deepcopy__(self, memo):
        del memo
        return StructuredBrowser()


def test_source_url_canonicalization_removes_tracking_and_fragments() -> None:
    assert canonicalize_source_url(
        "HTTPS://Example.COM/report/?utm_source=x&b=2&a=1#section"
    ) == "https://example.com/report?a=1&b=2"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("PIPL 数据出境 标准合同", ("cac.gov.cn", "gov.cn")),
        ("GDPR Article 46 SCC EDPB", (
            "eur-lex.europa.eu", "edpb.europa.eu", "ec.europa.eu"
        )),
        ("FDA approval and EMA EPAR", ("fda.gov", "ema.europa.eu")),
    ],
)
def test_requirement_query_infers_primary_source_registries(query, expected) -> None:
    assert inferred_primary_domains(query) == expected


@pytest.mark.asyncio
async def test_acquisition_ranks_deduplicates_and_opens_full_sources() -> None:
    StructuredBrowser.calls.clear()
    tool = EvidenceAcquisitionTool(
        FixedSearch(),
        StructuredBrowser(),
        default_sources=2,
    )

    result = await tool.execute("green bond principles reporting")

    assert result["metrics"]["candidate_count"] == 3
    assert result["metrics"]["duplicate_candidate_count"] == 1
    assert result["metrics"]["opened_count"] == 2
    assert result["selected_urls"][0].endswith(".pdf")
    assert len({item["url"] for item in result["documents"]}) == 2
    assert len(StructuredBrowser.calls) == 2


@pytest.mark.asyncio
async def test_isolated_worker_clones_share_opened_document_cache() -> None:
    StructuredBrowser.calls.clear()
    root = EvidenceAcquisitionTool(
        FixedSearch(),
        StructuredBrowser(),
        default_sources=1,
    )
    first = copy.deepcopy(root)
    second = copy.deepcopy(root)

    results = await asyncio.gather(
        first.execute("green bond principles reporting"),
        second.execute("green bond principles reporting"),
    )

    assert len(StructuredBrowser.calls) == 1
    assert sum(item["metrics"]["cache_hit_count"] for item in results) == 1
    assert all(item["documents"] for item in results)


@pytest.mark.asyncio
async def test_acquisition_preserves_browser_status_error_and_warnings() -> None:
    class BrokenBrowser:
        async def execute(self, *, url: str, max_chars: int, query: str):
            del max_chars, query
            return {
                "status": "error",
                "url": url,
                "error": "Extraction error: ModuleNotFoundError: markdownify",
                "warnings": ["structured extractor unavailable"],
            }

    tool = EvidenceAcquisitionTool(
        FixedSearch(),
        BrokenBrowser(),
        default_sources=1,
    )

    result = await tool.execute("green bond principles reporting")

    assert result["status"] == "error"
    assert result["fetch_errors"] == [{
        "url": "https://authority.gov/green-bond-principles.pdf",
        "status": "error",
        "error": "Extraction error: ModuleNotFoundError: markdownify",
        "warnings": ["structured extractor unavailable"],
    }]


def test_acquired_documents_become_source_locatable_evidence() -> None:
    result = {
        "search_backend": "tavily",
        "documents": [
            {
                "url": "https://authority.gov/principles.pdf",
                "title": "Official principles",
                "format": "pdf",
                "extractor": "pypdf",
                "warnings": [],
                "blocks": [
                    {
                        "heading": "Reporting",
                        "locator": "page:5",
                        "text": "Issuers should publish an annual reporting update.",
                    }
                ],
            }
        ],
    }
    action = NextResearchAction(
        "question-1",
        "primary-source",
        "official reporting requirements",
        "high",
        "Obtain exact terms",
        "action-1",
    )

    evidence = _extract_evidence(
        "acquire_evidence",
        {"query": action.query},
        result,
        action=action,
        artifact_id="artifact-1",
    )

    assert len(evidence) == 1
    assert evidence[0].source_ref == "https://authority.gov/principles.pdf"
    assert evidence[0].locator == "page:5"
    assert "tavily" in evidence[0].limitations


@pytest.mark.asyncio
async def test_acquisition_prefers_two_primary_documents_from_same_official_host() -> None:
    class OfficialSearch:
        async def execute(self, query: str, top_n: int = 3):
            del query, top_n
            return {"source": "test", "results": [
                {
                    "title": "Official Green Bond Principles",
                    "url": "https://standards.org/green-bond-principles.pdf",
                    "snippet": "Official principles.",
                },
                {
                    "title": "Official Sustainability-Linked Bond Principles",
                    "url": "https://standards.org/slb-principles.pdf",
                    "snippet": "Official principles.",
                },
                {
                    "title": "Interview questions blog summary",
                    "url": "https://questions.com/blog/summary",
                    "snippet": "Secondary summary.",
                },
            ]}

    StructuredBrowser.calls.clear()
    tool = EvidenceAcquisitionTool(OfficialSearch(), StructuredBrowser(), default_sources=2)

    result = await tool.execute("official green bond and SLB principles")

    assert result["selected_urls"] == [
        "https://standards.org/green-bond-principles.pdf",
        "https://standards.org/slb-principles.pdf",
    ]


@pytest.mark.asyncio
async def test_acquisition_requeries_preferred_domain_when_initial_recall_misses_it() -> None:
    class TargetedSearch:
        def __init__(self):
            self.queries = []

        async def execute(self, query: str, top_n: int = 3):
            self.queries.append((query, top_n))
            if "site:eur-lex.europa.eu" in query:
                return {"source": "test", "results": [{
                    "title": "Official GDPR Chapter V",
                    "url": "https://eur-lex.europa.eu/eli/reg/2016/679/oj",
                    "snippet": "Official consolidated regulation.",
                }]}
            return {"source": "test", "results": [{
                "title": "Unofficial GDPR summary",
                "url": "https://example.com/gdpr-summary",
                "snippet": "Secondary summary.",
            }]}

    search = TargetedSearch()
    StructuredBrowser.calls.clear()
    tool = EvidenceAcquisitionTool(search, StructuredBrowser(), default_sources=1)

    result = await tool.execute("GDPR Article 46 SCC transfer safeguards")

    assert any("site:eur-lex.europa.eu" in query for query, _ in search.queries)
    assert result["selected_urls"] == [
        "https://eur-lex.europa.eu/eli/reg/2016/679/oj"
    ]
    assert result["search_queries"][0] == "GDPR Article 46 SCC transfer safeguards"


def test_v2_runtime_exposes_composite_acquisition_but_legacy_keeps_low_level_tools() -> None:
    common = {
        "tools": {"enabled": ["web_search", "browser", "calculator"], "web_search": {"mock_mode": True}},
    }
    legacy = build_research_tools(common)
    v2 = build_research_tools(
        {
            **common,
            "research": {
                "architecture": "supervisor_v2",
                "supervisor_v2": {"enabled": True},
            },
        }
    )

    assert [item.name for item in legacy] == ["web_search", "browser", "calculator"]
    assert [item.name for item in v2] == ["acquire_evidence", "calculator"]

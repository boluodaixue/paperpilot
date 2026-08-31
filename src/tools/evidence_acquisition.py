"""Deterministic discovery-to-document acquisition for Research Agent V2.

The Agent chooses a scoped research query once.  This adapter then searches,
ranks and deduplicates candidates, opens the strongest sources, and returns
the structured Browser documents needed for source-locatable Evidence.
"""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
_PRIMARY_HINTS = (
    "principles",
    "standard",
    "regulation",
    "guideline",
    "official",
    "report",
    "paper",
    "公告",
    "办法",
    "规则",
    "原则",
    "指引",
    "报告",
)
_QUERY_STOPWORDS = {
    "and",
    "for",
    "from",
    "official",
    "of",
    "pdf",
    "the",
    "with",
}
_SECONDARY_DOCUMENT_HINTS = (
    "case-study",
    "information-template",
    "market-information",
    "template",
)
_AUTHORITATIVE_HINTS = (
    "official",
    "principles",
    "standard",
    "regulation",
    "guideline",
    "framework",
    "官方",
    "原则",
    "标准",
    "监管",
    "指引",
    "框架",
)
_SECONDARY_SOURCE_HINTS = (
    "blog",
    "interview",
    "opinion",
    "summary",
    "questions.com",
    "substack",
    "wordpress",
    "博客",
    "访谈",
    "解读",
    "转载",
)


def canonicalize_source_url(url: str) -> str:
    """Normalize one HTTP(S) URL for cross-query and cross-worker reuse."""
    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return raw
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_QUERY_KEYS
        )
    )
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _query_terms(text: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]{1,}|[\u4e00-\u9fff]{2,}", text)
        if token.casefold() not in _QUERY_STOPWORDS
    }


def _candidate_score(
    item: dict[str, Any],
    *,
    query: str,
    preferred_domains: Iterable[str],
) -> float:
    url = str(item.get("url") or "")
    title = str(item.get("title") or "")
    snippet = str(item.get("snippet") or "")
    host = _host(url)
    lowered = f"{title} {url}".casefold()
    score = 0.0
    preferred = tuple(str(value).strip().lower() for value in preferred_domains if str(value).strip())
    if any(host == domain or host.endswith("." + domain) for domain in preferred):
        score += 100.0
    if host.endswith((".gov", ".gov.cn", ".edu", ".edu.cn", ".int")):
        score += 35.0
    elif host.endswith(".org") or ".org." in host:
        score += 20.0
    if urlsplit(url).path.casefold().endswith(".pdf") or "pdf" in str(item.get("content_type") or "").casefold():
        score += 10.0
    score += 4.0 * sum(hint in lowered for hint in _PRIMARY_HINTS)
    score += 10.0 * sum(hint in lowered for hint in _AUTHORITATIVE_HINTS)
    terms = _query_terms(query)
    title_terms = _query_terms(title)
    candidate_terms = _query_terms(f"{title} {snippet}")
    if terms:
        score += 60.0 * len(terms & title_terms) / len(terms)
        score += 20.0 * len(terms & candidate_terms) / len(terms)
        score += 25.0 * sum(
            term in host.replace("-", "")
            for term in terms
            if len(term) >= 4
        )
    if any(hint in lowered for hint in _SECONDARY_DOCUMENT_HINTS):
        score -= 35.0
    if any(hint in lowered for hint in _SECONDARY_SOURCE_HINTS):
        score -= 30.0
    if snippet.strip():
        score += min(5.0, len(snippet.strip()) / 300.0)
    return round(score, 6)


@dataclass
class AcquisitionRegistry:
    """Share opened documents across isolated Worker tool clones."""

    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    inflight: dict[
        str,
        asyncio.Task[tuple[dict[str, Any] | None, dict[str, Any] | None]],
    ] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def get_or_fetch(
        self,
        url: str,
        loader: Callable[
            [],
            Awaitable[tuple[dict[str, Any] | None, dict[str, Any] | None]],
        ],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        canonical = canonicalize_source_url(url)
        async with self._lock:
            cached = self.documents.get(canonical)
            if cached is not None:
                return copy.deepcopy(cached), None, True
            task = self.inflight.get(canonical)
            reused = task is not None
            if task is None:
                task = asyncio.create_task(loader())
                self.inflight[canonical] = task
        try:
            document, error = await task
        finally:
            async with self._lock:
                if self.inflight.get(canonical) is task:
                    self.inflight.pop(canonical, None)
        if document is not None:
            async with self._lock:
                self.documents.setdefault(canonical, copy.deepcopy(document))
        return copy.deepcopy(document), copy.deepcopy(error), reused


class EvidenceAcquisitionTool:
    """Search and open a bounded, high-value set of source documents."""

    name = "acquire_evidence"
    accepts_relevance_query = True
    description = (
        "Acquire source-locatable evidence for one research question. This single action "
        "searches all configured web-search backends with fallback, ranks and deduplicates "
        "candidate URLs, automatically opens the strongest sources, and returns full-content "
        "HTML sections or PDF pages. Use it instead of separate web_search/browser calls."
    )

    def __init__(
        self,
        search_tool: Any,
        browser_tool: Any,
        *,
        default_candidates: int = 8,
        default_sources: int = 3,
        max_sources: int = 6,
        max_chars_per_source: int = 12000,
        registry: AcquisitionRegistry | None = None,
    ) -> None:
        self.search_tool = search_tool
        self.browser_tool = browser_tool
        self.default_candidates = max(1, int(default_candidates))
        self.default_sources = max(1, int(default_sources))
        self.max_sources = max(self.default_sources, int(max_sources))
        self.max_chars_per_source = max(1000, int(max_chars_per_source))
        self.registry = registry or AcquisitionRegistry()

    def __deepcopy__(self, memo: dict[int, Any]) -> "EvidenceAcquisitionTool":
        clone = type(self)(
            copy.deepcopy(self.search_tool, memo),
            copy.deepcopy(self.browser_tool, memo),
            default_candidates=self.default_candidates,
            default_sources=self.default_sources,
            max_sources=self.max_sources,
            max_chars_per_source=self.max_chars_per_source,
            registry=self.registry,
        )
        memo[id(self)] = clone
        return clone

    def clone(self) -> "EvidenceAcquisitionTool":
        return copy.deepcopy(self)

    def fork(self) -> "EvidenceAcquisitionTool":
        return self.clone()

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Requirement-scoped research query",
                        },
                        "top_n": {
                            "type": "integer",
                            "description": "Candidate URLs to consider",
                            "default": self.default_candidates,
                        },
                        "max_sources": {
                            "type": "integer",
                            "description": "Strongest distinct sources to open automatically",
                            "default": self.default_sources,
                        },
                        "preferred_domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional official or primary domains to prioritize",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    @staticmethod
    def _normalize_document(
        raw: Any,
        *,
        requested_url: str,
        title: str,
    ) -> dict[str, Any] | None:
        if isinstance(raw, dict):
            blocks = raw.get("blocks")
            if isinstance(blocks, list) and blocks:
                return {
                    **raw,
                    "url": str(raw.get("url") or requested_url),
                    "title": str(raw.get("title") or title or requested_url),
                }
            return None
        text = str(raw or "").strip()
        if not text or re.match(r"^\[Browser (?:Error|Warning)\]", text, re.IGNORECASE):
            return None
        return {
            "url": requested_url,
            "title": title or requested_url,
            "format": "html",
            "extractor": "legacy-browser",
            "quality_score": 0.5,
            "warnings": ["Legacy Browser output has no section-level locator."],
            "blocks": [
                {
                    "heading": title or requested_url,
                    "locator": requested_url,
                    "text": text,
                    "relevance_score": 0.0,
                }
            ],
        }

    @staticmethod
    def _browser_failure(raw: Any, *, requested_url: str) -> dict[str, Any]:
        """Preserve the Browser's actionable failure instead of masking it."""

        if isinstance(raw, dict):
            warnings = raw.get("warnings")
            return {
                "url": str(raw.get("url") or requested_url),
                "status": str(raw.get("status") or "invalid_content"),
                "error": str(
                    raw.get("error")
                    or "Browser returned no source-locatable content blocks"
                ),
                "warnings": [
                    str(item)
                    for item in warnings
                    if str(item).strip()
                ] if isinstance(warnings, list) else [],
            }
        text = str(raw or "").strip()
        return {
            "url": requested_url,
            "status": "error",
            "error": text or "Browser returned empty content",
            "warnings": [],
        }

    def _rank_candidates(
        self,
        results: Iterable[Any],
        *,
        query: str,
        preferred_domains: Iterable[str],
    ) -> tuple[list[dict[str, Any]], int]:
        unique: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for raw in results:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            canonical = canonicalize_source_url(url)
            if not canonical.startswith(("http://", "https://")):
                continue
            candidate = {
                "title": str(raw.get("title") or canonical),
                "url": url,
                "canonical_url": canonical,
                "snippet": str(raw.get("snippet") or ""),
            }
            candidate["score"] = _candidate_score(
                candidate,
                query=query,
                preferred_domains=preferred_domains,
            )
            previous = unique.get(canonical)
            if previous is not None:
                duplicates += 1
                if candidate["score"] > previous["score"]:
                    unique[canonical] = candidate
                continue
            unique[canonical] = candidate
        ranked = sorted(
            unique.values(),
            key=lambda item: (-float(item["score"]), item["canonical_url"]),
        )
        return ranked, duplicates

    @staticmethod
    def _select_diverse(
        ranked: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        host_counts: dict[str, int] = {}
        for candidate in ranked:
            host = _host(candidate["canonical_url"])
            lowered = f"{candidate.get('title', '')} {candidate.get('canonical_url', '')}".casefold()
            authoritative = host.endswith(
                (".gov", ".gov.cn", ".edu", ".edu.cn", ".int", ".org")
            ) or any(hint in lowered for hint in _AUTHORITATIVE_HINTS)
            host_limit = 2 if authoritative else 1
            if host and host_counts.get(host, 0) >= host_limit:
                deferred.append(candidate)
                continue
            selected.append(candidate)
            if host:
                host_counts[host] = host_counts.get(host, 0) + 1
            if len(selected) >= limit:
                return selected
        selected.extend(deferred[: max(0, limit - len(selected))])
        return selected

    async def execute(
        self,
        query: str,
        top_n: int | None = None,
        max_sources: int | None = None,
        preferred_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        scoped_query = str(query or "").strip()
        if not scoped_query:
            return {"status": "error", "error": "acquire_evidence requires a query"}
        candidate_limit = min(20, max(1, int(top_n or self.default_candidates)))
        source_limit = min(
            self.max_sources,
            max(1, int(max_sources or self.default_sources)),
        )
        domains = tuple(preferred_domains or ())
        search_result = await self.search_tool.execute(scoped_query, top_n=candidate_limit)
        raw_results = search_result.get("results", []) if isinstance(search_result, dict) else []
        ranked, duplicate_count = self._rank_candidates(
            raw_results,
            query=scoped_query,
            preferred_domains=domains,
        )
        selected = self._select_diverse(ranked, source_limit)

        async def open_candidate(
            candidate: dict[str, Any],
        ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
            async def load() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
                raw = await self.browser_tool.execute(
                    url=candidate["url"],
                    max_chars=self.max_chars_per_source,
                    query=scoped_query,
                )
                document = self._normalize_document(
                    raw,
                    requested_url=candidate["url"],
                    title=candidate["title"],
                )
                error = None if document is not None else self._browser_failure(
                    raw,
                    requested_url=candidate["url"],
                )
                return document, error

            return await self.registry.get_or_fetch(candidate["canonical_url"], load)

        opened = await asyncio.gather(*(open_candidate(item) for item in selected))
        documents = [document for document, _, _ in opened if document is not None]
        cache_hits = sum(
            reused for document, _, reused in opened if document is not None
        )
        fetch_errors = [
            error
            for candidate, (document, error, _) in zip(selected, opened)
            if document is None
            for error in (
                error
                or {
                    "url": candidate["url"],
                    "status": "error",
                    "error": "Browser returned no structured content",
                    "warnings": [],
                },
            )
        ]
        metrics = {
            "candidate_count": len(ranked),
            "selected_count": len(selected),
            "opened_count": len(documents),
            "duplicate_candidate_count": duplicate_count,
            "cache_hit_count": cache_hits,
            "search_to_open_rate": (
                len(documents) / len(ranked) if ranked else 0.0
            ),
        }
        response = {
            "query": scoped_query,
            "search_backend": (
                search_result.get("source") if isinstance(search_result, dict) else None
            ),
            "backends_tried": (
                search_result.get("backends_tried", []) if isinstance(search_result, dict) else []
            ),
            "fallback_used": (
                bool(search_result.get("fallback_used", False)) if isinstance(search_result, dict) else False
            ),
            "backend_errors": (
                search_result.get("backend_errors", []) if isinstance(search_result, dict) else []
            ),
            "candidates": ranked,
            "selected_urls": [item["url"] for item in selected],
            "documents": documents,
            "fetch_errors": fetch_errors,
            "metrics": metrics,
        }
        if not documents:
            search_error = search_result.get("error") if isinstance(search_result, dict) else None
            response["status"] = "error"
            response["error"] = str(search_error or "No candidate source produced readable full content")
        return response


__all__ = [
    "AcquisitionRegistry",
    "EvidenceAcquisitionTool",
    "canonicalize_source_url",
]

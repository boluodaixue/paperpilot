"""
论文阅读工具 (ArxivReaderTool) — ArXiv / Semantic Scholar / OpenAlex 自动回退

设计理由：
  ArXiv API 可能受网络和证书链影响，客户端统一加载可信 CA。
  Semantic Scholar API 国内可达且免费，申请 Key 后 rate limit 更高。
  OpenAlex API 基础访问无需 Key，免费 Key 可提高日额度。
  优先使用 .env 中的 ARXIV_READER_BACKEND，并在失败或空结果时自动切换后端。

后端对比：
  - arxiv:              论文库最全，公开 API，网络可用性因环境而异
  - semantic_scholar:   国内可达，覆盖 2 亿+ 论文，含引用数据
  - openalex:           基础访问无需 Key，元数据丰富
"""

from __future__ import annotations

import asyncio
import random
import re
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp

from ..utils.env_config import get_env
from .http_client import trusted_connector

__all__ = ["ArxivReaderTool"]


class ArxivReaderTool:
    """论文读取工具：支持 ArXiv / Semantic Scholar / OpenAlex 自动回退。

    配置优先从 .env / .env.local 读取：
      - ARXIV_READER_BACKEND: 后端选择，可选 "arxiv" | "semantic_scholar" | "openalex"（默认 semantic_scholar）
      - ARXIV_API_ENDPOINT:    ArXiv API 端点（一般不需要改）
      - SEMANTIC_SCHOLAR_API_KEY: Semantic Scholar API Key（免费申请，可选）
      - OPENALEX_API_KEY:      OpenAlex 免费 API Key（可选，建议填写）
      - OPENALEX_EMAIL:        OpenAlex polite 标识邮箱（可选）
    """

    name: str = "arxiv_reader"
    description: str = (
        "Read paper metadata from academic databases. "
        "Supports ArXiv, Semantic Scholar, and OpenAlex backends. "
        "Input: {'paper_id': str(optional), 'query': str(optional), 'max_results': int(default=3)}. "
        "Output: list of paper metadata dicts."
    )

    def __init__(
        self, backend: str | None = None, use_mock: bool = False, delay_ms: tuple[int, int] = (50, 200)
    ) -> None:
        self.backend = (backend or get_env("ARXIV_READER_BACKEND", "semantic_scholar")).lower().strip()
        self.use_mock = use_mock
        self.delay_ms = delay_ms

        # ArXiv 配置
        self.arxiv_base_url = get_env("ARXIV_API_ENDPOINT", "http://export.arxiv.org/api/query")

        # Semantic Scholar 配置
        self.ss_api_key = get_env("SEMANTIC_SCHOLAR_API_KEY")
        self.ss_base_url = "https://api.semanticscholar.org/graph/v1"

        # OpenAlex 配置
        self.openalex_email = get_env("OPENALEX_EMAIL", "")
        self.openalex_api_key = get_env("OPENALEX_API_KEY", "")
        self.openalex_base_url = "https://api.openalex.org"

    def get_openai_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paper_id": {
                            "type": "string",
                            "description": "ArXiv paper ID or Semantic Scholar paper ID, e.g. '1706.03762'",
                        },
                        "query": {
                            "type": "string",
                            "description": "Search query",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results",
                            "default": 3,
                        },
                    },
                    "anyOf": [{"required": ["paper_id"]}, {"required": ["query"]}],
                },
            },
        }

    async def execute(
        self, paper_id: str | None = None, query: str | None = None, max_results: int = 3
    ) -> dict[str, Any]:
        if self.use_mock:
            return await self._mock_execute(paper_id, query, max_results)

        normalized_id = self._normalize_paper_id(paper_id)
        backends = self._backend_order(normalized_id)
        attempts: list[dict[str, str]] = []
        for backend in backends:
            result = await self._execute_backend(
                backend,
                normalized_id,
                query,
                max_results,
            )
            if result.get("papers"):
                return {
                    **result,
                    "backends_tried": [item["backend"] for item in attempts] + [backend],
                    "fallback_used": bool(attempts),
                }
            attempts.append(
                {
                    "backend": backend,
                    "error": str(result.get("error") or "no results"),
                }
            )

        all_failed = all(item["error"] != "no results" for item in attempts)
        detail = " | ".join(f"{item['backend']}: {item['error']}" for item in attempts)
        return {
            "source": "academic_fallback",
            "query": query or normalized_id,
            "papers": [],
            "error": (
                f"All academic metadata backends unavailable: {detail}"
                if all_failed
                else f"No academic metadata results found after fallback: {detail}"
            ),
            "backend_errors": attempts,
            "backends_tried": [item["backend"] for item in attempts],
            "fallback_used": len(attempts) > 1,
        }

    async def _execute_backend(
        self,
        backend: str,
        paper_id: str | None,
        query: str | None,
        max_results: int,
    ) -> dict[str, Any]:
        if backend == "semantic_scholar":
            return await self._semantic_scholar_execute(paper_id, query, max_results)
        if backend == "openalex":
            return await self._openalex_execute(paper_id, query, max_results)
        return await self._arxiv_execute(paper_id, query, max_results)

    def _backend_order(self, paper_id: str | None) -> tuple[str, ...]:
        supported = ("arxiv", "semantic_scholar", "openalex")
        preferred = self.backend if self.backend in supported else "semantic_scholar"
        if paper_id and self._is_arxiv_id(paper_id):
            return supported
        return (preferred, *(item for item in supported if item != preferred))

    @staticmethod
    def _normalize_paper_id(paper_id: str | None) -> str | None:
        value = str(paper_id or "").strip()
        if not value:
            return None
        value = re.sub(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/", "", value, flags=re.I)
        value = re.sub(r"\.pdf$", "", value, flags=re.I)
        value = re.sub(r"^arxiv:", "", value, flags=re.I)
        return value

    @staticmethod
    def _is_arxiv_id(paper_id: str) -> bool:
        return bool(
            re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", paper_id, flags=re.I)
            or re.fullmatch(
                r"[a-z][a-z0-9.-]+/\d{7}(?:v\d+)?",
                paper_id,
                flags=re.I,
            )
        )

    # ------------------------------------------------------------------
    # Mock 模式
    # ------------------------------------------------------------------
    async def _mock_execute(self, paper_id: str | None, query: str | None, max_results: int) -> dict[str, Any]:
        await asyncio.sleep(random.randint(*self.delay_ms) / 1000.0)

        mock_papers = [
            {
                "id": "1706.03762",
                "title": "Attention Is All You Need",
                "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar", "Jakob Uszkoreit"],
                "summary": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks...",
                "published": "2017-06-12",
                "pdf_url": "https://arxiv.org/pdf/1706.03762.pdf",
                "source": "arxiv_mock",
            },
            {
                "id": "1810.04805",
                "title": "BERT: Pre-training of Deep Bidirectional Transformers",
                "authors": ["Jacob Devlin", "Ming-Wei Chang", "Kenton Lee", "Kristina Toutanova"],
                "summary": "We introduce a new language representation model called BERT...",
                "published": "2018-10-11",
                "pdf_url": "https://arxiv.org/pdf/1810.04805.pdf",
                "source": "arxiv_mock",
            },
            {
                "id": "2303.18223",
                "title": "Large Language Models: A Survey",
                "authors": ["Wayne Xin Zhao", "Kun Zhou", "Junyi Li"],
                "summary": "This survey reviews the recent advances in large language models...",
                "published": "2023-03-31",
                "pdf_url": "https://arxiv.org/pdf/2303.18223.pdf",
                "source": "arxiv_mock",
            },
        ]

        if paper_id:
            papers = [p for p in mock_papers if p["id"] == paper_id]
        else:
            q = (query or "").lower()
            papers = [p for p in mock_papers if q in p["title"].lower() or q in p["summary"].lower()]

        return {
            "source": "arxiv_mock",
            "query": query or paper_id,
            "papers": papers[:max_results],
        }

    # ------------------------------------------------------------------
    # ArXiv 后端（论文最全，公开 API）
    # ------------------------------------------------------------------
    async def _arxiv_execute(self, paper_id: str | None, query: str | None, max_results: int) -> dict[str, Any]:
        if paper_id:
            params = {
                "id_list": paper_id,
                "start": 0,
                "max_results": max_results,
            }
        else:
            params = {
                "search_query": f"all:{query}",
                "start": 0,
                "max_results": max_results,
            }

        try:
            async with aiohttp.ClientSession(connector=trusted_connector()) as session:
                async with session.get(
                    self.arxiv_base_url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    resp.raise_for_status()
                    text = await resp.text()
        except Exception as e:
            return {
                "source": "arxiv_api",
                "query": query or paper_id,
                "papers": [],
                "error": "ArXiv API 网络错误；将自动尝试其他学术后端。"
                f"原始错误: {e}",
            }

        # 解析 Atom XML
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            preview = text[:200].replace("\n", " ")
            return {
                "source": "arxiv_api",
                "query": query or paper_id,
                "papers": [],
                "error": f"ArXiv API 返回了无法解析的内容。可能是服务暂时不可用或网络问题。"
                f"内容预览: {preview}... (原始错误: {e})",
            }

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        papers = []
        for entry in root.findall("atom:entry", ns):
            paper = {
                "id": (entry.find("atom:id", ns).text or "").split("/")[-1],
                "title": (entry.find("atom:title", ns).text or "").strip().replace("\n", " "),
                "summary": (entry.find("atom:summary", ns).text or "").strip(),
                "published": entry.find("atom:published", ns).text or "",
                "pdf_url": "",
                "source": "arxiv_api",
            }
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf":
                    paper["pdf_url"] = link.get("href", "")
                    break
            authors = []
            for author in entry.findall("atom:author", ns):
                name_el = author.find("atom:name", ns)
                if name_el is not None:
                    authors.append(name_el.text or "")
            paper["authors"] = authors
            papers.append(paper)

        return {
            "source": "arxiv_api",
            "query": query or paper_id,
            "papers": papers,
        }

    # ------------------------------------------------------------------
    # Semantic Scholar 后端（国内可达，免费）
    # 申请 Key: https://www.semanticscholar.org/product/api#api-key-form
    # ------------------------------------------------------------------
    async def _semantic_scholar_execute(
        self, paper_id: str | None, query: str | None, max_results: int
    ) -> dict[str, Any]:
        headers = {}
        if self.ss_api_key:
            headers["x-api-key"] = self.ss_api_key

        try:
            if paper_id:
                # 直接按 ID 查询
                semantic_id = f"ARXIV:{paper_id}" if self._is_arxiv_id(paper_id) else paper_id
                url = f"{self.ss_base_url}/paper/{semantic_id}"
                params = {"fields": "title,authors,year,abstract,url,citationCount"}
                async with aiohttp.ClientSession(connector=trusted_connector()) as session:
                    async with session.get(
                        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            return {
                                "source": "semantic_scholar",
                                "query": semantic_id,
                                "papers": [],
                                "error": f"Semantic Scholar API 错误: {data.get('message', resp.status)}",
                            }
                        paper = self._ss_paper_to_dict(data)
                        return {
                            "source": "semantic_scholar",
                            "query": semantic_id,
                            "papers": [paper],
                        }
            else:
                # 搜索查询
                url = f"{self.ss_base_url}/paper/search"
                params = {
                    "query": query,
                    "fields": "title,authors,year,abstract,url,citationCount",
                    "limit": max_results,
                }
                async with aiohttp.ClientSession(connector=trusted_connector()) as session:
                    async with session.get(
                        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            return {
                                "source": "semantic_scholar",
                                "query": query,
                                "papers": [],
                                "error": f"Semantic Scholar API 错误: {data.get('message', resp.status)}",
                            }
                        papers = [self._ss_paper_to_dict(p) for p in data.get("data", [])]
                        return {
                            "source": "semantic_scholar",
                            "query": query,
                            "papers": papers,
                        }
        except Exception as e:
            return {
                "source": "semantic_scholar",
                "query": query or paper_id,
                "papers": [],
                "error": f"Semantic Scholar 网络错误: {e}",
            }

    @staticmethod
    def _ss_paper_to_dict(data: dict) -> dict:
        """将 Semantic Scholar 原始数据转为统一格式。"""
        authors = []
        for a in data.get("authors", [])[:10]:
            name = a.get("name", "")
            if name:
                authors.append(name)

        return {
            "id": data.get("paperId", "")[:20],
            "title": data.get("title", ""),
            "authors": authors,
            "summary": data.get("abstract", "") or "",
            "published": str(data.get("year", "")),
            "pdf_url": data.get("url", ""),
            "source": "semantic_scholar",
            "citation_count": data.get("citationCount"),
        }

    # ------------------------------------------------------------------
    # OpenAlex 后端（基础访问无需 Key，免费 Key 可提高日额度）
    # 文档: https://docs.openalex.org/
    # ------------------------------------------------------------------
    async def _openalex_execute(self, paper_id: str | None, query: str | None, max_results: int) -> dict[str, Any]:
        headers = {
            "User-Agent": "deep-research-agent",
            "Accept-Encoding": "gzip, deflate",  # 避免 brotli 解码问题
        }
        common_params: dict[str, Any] = {}
        if self.openalex_email:
            common_params["mailto"] = self.openalex_email
        if self.openalex_api_key:
            common_params["api_key"] = self.openalex_api_key

        try:
            if paper_id:
                openalex_id = self._openalex_lookup_id(paper_id)
                if openalex_id is None:
                    url = f"{self.openalex_base_url}/works"
                    params = {
                        **common_params,
                        "search": paper_id,
                        "per-page": max_results,
                    }
                else:
                    url = f"{self.openalex_base_url}/works/{openalex_id}"
                    params = common_params
                async with aiohttp.ClientSession(connector=trusted_connector()) as session:
                    async with session.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            return {
                                "source": "openalex",
                                "query": paper_id,
                                "papers": [],
                                "error": f"OpenAlex API 错误: {data.get('message', resp.status)}",
                            }
                        raw_papers = data.get("results", []) if openalex_id is None else [data]
                        papers = [self._openalex_paper_to_dict(item) for item in raw_papers]
                        return {
                            "source": "openalex",
                            "query": paper_id,
                            "papers": papers,
                        }
            else:
                # 搜索查询
                url = f"{self.openalex_base_url}/works"
                params = {
                    **common_params,
                    "search": query,
                    "per-page": max_results,
                }
                async with aiohttp.ClientSession(connector=trusted_connector()) as session:
                    async with session.get(
                        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            return {
                                "source": "openalex",
                                "query": query,
                                "papers": [],
                                "error": f"OpenAlex API 错误: {data.get('message', resp.status)}",
                            }
                        papers = [self._openalex_paper_to_dict(r) for r in data.get("results", [])]
                        return {
                            "source": "openalex",
                            "query": query,
                            "papers": papers,
                        }
        except Exception as e:
            return {
                "source": "openalex",
                "query": query or paper_id,
                "papers": [],
                "error": f"OpenAlex 网络错误: {e}",
            }

    @staticmethod
    def _openalex_lookup_id(paper_id: str) -> str | None:
        value = paper_id.strip()
        openalex_match = re.fullmatch(
            r"(?:https?://openalex\.org/)?(W\d+)",
            value,
            flags=re.I,
        )
        if openalex_match:
            return openalex_match.group(1).upper()

        value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
        value = re.sub(r"^doi:", "", value, flags=re.I)
        if re.match(r"^10\.\d{4,9}/\S+$", value, flags=re.I):
            return f"doi:{value}"
        if re.fullmatch(r"pm(?:id|cid):\d+", value, flags=re.I):
            return value.lower()
        return None

    @staticmethod
    def _openalex_paper_to_dict(data: dict) -> dict:
        """将 OpenAlex 原始数据转为统一格式。"""
        authors = []
        for a in data.get("authorships", [])[:10]:
            author_info = a.get("author", {})
            name = author_info.get("display_name", "")
            if name:
                authors.append(name)

        # OpenAlex 的 abstract 是倒排索引，简单处理为空或从 summary 取
        summary = ""
        ab = data.get("abstract_inverted_index")
        if ab:
            # 倒排索引还原为近似文本（按词频排序不够精确，这里简单拼接）
            words = []
            for word, positions in ab.items():
                for pos in positions:
                    while len(words) <= pos:
                        words.append("")
                    words[pos] = word
            summary = " ".join(words)

        # PDF 链接
        pdf_url = ""
        oa = data.get("open_access", {})
        if oa:
            pdf_url = oa.get("oa_url", "") or oa.get("pdf_url", "")
        if not pdf_url:
            # 尝试从 best_oa_location 取
            loc = data.get("best_oa_location", {})
            if loc:
                pdf_url = loc.get("pdf_url", "") or loc.get("landing_page_url", "")

        return {
            "id": (data.get("id") or "").split("/")[-1],
            "title": data.get("display_name", ""),
            "authors": authors,
            "summary": summary,
            "published": str(data.get("publication_year", "")),
            "pdf_url": pdf_url,
            "source": "openalex",
            "citation_count": data.get("cited_by_count"),
        }

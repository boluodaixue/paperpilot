"""
论文阅读工具 (ArxivReaderTool) — 支持多后端：ArXiv / Semantic Scholar / OpenAlex

设计理由：
  ArXiv API 在中国大陆访问不稳定（经常超时/断开）。
  Semantic Scholar API 国内可达且免费，申请 Key 后 rate limit 更高。
  OpenAlex API 国内可达、完全免费、无需 Key，覆盖 2 亿+ 论文。
  通过 .env 中的 ARXIV_READER_BACKEND 切换后端，零源码修改。

后端对比：
  - arxiv:              论文库最全，但国内需 VPN
  - semantic_scholar:   国内可达，覆盖 2 亿+ 论文，含引用数据
  - openalex:           国内可达，完全免费无需 Key，元数据丰富
"""
from __future__ import annotations

import asyncio
import json
import random
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp

from ..utils.env_config import get_env

__all__ = ["ArxivReaderTool"]


class ArxivReaderTool:
    """论文读取工具：支持 ArXiv / Semantic Scholar / OpenAlex 三后端。

    配置优先从 .env / .env.local 读取：
      - ARXIV_READER_BACKEND: 后端选择，可选 "arxiv" | "semantic_scholar" | "openalex"（默认 semantic_scholar）
      - ARXIV_API_ENDPOINT:    ArXiv API 端点（一般不需要改）
      - SEMANTIC_SCHOLAR_API_KEY: Semantic Scholar API Key（免费申请，可选）
      - OPENALEX_EMAIL:        OpenAlex 可选邮箱（提高 rate limit，建议填写）
    """

    name: str = "arxiv_reader"
    description: str = (
        "Read paper metadata from academic databases. "
        "Supports ArXiv, Semantic Scholar, and OpenAlex backends. "
        "Input: {'paper_id': str(optional), 'query': str(optional), 'max_results': int(default=3)}. "
        "Output: list of paper metadata dicts."
    )

    def __init__(self, backend: str | None = None, use_mock: bool = False, delay_ms: tuple[int, int] = (50, 200)) -> None:
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

        if self.backend == "semantic_scholar":
            return await self._semantic_scholar_execute(paper_id, query, max_results)
        if self.backend == "openalex":
            return await self._openalex_execute(paper_id, query, max_results)
        return await self._arxiv_execute(paper_id, query, max_results)

    # ------------------------------------------------------------------
    # Mock 模式
    # ------------------------------------------------------------------
    async def _mock_execute(
        self, paper_id: str | None, query: str | None, max_results: int
    ) -> dict[str, Any]:
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
    # ArXiv 后端（论文最全，国内需 VPN）
    # ------------------------------------------------------------------
    async def _arxiv_execute(
        self, paper_id: str | None, query: str | None, max_results: int
    ) -> dict[str, Any]:
        if paper_id:
            search_query = f"id:{paper_id}"
        else:
            search_query = f"all:{query}"

        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.arxiv_base_url, params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    text = await resp.text()
        except Exception as e:
            return {
                "source": "arxiv_api",
                "query": query or paper_id,
                "papers": [],
                "error": f"ArXiv API 网络错误（国内访问可能需 VPN）。建议切换到 semantic_scholar 后端："
                        f"在 .env 中设置 ARXIV_READER_BACKEND=semantic_scholar。原始错误: {e}",
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
                url = f"{self.ss_base_url}/paper/{paper_id}"
                params = {"fields": "title,authors,year,abstract,url,citationCount"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            return {
                                "source": "semantic_scholar",
                                "query": paper_id,
                                "papers": [],
                                "error": f"Semantic Scholar API 错误: {data.get('message', resp.status)}",
                            }
                        paper = self._ss_paper_to_dict(data)
                        return {
                            "source": "semantic_scholar",
                            "query": paper_id,
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
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
    # OpenAlex 后端（国内可达，完全免费，无需 Key）
    # 文档: https://docs.openalex.org/
    # ------------------------------------------------------------------
    async def _openalex_execute(
        self, paper_id: str | None, query: str | None, max_results: int
    ) -> dict[str, Any]:
        headers = {
            "User-Agent": "deep-research-agent",
            "Accept-Encoding": "gzip, deflate",  # 避免 brotli 解码问题
        }
        if self.openalex_email:
            headers["mailto"] = self.openalex_email

        try:
            if paper_id:
                # 直接按 ID 查询（支持 OpenAlex ID 或 DOI）
                url = f"{self.openalex_base_url}/works/{paper_id}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            return {
                                "source": "openalex",
                                "query": paper_id,
                                "papers": [],
                                "error": f"OpenAlex API 错误: {data.get('message', resp.status)}",
                            }
                        paper = self._openalex_paper_to_dict(data)
                        return {
                            "source": "openalex",
                            "query": paper_id,
                            "papers": [paper],
                        }
            else:
                # 搜索查询
                url = f"{self.openalex_base_url}/works"
                params = {
                    "search": query,
                    "per-page": max_results,
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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

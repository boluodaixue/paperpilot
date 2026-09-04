"""Tavily / 秘塔 / Exa / 博查 / SerpAPI 网页搜索与自动回退。"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Any

import aiohttp

from ..utils.env_config import get_env
from .http_client import trusted_connector

__all__ = ["WebSearchTool", "MockWebSearchTool", "BaseWebSearchTool"]


class BaseWebSearchTool(ABC):
    """网页搜索工具抽象基类。"""

    name: str = "web_search"
    description: str = (
        "Search the web for information. "
        "Supports Tavily / 秘塔AI / Exa / 博查AI / SerpAPI backends with fallback. "
        "Input: {'query': str, 'top_n': int(optional, default=3)}. "
        "Output: list of {'title': str, 'url': str, 'snippet': str}."
    )

    @abstractmethod
    async def execute(self, query: str, top_n: int = 3) -> dict[str, Any]:
        """执行搜索并返回结果。"""
        pass

    def get_openai_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "top_n": {
                            "type": "integer",
                            "description": "返回结果数量",
                            "default": 3,
                        },
                    },
                    "required": ["query"],
                },
            },
        }


class MockWebSearchTool(BaseWebSearchTool):
    """Mock 搜索工具：用于无网络环境的测试和演示。"""

    def __init__(self, delay_ms: tuple[int, int] = (50, 200)) -> None:
        self.delay_ms = delay_ms

    async def execute(self, query: str, top_n: int = 3) -> dict[str, Any]:
        await asyncio.sleep(random.randint(*self.delay_ms) / 1000.0)

        query_lower = query.lower()
        mock_db: dict[str, list[dict]] = {
            "transformer": [
                {
                    "title": "Attention Is All You Need",
                    "url": "https://arxiv.org/abs/1706.03762",
                    "snippet": "We propose a new simple network architecture, the Transformer, based solely on attention mechanisms.",
                },
                {
                    "title": "BERT: Pre-training of Deep Bidirectional Transformers",
                    "url": "https://arxiv.org/abs/1810.04805",
                    "snippet": "BERT obtains new state-of-the-art results on eleven natural language processing tasks.",
                },
            ],
            "llm": [
                {
                    "title": "Large Language Models: A Survey",
                    "url": "https://arxiv.org/abs/2303.18223",
                    "snippet": "This survey reviews the recent advances in large language models, including pre-training, adaptation, and applications.",
                },
            ],
            "python": [
                {
                    "title": "Python Documentation",
                    "url": "https://docs.python.org/3/",
                    "snippet": "Official Python programming language documentation.",
                },
            ],
        }

        results: list[dict] = []
        for keyword, entries in mock_db.items():
            if keyword in query_lower:
                results.extend(entries)

        seen = set()
        unique = []
        for r in results:
            key = r["url"]
            if key not in seen:
                seen.add(key)
                unique.append(r)
        results = unique[:top_n]

        if not results:
            results = [
                {
                    "title": f"Mock result for '{query}'",
                    "url": "https://example.com/mock",
                    "snippet": "This is a mock search result for testing purposes.",
                }
            ]

        return {
            "query": query,
            "results": results,
            "total": len(results),
        }


class WebSearchTool(BaseWebSearchTool):
    """真实网页搜索工具：首选后端失败时按配置自动回退。

    配置优先从 .env / .env.local 读取：
      - SEARCH_BACKEND: 首选后端（默认 tavily）
      - SEARCH_FALLBACK_BACKENDS: 逗号分隔的备用后端
      - TAVILY_API_KEY / TAVILY_API_ENDPOINT: Tavily 配置
      - EXA_API_KEY / EXA_API_ENDPOINT: Exa 配置
      - SERPAPI_KEY / SERPAPI_ENDPOINT: SerpAPI 配置
    """

    _session: aiohttp.ClientSession | None = None
    _supported_backends = (
        "tavily",
        "metaso",
        "exa",
        "bocha",
        "serpapi",
    )

    def __init__(
        self,
        backend: str | None = None,
        api_key: str | None = None,
        api_endpoint: str | None = None,
    ) -> None:
        self.backend = (backend or get_env("SEARCH_BACKEND", "tavily")).lower().strip()
        fallback_config = get_env(
            "SEARCH_FALLBACK_BACKENDS",
            "tavily,metaso,exa,bocha,serpapi",
        )
        self.fallback_backends = tuple(
            item.strip().lower()
            for item in str(fallback_config or "").split(",")
            if item.strip().lower() in self._supported_backends
        )

        def override_for(name: str, env_name: str) -> str | None:
            return api_key if api_key and self.backend == name else get_env(env_name)

        def endpoint_for(name: str, env_name: str, default: str) -> str:
            if api_endpoint and self.backend == name:
                return api_endpoint
            return str(get_env(env_name, default) or default)

        # Tavily / Exa 配置
        self.tavily_key = override_for("tavily", "TAVILY_API_KEY")
        self.tavily_endpoint = endpoint_for(
            "tavily",
            "TAVILY_API_ENDPOINT",
            "https://api.tavily.com/search",
        )
        self.exa_key = override_for("exa", "EXA_API_KEY")
        self.exa_endpoint = endpoint_for(
            "exa",
            "EXA_API_ENDPOINT",
            "https://api.exa.ai/search",
        )

        # SerpAPI 配置
        self.serpapi_key = override_for("serpapi", "SERPAPI_KEY")
        self.serpapi_endpoint = endpoint_for("serpapi", "SERPAPI_ENDPOINT", "https://serpapi.com/search")

        # 博查AI 配置
        self.bocha_key = override_for("bocha", "BOCHA_API_KEY")
        self.bocha_endpoint = endpoint_for("bocha", "BOCHA_API_ENDPOINT", "https://api.bochaai.com/v1/web-search")

        # 秘塔AI 配置
        self.metaso_key = override_for("metaso", "METASO_API_KEY")
        self.metaso_endpoint = endpoint_for(
            "metaso",
            "METASO_API_ENDPOINT",
            "https://metaso.cn/api/v1/search",
        )

    def _get_session(self) -> aiohttp.ClientSession:
        """获取复用的 ClientSession，避免每次搜索新建连接。"""
        if WebSearchTool._session is None or WebSearchTool._session.closed:
            WebSearchTool._session = aiohttp.ClientSession(
                headers={"Accept-Encoding": "gzip, deflate"},
                connector=trusted_connector(),
            )
        return WebSearchTool._session

    @classmethod
    async def close_session(cls) -> None:
        """关闭类级别的共享 session。应在程序退出前调用。"""
        if cls._session is not None and not cls._session.closed:
            await cls._session.close()
            cls._session = None

    def __del__(self):
        """析构时尝试关闭 session（同步环境回退）。"""
        if WebSearchTool._session is not None and not WebSearchTool._session.closed:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.close_session())
            except RuntimeError:
                # 无运行中的事件循环，忽略
                pass

    async def execute(self, query: str, top_n: int = 3) -> dict[str, Any]:
        attempts: list[dict[str, str]] = []
        for backend in self._backend_order():
            try:
                result = await self._execute_backend(backend, query, top_n)
            except Exception as exc:
                result = {
                    "query": query,
                    "results": [],
                    "total": 0,
                    "error": str(exc),
                }
            if result.get("results"):
                return {
                    **result,
                    "backends_tried": [item["backend"] for item in attempts] + [backend],
                    "fallback_used": bool(attempts),
                    "backend_errors": attempts,
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
            "query": query,
            "results": [],
            "total": 0,
            "source": "web_search_fallback",
            "error": (
                f"All web search backends unavailable: {detail}"
                if all_failed
                else f"No web search results found after fallback: {detail}"
            ),
            "backends_tried": [item["backend"] for item in attempts],
            "fallback_used": len(attempts) > 1,
            "backend_errors": attempts,
        }

    def _backend_order(self) -> tuple[str, ...]:
        preferred = self.backend if self.backend in self._supported_backends else "tavily"
        ordered = tuple(dict.fromkeys((preferred, *self.fallback_backends)))
        # Always retain the preferred backend so a missing primary key is visible.
        # Unconfigured optional fallbacks are skipped without making network calls.
        return tuple(backend for backend in ordered if backend == preferred or self._backend_configured(backend))

    def _backend_configured(self, backend: str) -> bool:
        key = {
            "tavily": self.tavily_key,
            "metaso": self.metaso_key,
            "exa": self.exa_key,
            "bocha": self.bocha_key,
            "serpapi": self.serpapi_key,
        }.get(backend)
        if not key:
            return False
        normalized = str(key).strip().lower()
        return not (
            normalized.startswith("your_")
            or normalized.endswith("_here")
            or normalized in {"changeme", "replace-me", "placeholder"}
        )

    async def _execute_backend(
        self,
        backend: str,
        query: str,
        top_n: int,
    ) -> dict[str, Any]:
        if backend == "tavily":
            return await self._tavily_execute(query, top_n)
        if backend == "exa":
            return await self._exa_execute(query, top_n)
        if backend == "bocha":
            return await self._bocha_execute(query, top_n)
        if backend == "metaso":
            return await self._metaso_execute(query, top_n)
        return await self._serpapi_execute(query, top_n)

    async def _tavily_execute(self, query: str, top_n: int) -> dict[str, Any]:
        if not self.tavily_key:
            raise RuntimeError("Tavily unavailable: TAVILY_API_KEY is not configured")

        payload = {
            "query": query,
            "search_depth": "basic",
            "max_results": min(max(1, top_n), 20),
            "include_answer": False,
            "include_raw_content": False,
        }
        headers = {
            "Authorization": f"Bearer {self.tavily_key}",
            "Content-Type": "application/json",
        }
        try:
            session = self._get_session()
            async with session.post(
                self.tavily_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    return {
                        "query": query,
                        "results": [],
                        "total": 0,
                        "error": f"Tavily error: {data.get('detail') or data.get('error') or resp.status}",
                    }
        except Exception as exc:
            return {
                "query": query,
                "results": [],
                "total": 0,
                "error": f"Tavily network error: {exc}",
            }

        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", "")[:1500],
            }
            for item in data.get("results", [])[:top_n]
        ]
        deduplicated = self._deduplicate_results(results)
        return {
            "query": query,
            "results": deduplicated,
            "total": len(deduplicated),
            "source": "tavily",
        }

    async def _exa_execute(self, query: str, top_n: int) -> dict[str, Any]:
        if not self.exa_key:
            raise RuntimeError("Exa unavailable: EXA_API_KEY is not configured")

        payload = {
            "query": query,
            "numResults": min(max(1, top_n), 10),
            "type": "auto",
            "contents": {"highlights": True},
        }
        headers = {
            "x-api-key": self.exa_key,
            "Content-Type": "application/json",
        }
        try:
            session = self._get_session()
            async with session.post(
                self.exa_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    return {
                        "query": query,
                        "results": [],
                        "total": 0,
                        "error": f"Exa error: {data.get('error') or data.get('message') or resp.status}",
                    }
        except Exception as exc:
            return {
                "query": query,
                "results": [],
                "total": 0,
                "error": f"Exa network error: {exc}",
            }

        results = []
        for item in data.get("results", [])[:top_n]:
            highlights = item.get("highlights") or []
            snippet = " ".join(str(value) for value in highlights if value)
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": (snippet or item.get("text") or "")[:1500],
                }
            )
        deduplicated = self._deduplicate_results(results)
        return {
            "query": query,
            "results": deduplicated,
            "total": len(deduplicated),
            "source": "exa",
        }

    async def _serpapi_execute(self, query: str, top_n: int) -> dict[str, Any]:
        if not self.serpapi_key:
            raise RuntimeError(
                "WebSearchTool (serpapi 后端) 需要 API Key。\n"
                "请在 .env 或 .env.local 中设置 SERPAPI_KEY，\n"
                "或构造函数传入: WebSearchTool(api_key='your_key')\n"
                "如需 Mock 模式，请显式使用 MockWebSearchTool()"
            )

        params = {
            "q": query,
            "num": top_n,
            "api_key": self.serpapi_key,
            "engine": "google",
            "gl": "us",
            "hl": "en",
        }

        try:
            session = self._get_session()
            async with session.get(
                self.serpapi_endpoint,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    error_msg = data.get("error", f"HTTP {resp.status}")
                    return {
                        "query": query,
                        "results": [],
                        "total": 0,
                        "error": f"SerpAPI 错误: {error_msg}",
                    }
        except Exception as e:
            return {
                "query": query,
                "results": [],
                "total": 0,
                "error": f"SerpAPI 网络错误: {e}",
            }

        # 解析 SerpAPI 响应
        organic = data.get("organic_results", [])
        results = []
        for item in organic[:top_n]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
            )

        return {
            "query": query,
            "results": results,
            "total": len(results),
            "source": "serpapi",
        }

    async def _bocha_execute(self, query: str, top_n: int) -> dict[str, Any]:
        """博查AI搜索后端。

        文档: https://open.bochaai.com
        特点: 国内网页索引最全，面向 AI Agent 和 RAG 优化，返回结构化摘要。
        """
        if not self.bocha_key:
            raise RuntimeError(
                "WebSearchTool (bocha 后端) 需要 API Key。\n"
                "请在 .env 或 .env.local 中设置 BOCHA_API_KEY，\n"
                "或访问 https://open.bochaai.com 注册获取。\n"
                "如需 Mock 模式，请显式使用 MockWebSearchTool()"
            )

        payload = {
            "query": query,
            "summary": True,
            "freshness": "noLimit",
            "count": top_n,
        }
        headers = {
            "Authorization": f"Bearer {self.bocha_key}",
            "Content-Type": "application/json",
        }

        try:
            session = self._get_session()
            async with session.post(
                self.bocha_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    error_msg = data.get("message", f"HTTP {resp.status}")
                    return {
                        "query": query,
                        "results": [],
                        "total": 0,
                        "error": f"博查AI 错误: {error_msg}",
                    }
        except Exception as e:
            return {
                "query": query,
                "results": [],
                "total": 0,
                "error": f"博查AI 网络错误: {e}",
            }

        # 解析博查响应 — 兼容 web-search 和 ai-search 两种端点返回结构
        results: list[dict] = []

        # 结构 A: /v1/web-search → data.webPages.value[]
        web_pages = data.get("data", {}).get("webPages", {}).get("value", [])
        for item in web_pages[:top_n]:
            results.append(
                {
                    "title": item.get("name", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("snippet", ""),
                }
            )

        # 结构 B: /v1/ai-search → data.messages[] content 里含引用
        if not results:
            messages = data.get("data", {}).get("messages", [])
            for msg in messages[:top_n]:
                content = msg.get("content", "")
                if content:
                    results.append(
                        {
                            "title": msg.get("role", "引用")[:30],
                            "url": "",
                            "snippet": content[:500],
                        }
                    )

        # 去重：同一篇文章的不同 URL（移动端/PC端/转发）会被当作多条结果
        results = self._deduplicate_results(results)

        return {
            "query": query,
            "results": results,
            "total": len(results),
            "source": "bocha",
        }

    def _deduplicate_results(self, results: list[dict]) -> list[dict]:
        """对搜索结果去重：基于规范化 URL 和清洗后的标题。"""
        import re
        from urllib.parse import urlparse

        seen_keys: set[str] = set()
        unique: list[dict] = []

        for r in results:
            raw_url = r.get("url", "")
            raw_title = r.get("title", "").strip()

            # --- URL 规范化 ---
            try:
                parsed = urlparse(raw_url)
                netloc = parsed.netloc.lower()
                path = parsed.path.lower().rstrip("/")

                # 去掉移动端前缀
                for prefix in ("m.", "wap.", "mobile.", "app."):
                    if netloc.startswith(prefix):
                        netloc = netloc[len(prefix) :]
                        break
                # 去掉 www 前缀
                if netloc.startswith("www."):
                    netloc = netloc[4:]

                # 对常见新闻/博客站，只保留域名+路径（去掉查询参数）
                normalized_url = f"{netloc}{path}"
            except Exception:
                normalized_url = raw_url.lower().strip()

            # --- 标题清洗 ---
            # 去掉常见来源后缀，如 " - 虎嗅网"、"_CSDN博客"、"| 人人都是产品经理"
            cleaned_title = re.sub(
                r"[_\-\s|]*(CSDN博客|虎嗅网|人人都是产品经理|36氪|知乎|搜狐|新浪|网易|腾讯|今日头条|飞书云文档|简书|豆瓣|百度文库|原创力文档|道客巴巴|豆丁网|MBA智库文档|外唐智库|未来智库|中研网|中商产业研究院|三个皮匠报告|book118\.com|doc88\.com|docin\.com|mbalib\.com|askci\.com|chinairn\.com|vzkoo\.com|waitang\.com|sgpjbg\.com|toutiao\.com|sohu\.com|sina\.com|163\.com|qq\.com|ifeng\.com|huxiu\.com|36kr\.com|woshipm\.com|csdn\.net|zhihu\.com|juejin\.cn|segmentfault\.com|cnblogs\.com|简书|知乎专栏|百家号|大鱼号|企鹅号|新浪看点|一点资讯|趣头条|东方财富|雪球|同花顺|财联社|华尔街见闻|界面新闻|澎湃|新京报|南方周末|财新|第一财经|经济观察网|21世纪经济报道|新浪财经|腾讯财经|网易财经|凤凰财经|和讯网|中金在线|东方财富网|中国证券报|上海证券报|证券时报|证券日报|每日经济新闻|第一财经日报|经济参考报|人民日报|新华社|央视新闻|中央广播电视总台|中国日报|环球时报|参考消息|瞭望|半月谈|求是|学习强国|新华网|人民网|中国网|国际在线|中国新闻网|环球网等?)",
                "",
                raw_title,
                flags=re.IGNORECASE,
            ).strip()
            # 再去掉末尾的 " - "、" | "、"_"
            cleaned_title = re.sub(r"[_\-\s|]+$", "", cleaned_title).strip()

            # --- 去重键：优先用 URL，URL 为空时用清洗后的标题 ---
            key = normalized_url if normalized_url else cleaned_title.lower()
            if not key:
                unique.append(r)
                continue

            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique.append(r)

        return unique

    async def _metaso_execute(self, query: str, top_n: int) -> dict[str, Any]:
        """秘塔AI搜索后端。

        文档: https://metaso.cn/search-api/playground
        """
        if not self.metaso_key:
            raise RuntimeError(
                "WebSearchTool (metaso 后端) 需要 API Key。\n"
                "请在 .env 或 .env.local 中设置 METASO_API_KEY，\n"
                "或访问 https://metaso.cn/search-api/api-keys 创建。\n"
                "如需 Mock 模式，请显式使用 MockWebSearchTool()"
            )

        payload = {
            "q": query,
            "scope": "webpage",
            "size": str(min(max(1, top_n), 20)),
            "includeSummary": False,
            "includeRawContent": False,
            "conciseSnippet": True,
        }
        headers = {
            "Authorization": f"Bearer {self.metaso_key}",
            "Content-Type": "application/json",
        }

        try:
            session = self._get_session()
            async with session.post(
                self.metaso_endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if resp.status != 200 or data.get("errCode"):
                    error_msg = data.get("errMsg", f"HTTP {resp.status}")
                    return {
                        "query": query,
                        "results": [],
                        "total": 0,
                        "error": f"秘塔AI 错误: {error_msg}",
                    }
        except Exception as e:
            return {
                "query": query,
                "results": [],
                "total": 0,
                "error": f"秘塔AI 网络错误: {e}",
            }

        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", "")[:1500],
            }
            for item in data.get("webpages", [])[:top_n]
        ]
        results = self._deduplicate_results(results)

        return {
            "query": query,
            "results": results,
            "total": len(results),
            "source": "metaso",
        }

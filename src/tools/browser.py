"""
浏览器工具 (BrowserTool) — 网页全文阅读器

设计理由：
  web_search 只能返回搜索摘要（通常 100-200 字），而深度研究需要阅读网页原文。
  BrowserTool 负责：打开 URL → 提取正文 → 清理广告/导航栏 → 返回结构化文本。

与 web_search 的关系：
  web_search: "找到可能有信息的链接"
  browser: "读这个链接里的具体内容"
  两者是上下游，不是重复。

实现要点：
  - 使用 aiohttp 异步抓取，避免阻塞事件循环
  - 用 BeautifulSoup 提取正文（去除 script/style/nav 等噪声标签）
  - 自动截断超长页面（保留前 N 个段落，防止 token 爆炸）
  - 支持重试和错误降级
"""

from __future__ import annotations

import asyncio
import io
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .http_client import trusted_connector
from .content_extraction import (
    ContentExtractionConfig,
    UnsupportedScannedPdfError,
    extract_html_document,
    extract_markdown_document,
    extract_pdf_document,
    validate_content_extraction_dependencies,
)

__all__ = ["BrowserTool", "MockBrowserTool", "FetchedContent"]

# 默认保留的正文字数上限（防止超长网页占满上下文）
_DEFAULT_MAX_CHARS = 8000

# HTML 中通常包含正文的标签
_CONTENT_TAGS = ["article", "main", "section", "div"]
# 噪声标签（直接移除）
_NOISE_TAGS = ["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe", "svg"]


@dataclass(frozen=True)
class FetchedContent:
    """Bounded response body and the provenance needed by extractors."""

    final_url: str
    content_type: str
    payload: bytes
    charset: str = "utf-8"


class BaseBrowserTool(ABC):
    """浏览器工具基类。"""

    name: str = "browser"
    accepts_relevance_query: bool = True
    description: str = (
        "Open a URL and extract the main article text. "
        "Use this after web_search when you need to read the full content of a webpage. "
        "Input: {'url': str, 'max_chars': int(optional, default=8000)}. "
        "Output: extracted text content."
    )

    @abstractmethod
    async def execute(
        self,
        url: str,
        max_chars: int = _DEFAULT_MAX_CHARS,
        query: str = "",
    ) -> Any:
        """打开 URL，提取正文。

        Args:
            url: 要访问的网页地址。
            max_chars: 返回的最大字符数，超出则截断。

        Returns:
            提取后的正文文本。
        """
        ...

    def get_openai_tool_schema(self) -> dict:
        """返回 OpenAI Function Calling 格式的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL of the webpage to open and read",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Maximum characters to return (default: 8000)",
                            "default": 8000,
                        },
                        "query": {
                            "type": "string",
                            "description": "Current research question used to rank relevant sections",
                        },
                    },
                    "required": ["url"],
                },
            },
        }


class BrowserTool(BaseBrowserTool):
    """真实浏览器工具：异步 HTTP 抓取 + 正文提取。

    配置优先从 .env / .env.local 读取，构造函数参数仅作为覆盖。
    支持的环境变量：
      - BROWSER_TIMEOUT: HTTP 请求超时秒数（默认 15）
      - BROWSER_USER_AGENT: 自定义 User-Agent
    """

    def __init__(
        self,
        timeout: int | None = None,
        user_agent: str | None = None,
        extraction_config: ContentExtractionConfig | None = None,
    ) -> None:
        from ..utils.env_config import get_env, get_env_int

        self.timeout = timeout or get_env_int("BROWSER_TIMEOUT", 15)
        self.user_agent = user_agent or get_env(
            "BROWSER_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        self.extraction_config = extraction_config or ContentExtractionConfig()
        validate_content_extraction_dependencies(self.extraction_config)
        self.tavily_extract_key = get_env("TAVILY_API_KEY")
        self.tavily_extract_endpoint = str(
            get_env("TAVILY_EXTRACT_ENDPOINT", "https://api.tavily.com/extract") or "https://api.tavily.com/extract"
        )

    async def execute(
        self,
        url: str,
        max_chars: int = _DEFAULT_MAX_CHARS,
        query: str = "",
    ) -> Any:
        if not url.startswith(("http://", "https://")):
            return f"[Browser Error] Invalid URL: {url}. URL must start with http:// or https://"

        if self.extraction_config.mode == "structured":
            return await self._execute_structured(url, query=query)

        try:
            html = await self._fetch(url)
            text = self._extract_text(html)
            text = self._clean_text(text)

            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n[CONTENT_TRUNCATED: {len(text)} chars total, showing first {max_chars}]"

            return text if text else "[Browser Warning] No meaningful content extracted from the page."

        except aiohttp.ClientResponseError as e:
            if e.status == 403:
                alternative = await self._fetch_safe_alternative(url, max_chars)
                if alternative is not None:
                    alternative_url, alternative_text = alternative
                    return (
                        f"[ALTERNATIVE_SOURCE: {alternative_url}]\n"
                        f"[ORIGINAL_SOURCE_BLOCKED: {url}]\n\n"
                        f"{alternative_text}"
                    )
            return f"[Browser Error] Network error: {type(e).__name__}: {e}"
        except aiohttp.ClientError as e:
            return f"[Browser Error] Network error: {type(e).__name__}: {e}"
        except Exception as e:
            return f"[Browser Error] Unexpected: {type(e).__name__}: {e}"

    async def _execute_structured(self, url: str, *, query: str) -> dict[str, Any]:
        try:
            fetched = await self._fetch_payload(url)
            return await self._extract_structured_payload(
                fetched,
                original_url=url,
                query=query,
            )
        except UnsupportedScannedPdfError as exc:
            return {
                "status": "unsupported",
                "error": str(exc),
                "url": url,
                "warnings": ["OCR is disabled; scanned PDFs are out of scope."],
            }
        except aiohttp.ClientResponseError as exc:
            if exc.status == 403:
                for alternative_url in self._alternative_urls(url):
                    try:
                        fetched = await self._fetch_payload(alternative_url)
                        result = await self._extract_structured_payload(
                            fetched,
                            original_url=url,
                            query=query,
                        )
                        result.setdefault("warnings", []).append(
                            "Official alternative source used because the requested page was blocked."
                        )
                        return result
                    except (aiohttp.ClientError, ValueError):
                        continue
            return {
                "status": "error",
                "error": f"Network error: {type(exc).__name__}: {exc}",
                "url": url,
            }
        except aiohttp.ClientError as exc:
            return {
                "status": "error",
                "error": f"Network error: {type(exc).__name__}: {exc}",
                "url": url,
            }
        except Exception as exc:
            return {
                "status": "error",
                "error": f"Extraction error: {type(exc).__name__}: {exc}",
                "url": url,
            }

    async def _extract_structured_payload(
        self,
        fetched: FetchedContent,
        *,
        original_url: str,
        query: str,
    ) -> dict[str, Any]:
        provenance_url = original_url if fetched.final_url != original_url else ""
        if fetched.content_type.split(";", 1)[0].strip().lower() == "application/pdf" or urlparse(
            fetched.final_url
        ).path.lower().endswith(".pdf"):
            document = extract_pdf_document(
                fetched.payload,
                url=fetched.final_url,
                original_url=provenance_url,
                query=query,
                max_blocks=self.extraction_config.max_blocks,
                max_output_chars=self.extraction_config.max_output_chars,
                docling_enabled=self.extraction_config.docling_enabled,
            )
            return document.to_dict()
        charset = fetched.charset or "utf-8"
        html = fetched.payload.decode(charset, errors="replace")
        document = None
        local_error: ValueError | None = None
        try:
            document = extract_html_document(
                html,
                url=fetched.final_url,
                original_url=provenance_url,
                query=query,
                max_blocks=self.extraction_config.max_blocks,
                max_output_chars=self.extraction_config.max_output_chars,
            )
        except ValueError as exc:
            local_error = exc
        if self.extraction_config.tavily_extract_fallback and (
            document is None or document.quality_score < 0.45
        ):
            fallback = await self._tavily_extract(fetched.final_url)
            if fallback is not None:
                fallback_title, fallback_markdown = fallback
                fallback_document = extract_markdown_document(
                    fallback_markdown,
                    url=fetched.final_url,
                    original_url=provenance_url,
                    title=(
                        fallback_title
                        or (document.title if document is not None else fetched.final_url)
                    ),
                    query=query,
                    extractor="tavily-extract",
                    max_blocks=self.extraction_config.max_blocks,
                    max_output_chars=self.extraction_config.max_output_chars,
                    warnings=(
                        "Local HTML extraction failed or had low quality; Tavily Extract fallback used.",
                    ),
                )
                if (
                    document is None
                    or fallback_document.quality_score >= document.quality_score
                ):
                    document = fallback_document
        if document is None:
            raise local_error or ValueError("no meaningful source blocks extracted")
        return document.to_dict()

    async def _fetch_payload(self, url: str) -> FetchedContent:
        """Fetch a response under a hard byte bound for structured extraction."""

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": self.user_agent},
            connector=trusted_connector(),
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > self.extraction_config.max_download_bytes:
                        raise ValueError("response exceeds content_extraction.max_download_bytes")
                    chunks.append(chunk)
                return FetchedContent(
                    final_url=str(resp.url),
                    content_type=str(resp.headers.get("Content-Type") or resp.content_type or ""),
                    payload=b"".join(chunks),
                    charset=resp.charset or "utf-8",
                )

    async def _tavily_extract(self, url: str) -> tuple[str, str] | None:
        """Use Tavily Extract once, only after poor local HTML extraction."""

        if not self.tavily_extract_key:
            return None
        headers = {
            "Authorization": f"Bearer {self.tavily_extract_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "urls": [url],
            "format": "markdown",
            "include_images": False,
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            connector=trusted_connector(),
        ) as session:
            async with session.post(
                self.tavily_extract_endpoint,
                headers=headers,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        first = results[0]
        content = first.get("raw_content") or first.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return str(first.get("title") or ""), content.strip()

    async def _fetch(self, url: str) -> str:
        """异步获取网页 HTML。"""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": self.user_agent},
            connector=trusted_connector(),
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                resp.raise_for_status()
                if resp.content_type == "application/pdf" or urlparse(str(resp.url)).path.lower().endswith(".pdf"):
                    return self._extract_pdf_text(await resp.read())
                # 尝试自动检测编码
                charset = resp.charset or "utf-8"
                return await resp.text(encoding=charset)

    async def _fetch_safe_alternative(
        self,
        url: str,
        max_chars: int,
    ) -> tuple[str, str] | None:
        for alternative_url in self._alternative_urls(url):
            try:
                content = await self._fetch(alternative_url)
                text = self._clean_text(self._extract_text(content))
                if not text:
                    continue
                if len(text) > max_chars:
                    text = text[:max_chars] + (
                        f"\n\n[CONTENT_TRUNCATED: {len(text)} chars total, " f"showing first {max_chars}]"
                    )
                return alternative_url, text
            except (aiohttp.ClientError, ValueError):
                continue
        return None

    @staticmethod
    def _alternative_urls(url: str) -> tuple[str, ...]:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
        if host in {"openai.com", "www.openai.com"}:
            if path.lower() == "/index/hello-gpt-4o":
                return ("https://cdn.openai.com/gpt-4o-system-card.pdf",)
            match = re.fullmatch(r"/(?:index/)?([^/]*system-card[^/]*)", path, re.I)
            if match:
                return (f"https://cdn.openai.com/{match.group(1)}.pdf",)
        if host in {"arxiv.org", "www.arxiv.org"}:
            match = re.fullmatch(r"/abs/(.+)", path, re.I)
            if match:
                return (f"https://arxiv.org/pdf/{match.group(1)}.pdf",)
        return ()

    @staticmethod
    def _extract_pdf_text(payload: bytes) -> str:
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(payload))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            raise ValueError(f"unable to extract PDF text: {exc}") from exc

    def _extract_text(self, html: str) -> str:
        """从 HTML 中提取正文。"""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            # 降级：简单正则提取
            return self._fallback_extract(html)

        soup = BeautifulSoup(html, "html.parser")

        # 移除噪声标签
        for tag_name in _NOISE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 策略 1：找 article 或 main 标签（语义化 HTML 常用）
        for tag_name in ["article", "main"]:
            tag = soup.find(tag_name)
            if tag:
                return tag.get_text(separator="\n", strip=True)

        # 策略 2：找最长的 div（启发式：正文通常在最长的 div 中）
        best_div = None
        best_len = 0
        for div in soup.find_all("div"):
            text_len = len(div.get_text(strip=True))
            if text_len > best_len:
                best_len = text_len
                best_div = div

        if best_div and best_len > 200:
            return best_div.get_text(separator="\n", strip=True)

        # 策略 3：降级到整个 body
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)

        return soup.get_text(separator="\n", strip=True)

    def _fallback_extract(self, html: str) -> str:
        """无 BeautifulSoup 时的降级提取。"""
        # 移除 script/style 内容
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # 移除所有标签，保留文本
        text = re.sub(r"<[^>]+>", "\n", html)
        return self._clean_text(text)

    @staticmethod
    def _clean_text(text: str) -> str:
        """清理提取后的文本。"""
        # 合并多余换行
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        # 去除每行首尾空白
        lines = [line.strip() for line in text.splitlines()]
        # 过滤空行和过短行（通常是导航项）
        lines = [line for line in lines if len(line) > 3]
        return "\n".join(lines)


class MockBrowserTool(BaseBrowserTool):
    """Mock 浏览器工具：用于无网络环境调试。"""

    _MOCK_PAGES: dict[str, str] = {
        "https://example.com/ai-report-2024": """
2024 年全球人工智能发展报告

摘要
2024 年，全球 AI 产业进入爆发期。大语言模型参数量突破万亿级别，
多模态能力显著增强。中美两国在 AI 基础研究和应用落地上持续领跑。

一、市场规模
据 IDC 统计，2024 年全球 AI 市场规模达到 5,540 亿美元，同比增长 38.2%。
其中，生成式 AI 占比 28%，约 1,551 亿美元。

二、技术进展
1. 大语言模型：GPT-4o、Claude 3.5、Gemini 1.5 Pro 等模型在多模态推理上取得突破
2. 代码生成：GitHub Copilot 月活开发者超过 500 万
3. 科学发现：AlphaFold 3 预测几乎所有生物分子结构

三、主要玩家
- OpenAI：估值 1,570 亿美元，年化收入 34 亿美元
- Anthropic：估值 400 亿美元，Claude 系列增长迅速
- Google DeepMind：Gemini 整合进全线产品
- 百度：文心一言用户数突破 3 亿

四、政策与监管
欧盟《人工智能法案》于 2024 年 8 月正式生效，成为全球首部全面监管 AI 的法律。

来源：IDC、OpenAI Blog、Anthropic 官方公告
        """.strip(),
        "https://example.com/quantum-computing": """
量子计算最新进展（2024）

IBM 于 2024 年 12 月发布了 Condor 量子处理器，拥有 1,121 个量子比特，
是目前 publicly available 的最大量子处理器。

Google Quantum AI 团队实现了 surface code 纠错的关键里程碑，
逻辑错误率首次低于物理错误率。

中国科学技术大学潘建伟团队实现了 255 光子量子计算优越性。

来源：IBM Research Blog、Nature、中国科学技术大学官网
        """.strip(),
    }

    async def execute(
        self,
        url: str,
        max_chars: int = _DEFAULT_MAX_CHARS,
        query: str = "",
    ) -> str:
        await asyncio.sleep(0.1)  # 模拟网络延迟

        # 精确匹配
        if url in self._MOCK_PAGES:
            content = self._MOCK_PAGES[url]
            if len(content) > max_chars:
                content = content[:max_chars] + "\n\n[CONTENT_TRUNCATED]"
            return content

        # 模糊匹配：根据 URL 关键词返回通用 mock
        if "wikipedia" in url.lower():
            return f"[Mock Browser] Wikipedia page for {url}\n\nThis is a mock Wikipedia article. In production, BrowserTool would fetch the real Wikipedia content."

        return f"[Mock Browser] Fetched {url}\n\nThis is a generic mock page. Content would be extracted from the real webpage in production mode."


def get_browser_tool(mock_mode: bool = False, **kwargs) -> BaseBrowserTool:
    """工厂函数：根据配置返回 BrowserTool 或 MockBrowserTool。"""
    if mock_mode:
        return MockBrowserTool()
    return BrowserTool(**kwargs)

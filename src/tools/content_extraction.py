"""Structured full-content extraction contracts and rollout configuration.

The contracts intentionally contain only JSON-safe values because Browser
results are checkpointed and passed across the Research Agent tool boundary.
"""

from __future__ import annotations

import atexit
import importlib
import re
import io
import multiprocessing as mp
import os
import tempfile
import threading
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Mapping
from urllib.parse import urljoin

__all__ = [
    "ContentExtractionConfig",
    "ExtractedBlock",
    "ExtractedDocument",
    "content_extraction_config_from_config",
    "extract_html_document",
    "extract_markdown_document",
    "extract_pdf_document",
    "validate_content_extraction_dependencies",
    "UnsupportedScannedPdfError",
]


@dataclass(frozen=True)
class ContentExtractionConfig:
    """Feature flags and hard bounds for the Browser extraction pipeline."""

    mode: str = "legacy"
    tavily_extract_fallback: bool = False
    docling_enabled: bool = False
    ocr_enabled: bool = False
    max_download_bytes: int = 12_000_000
    max_blocks: int = 24
    max_output_chars: int = 24_000

    def validate(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"legacy", "structured"}:
            raise ValueError("content_extraction.mode must be 'legacy' or 'structured'")
        for key in (
            "tavily_extract_fallback",
            "docling_enabled",
            "ocr_enabled",
        ):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"content_extraction.{key} must be a boolean")
        if self.ocr_enabled:
            raise ValueError("content_extraction OCR is unsupported in this personal-project rollout")
        bounds = {
            "max_download_bytes": (self.max_download_bytes, 100_000, 100_000_000),
            "max_blocks": (self.max_blocks, 1, 100),
            "max_output_chars": (self.max_output_chars, 1_000, 200_000),
        }
        for key, (value, lower, upper) in bounds.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"content_extraction.{key} must be an integer")
            if not lower <= value <= upper:
                raise ValueError(f"content_extraction.{key} must be between {lower} and {upper}")


def validate_content_extraction_dependencies(
    config: ContentExtractionConfig,
) -> None:
    """Fail before research starts when an enabled extractor cannot import."""

    config.validate()
    if config.mode != "structured":
        return
    required = [
        ("bs4", "beautifulsoup4"),
        ("markdownify", "markdownify"),
        ("pypdf", "pypdf"),
    ]
    if config.docling_enabled:
        required.append(("docling", "docling (install the 'documents' extra)"))
    missing: list[str] = []
    failures: list[str] = []
    for module_name, package_name in required:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                missing.append(package_name)
            else:
                failures.append(f"{module_name} requires missing module {exc.name}")
        except Exception as exc:
            failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
    if missing or failures:
        details = []
        if missing:
            details.append("missing packages: " + ", ".join(missing))
        if failures:
            details.append("import failures: " + "; ".join(failures))
        raise RuntimeError(
            "Structured content extraction dependency check failed ("
            + "; ".join(details)
            + "). Install this project in the active Python environment before starting research."
        )


@dataclass(frozen=True)
class ExtractedBlock:
    """One source-locatable passage selected from a fetched document."""

    locator: str
    heading: str
    text: str
    relevance_score: float = 0.0

    def validate(self) -> None:
        if not isinstance(self.locator, str) or not self.locator.strip():
            raise ValueError("ExtractedBlock.locator cannot be empty")
        if not isinstance(self.heading, str):
            raise ValueError("ExtractedBlock.heading must be a string")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("ExtractedBlock.text cannot be empty")
        if isinstance(self.relevance_score, bool) or not isinstance(self.relevance_score, (int, float)):
            raise ValueError("ExtractedBlock.relevance_score must be numeric")
        if self.relevance_score < 0:
            raise ValueError("ExtractedBlock.relevance_score cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(frozen=True)
class ExtractedDocument:
    """A bounded, structured Browser result with provenance and warnings."""

    url: str
    title: str
    format: str
    extractor: str
    blocks: tuple[ExtractedBlock, ...]
    quality_score: float
    warnings: tuple[str, ...] = ()
    original_url: str = ""

    def validate(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith(("http://", "https://")):
            raise ValueError("ExtractedDocument.url must be an HTTP(S) URL")
        if self.original_url and not self.original_url.startswith(("http://", "https://")):
            raise ValueError("ExtractedDocument.original_url must be an HTTP(S) URL")
        if self.format not in {"html", "pdf", "markdown", "text"}:
            raise ValueError("ExtractedDocument.format is unsupported")
        if not isinstance(self.extractor, str) or not self.extractor.strip():
            raise ValueError("ExtractedDocument.extractor cannot be empty")
        if not isinstance(self.blocks, tuple) or not self.blocks:
            raise ValueError("ExtractedDocument.blocks cannot be empty")
        for block in self.blocks:
            if not isinstance(block, ExtractedBlock):
                raise ValueError("ExtractedDocument.blocks must contain ExtractedBlock")
            block.validate()
        if isinstance(self.quality_score, bool) or not isinstance(self.quality_score, (int, float)):
            raise ValueError("ExtractedDocument.quality_score must be numeric")
        if not 0 <= self.quality_score <= 1:
            raise ValueError("ExtractedDocument.quality_score must be between 0 and 1")
        if any(not isinstance(item, str) or not item.strip() for item in self.warnings):
            raise ValueError("ExtractedDocument.warnings must contain non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "status": "ok",
            "url": self.url,
            "original_url": self.original_url or self.url,
            "title": self.title,
            "format": self.format,
            "extractor": self.extractor,
            "quality_score": float(self.quality_score),
            "blocks": [block.to_dict() for block in self.blocks],
            "warnings": list(self.warnings),
        }


class UnsupportedScannedPdfError(ValueError):
    """Raised when a PDF has no extractable text and OCR is disabled."""


def content_extraction_config_from_config(
    config: Mapping[str, Any],
) -> ContentExtractionConfig:
    """Strictly parse the opt-in content extraction feature switches."""

    raw = config.get("content_extraction", {})
    if not isinstance(raw, Mapping):
        raise ValueError("content_extraction configuration must be a mapping")
    known = {
        "mode",
        "tavily_extract_fallback",
        "docling_enabled",
        "ocr_enabled",
        "max_download_bytes",
        "max_blocks",
        "max_output_chars",
    }
    unknown = sorted(str(key) for key in raw if key not in known)
    if unknown:
        raise ValueError("unknown content_extraction settings: " + ", ".join(unknown))
    settings = ContentExtractionConfig(**dict(raw))
    settings.validate()
    return settings


_HTML_NOISE_TAGS = (
    "script",
    "style",
    "nav",
    "header",
    "footer",
    "aside",
    "noscript",
    "iframe",
    "svg",
    "form",
    "button",
)


def _query_terms(query: str) -> tuple[str, ...]:
    terms = re.findall(r"[\w][\w+./-]{1,}", unicodedata.normalize("NFKC", query).lower())
    return tuple(dict.fromkeys(term for term in terms if len(term) > 1))


def _slug(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", normalized, flags=re.UNICODE)
    return normalized.strip("-")[:80] or fallback


def _score_block(heading: str, text: str, terms: tuple[str, ...]) -> float:
    if not terms:
        return 1.0
    heading_lower = heading.lower()
    text_lower = text.lower()
    score = 0.0
    for term in terms:
        if term in heading_lower:
            score += 4.0
        if term in text_lower:
            score += 1.0 + min(text_lower.count(term), 3) * 0.25
    return score


def _split_markdown(
    markdown: str,
    query: str,
    default_heading: str = "Document introduction",
) -> list[ExtractedBlock]:
    """Split Markdown on ATX headings while retaining inline Markdown."""

    blocks: list[ExtractedBlock] = []
    heading = default_heading.strip() or "Document introduction"
    body: list[str] = []
    occurrences: dict[str, int] = {}
    terms = _query_terms(query)

    def flush() -> None:
        text = "\n".join(body).strip()
        if len(re.sub(r"\s+", " ", text)) < 20:
            return
        base = _slug(heading, f"section-{len(blocks) + 1}")
        occurrences[base] = occurrences.get(base, 0) + 1
        suffix = f"-{occurrences[base]}" if occurrences[base] > 1 else ""
        blocks.append(
            ExtractedBlock(
                locator=f"section:{base}{suffix}",
                heading=heading.strip() or "Untitled section",
                text=text,
                relevance_score=_score_block(heading, text, terms),
            )
        )

    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line.strip())
        if match:
            flush()
            heading = match.group(1).strip()
            body = []
        else:
            body.append(line)
    flush()
    return blocks


def _quality_score(markdown: str, blocks: list[ExtractedBlock], query: str) -> float:
    normalized_lines = [
        re.sub(r"\s+", " ", line).strip().lower() for line in markdown.splitlines() if len(line.strip()) > 3
    ]
    unique_ratio = len(set(normalized_lines)) / len(normalized_lines) if normalized_lines else 0.0
    length_score = min(len(markdown) / 4_000, 1.0)
    block_score = min(len(blocks) / 4, 1.0)
    terms = _query_terms(query)
    coverage = sum(1 for term in terms if term in markdown.lower()) / len(terms) if terms else 1.0
    return round(
        max(0.0, min(1.0, 0.35 * length_score + 0.2 * block_score + 0.25 * unique_ratio + 0.2 * coverage)),
        3,
    )


def _select_blocks(
    blocks: list[ExtractedBlock],
    *,
    max_blocks: int,
    max_output_chars: int,
) -> tuple[ExtractedBlock, ...]:
    ranked = sorted(
        enumerate(blocks),
        key=lambda item: (-item[1].relevance_score, item[0]),
    )
    selected: list[ExtractedBlock] = []
    remaining = max_output_chars
    for _, block in ranked:
        if len(selected) >= max_blocks or remaining <= 0:
            break
        text = block.text
        if len(text) > remaining:
            if remaining < 100:
                break
            text = text[:remaining].rstrip() + "\n\n[BLOCK_TRUNCATED]"
        selected.append(
            ExtractedBlock(
                locator=block.locator,
                heading=block.heading,
                text=text,
                relevance_score=block.relevance_score,
            )
        )
        remaining -= len(text)
    return tuple(selected)


def extract_markdown_document(
    markdown: str,
    *,
    url: str,
    title: str,
    query: str,
    extractor: str,
    max_blocks: int,
    max_output_chars: int,
    document_format: str = "markdown",
    warnings: tuple[str, ...] = (),
    original_url: str = "",
) -> ExtractedDocument:
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    blocks = _split_markdown(markdown, query, title)
    if not blocks:
        raise ValueError("no meaningful source blocks extracted")
    quality = _quality_score(markdown, blocks, query)
    return ExtractedDocument(
        url=url,
        original_url=original_url,
        title=title.strip() or url,
        format=document_format,
        extractor=extractor,
        blocks=_select_blocks(
            blocks,
            max_blocks=max_blocks,
            max_output_chars=max_output_chars,
        ),
        quality_score=quality,
        warnings=warnings,
    )


def extract_html_document(
    html: str,
    *,
    url: str,
    query: str,
    max_blocks: int,
    max_output_chars: int,
    original_url: str = "",
) -> ExtractedDocument:
    """Clean static HTML, convert it to Markdown, and rank locatable sections."""

    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    for tag_name in _HTML_NOISE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in soup.find_all(attrs={"role": re.compile(r"navigation|menu|banner|complementary", re.I)}):
        tag.decompose()
    root = _select_html_root(soup, query)
    # Make relative links useful after the HTML leaves its source page.
    for anchor in root.find_all("a", href=True):
        anchor["href"] = urljoin(url, str(anchor["href"]))
    markdown = markdownify(
        str(root),
        heading_style="ATX",
        bullets="-",
        strip=("img",),
    )
    return extract_markdown_document(
        markdown,
        url=url,
        original_url=original_url,
        title=title,
        query=query,
        extractor="beautifulsoup+markdownify",
        max_blocks=max_blocks,
        max_output_chars=max_output_chars,
        document_format="html",
    )


def _select_html_root(soup: Any, query: str) -> Any:
    """Choose the query-relevant content container, not the whole site shell."""

    terms = _query_terms(query)
    candidates = []
    seen: set[int] = set()
    selectors = (
        "article",
        "main",
        "[role='main']",
        "section",
        "div.content",
        "div.page-content",
        "div.entry-content",
        "div.article",
        "div.post",
        "div.pge",
        "div.item",
        "div",
    )
    for selector in selectors:
        for node in soup.select(selector):
            identity = id(node)
            if identity in seen:
                continue
            seen.add(identity)
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            if len(text) < 80:
                continue
            lowered = text.lower()
            link_chars = sum(
                len(re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))) for anchor in node.find_all("a")
            )
            link_density = min(link_chars / max(len(text), 1), 1.0)
            if terms:
                covered = sum(term in lowered for term in terms) / len(terms)
                hits = sum(min(lowered.count(term), 5) for term in terms)
                term_density = hits / max(len(text) / 1_000, 1.0)
            else:
                covered = 1.0
                term_density = 0.0
            classes = " ".join(str(item).lower() for item in node.get("class", ()))
            semantic_bonus = 0.0
            if node.name in {"article", "main"} or node.get("role") == "main":
                semantic_bonus += 2.0
            if any(token in classes for token in ("content", "article", "entry", "post", "pge", "item")):
                semantic_bonus += 1.0
            length_bonus = min(len(text) / 4_000, 1.5)
            score = covered * 10 + min(term_density, 6.0) * 1.5 + semantic_bonus + length_bonus - link_density * 8
            candidates.append((score, -len(text), node))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]
    return soup.body or soup


def _pdf_document_from_pages(
    page_texts: list[str],
    *,
    url: str,
    query: str,
    max_blocks: int,
    max_output_chars: int,
    extractor: str,
    title: str = "",
    warnings: tuple[str, ...] = (),
    original_url: str = "",
) -> ExtractedDocument:
    terms = _query_terms(query)
    blocks: list[ExtractedBlock] = []
    meaningful_chars = 0
    for page_number, raw_text in enumerate(page_texts, start=1):
        text = re.sub(r"[ \t]+", " ", raw_text or "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 20:
            continue
        meaningful_chars += len(text)
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        heading = first_line[:120] or f"Page {page_number}"
        blocks.append(
            ExtractedBlock(
                locator=f"page:{page_number}",
                heading=heading,
                text=text,
                relevance_score=_score_block(heading, text, terms),
            )
        )
    if not blocks:
        raise UnsupportedScannedPdfError(
            "PDF contains no extractable text; scanned PDFs require OCR, which is disabled"
        )
    query_coverage = (
        sum(1 for term in terms if any(term in text.lower() for text in page_texts)) / len(terms) if terms else 1.0
    )
    page_coverage = len(blocks) / max(len(page_texts), 1)
    quality = round(
        min(1.0, 0.45 * min(meaningful_chars / 5_000, 1.0) + 0.3 * page_coverage + 0.25 * query_coverage),
        3,
    )
    return ExtractedDocument(
        url=url,
        original_url=original_url,
        title=title.strip() or url.rsplit("/", 1)[-1] or url,
        format="pdf",
        extractor=extractor,
        blocks=_select_blocks(
            blocks,
            max_blocks=max_blocks,
            max_output_chars=max_output_chars,
        ),
        quality_score=quality,
        warnings=warnings,
    )


_TABLE_QUERY_HINTS = (
    "table",
    "tabular",
    "schedule",
    "matrix",
    "表格",
    "数据表",
    "对照表",
    "条款表",
)
_DOCLING_HARD_TIMEOUT_SECONDS = 60.0
_DOCLING_CIRCUIT_OPEN = False
_DOCLING_RUNTIME_LOCK = threading.Lock()


def _query_requests_table_structure(query: str) -> bool:
    lowered = unicodedata.normalize("NFKC", str(query or "")).casefold()
    return any(hint in lowered for hint in _TABLE_QUERY_HINTS)


def _pypdf_is_sufficient(page_texts: list[str], query: str) -> bool:
    """Return true when pypdf already provides enough relevant, locatable text."""
    meaningful = [text.strip() for text in page_texts if len(text.strip()) >= 80]
    total_chars = sum(len(text) for text in meaningful)
    if total_chars < 2_500 or not meaningful:
        return False
    terms = _query_terms(query)
    if not terms:
        return total_chars >= 5_000 and len(meaningful) >= 2
    corpus = "\n".join(meaningful).casefold()
    hits = sum(term in corpus for term in terms)
    return hits / len(terms) >= 0.45


def _needs_docling(page_texts: list[str], query: str = "") -> bool:
    """Use Docling only for table-specific queries that pypdf cannot satisfy."""

    if not _query_requests_table_structure(query):
        return False
    if _pypdf_is_sufficient(page_texts, query):
        return False

    lines = [line.strip() for text in page_texts for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    short_line_ratio = sum(len(line) < 35 for line in lines) / len(lines)
    table_like_ratio = sum(bool(re.search(r"\S\s{3,}\S|\S\t+\S", line)) for line in lines) / len(lines)
    return len(lines) >= 20 and (short_line_ratio > 0.78 or table_like_ratio > 0.18)


def _build_docling_converter(*, table_structure: bool):
    """Construct one local-only converter inside the isolated worker process."""

    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise RuntimeError("Docling is enabled but the optional 'documents' dependency is not installed") from exc

    options = _docling_pipeline_options(table_structure=table_structure)
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=options,
                backend=PyPdfiumDocumentBackend,
            ),
        }
    )


def _extract_docling_pages_local(payload: bytes, converter: Any) -> list[str]:
    """Convert one PDF using a converter owned by the current process."""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(payload)
            temp_path = handle.name
        result = converter.convert(temp_path)
        page_fragments: dict[int, list[str]] = {}
        iterator = getattr(result.document, "iterate_items", None)
        if callable(iterator):
            for item, _level in iterator():
                text = str(getattr(item, "text", "") or "").strip()
                if not text:
                    continue
                provenance = getattr(item, "prov", ()) or ()
                page_numbers = {
                    int(getattr(prov, "page_no")) for prov in provenance if getattr(prov, "page_no", None) is not None
                }
                for page_number in page_numbers:
                    page_fragments.setdefault(page_number, []).append(text)
        if not page_fragments:
            raise RuntimeError("Docling returned no page-locatable text")
        last_page = max(page_fragments)
        return ["\n\n".join(page_fragments.get(page, ())) for page in range(1, last_page + 1)]
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _docling_worker_main(connection) -> None:
    """Reuse converters across documents; the parent may terminate this process."""
    converters: dict[bool, Any] = {}
    try:
        while True:
            request = connection.recv()
            if request is None:
                return
            payload, table_structure = request
            try:
                key = bool(table_structure)
                converter = converters.get(key)
                if converter is None:
                    converter = _build_docling_converter(table_structure=key)
                    converters[key] = converter
                pages = _extract_docling_pages_local(payload, converter)
                connection.send((True, pages))
            except Exception as exc:
                connection.send((False, f"{type(exc).__name__}: {exc}"))
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class _DoclingProcessRuntime:
    """One reusable Docling process with a killable hard timeout."""

    def __init__(self) -> None:
        self._process = None
        self._connection = None

    def _start(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_docling_worker_main,
            args=(child,),
            name="paperpilot-docling",
            daemon=True,
        )
        process.start()
        child.close()
        self._process = process
        self._connection = parent

    def _stop(self, *, force: bool) -> None:
        process = self._process
        connection = self._connection
        self._process = None
        self._connection = None
        if connection is not None:
            if not force:
                try:
                    connection.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
            connection.close()
        if process is not None:
            if force and process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

    def extract(
        self,
        payload: bytes,
        *,
        table_structure: bool,
        timeout_seconds: float,
    ) -> list[str]:
        if self._process is None or not self._process.is_alive():
            self._stop(force=True)
            self._start()
        assert self._connection is not None
        self._connection.send((payload, bool(table_structure)))
        if not self._connection.poll(timeout_seconds):
            self._stop(force=True)
            raise TimeoutError(
                f"Docling exceeded hard timeout of {timeout_seconds:.0f}s; runtime terminated"
            )
        ok, value = self._connection.recv()
        if not ok:
            raise RuntimeError(str(value))
        return list(value)

    def close(self) -> None:
        self._stop(force=False)


_DOCLING_RUNTIME = _DoclingProcessRuntime()
atexit.register(_DOCLING_RUNTIME.close)


def _extract_docling_pages(
    payload: bytes,
    *,
    table_structure: bool = False,
    timeout_seconds: float = _DOCLING_HARD_TIMEOUT_SECONDS,
) -> list[str]:
    """Use the reusable isolated runtime and open a process-wide breaker on timeout."""
    global _DOCLING_CIRCUIT_OPEN
    if _DOCLING_CIRCUIT_OPEN:
        raise RuntimeError("Docling circuit breaker is open after an earlier timeout")
    with _DOCLING_RUNTIME_LOCK:
        try:
            return _DOCLING_RUNTIME.extract(
                payload,
                table_structure=table_structure,
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError:
            _DOCLING_CIRCUIT_OPEN = True
            raise


def _docling_pipeline_options(*, table_structure: bool = False):
    """Build the resource-bounded Docling standard pipeline configuration."""

    try:
        from docling.datamodel.pipeline_options import PdfPipelineOptions
    except ImportError as exc:
        raise RuntimeError("Docling is enabled but the optional 'documents' dependency is not installed") from exc
    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = bool(table_structure)
    options.enable_remote_services = False
    options.allow_external_plugins = False
    options.document_timeout = 90
    for attribute in (
        "do_picture_classification",
        "do_picture_description",
        "do_chart_extraction",
        "do_code_enrichment",
        "do_formula_enrichment",
        "generate_page_images",
        "generate_picture_images",
    ):
        if hasattr(options, attribute):
            setattr(options, attribute, False)
    return options


def extract_pdf_document(
    payload: bytes,
    *,
    url: str,
    query: str,
    max_blocks: int,
    max_output_chars: int,
    docling_enabled: bool,
    original_url: str = "",
) -> ExtractedDocument:
    """Extract text page by page; selectively use Docling for complex layouts."""

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(payload))
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    metadata_title = ""
    try:
        metadata_title = str(getattr(reader.metadata, "title", "") or "").strip()
    except Exception:
        metadata_title = ""
    if not any(text.strip() for text in page_texts):
        raise UnsupportedScannedPdfError(
            "PDF contains no extractable text; scanned PDFs require OCR, which is disabled"
        )
    extractor = "pypdf"
    warnings: tuple[str, ...] = ()
    table_structure = _query_requests_table_structure(query)
    if docling_enabled and _needs_docling(page_texts, query):
        try:
            docling_pages = _extract_docling_pages(
                payload,
                table_structure=table_structure,
            )
            if sum(len(text) for text in docling_pages) >= sum(len(text) for text in page_texts):
                page_texts = docling_pages
                extractor = "docling"
        except Exception as exc:
            warnings = (f"Docling fallback unavailable: {type(exc).__name__}: {exc}",)
    return _pdf_document_from_pages(
        page_texts,
        url=url,
        original_url=original_url,
        query=query,
        max_blocks=max_blocks,
        max_output_chars=max_output_chars,
        extractor=extractor,
        title=metadata_title,
        warnings=warnings,
    )

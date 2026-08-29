"""Focused W5 tests for bounded, controlled Memory import preparation."""
from __future__ import annotations

import asyncio
import io
import json
import socket
from pathlib import Path
from typing import Any

import pytest
import aiohttp
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import src.research.memory_import as import_module
import src.research.runtime as runtime_module
from src.research.memory import MarkdownMemoryStore
from src.research.memory_import import (
    MAX_EXTRACTED_CHARS,
    MAX_POLICY_CHARS,
    MAX_RAW_BYTES,
    MemoryImportLimitError,
    prepare_memory_file_import,
    prepare_memory_text_import,
    prepare_memory_url_import,
)
from src.research.models import MemoryImportDuplicate, MemoryImportProposal
from src.research.runtime import ResearchRuntime


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
    }


def _pdf(text: str = "Grounded PDF text") -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class _Policy:
    def __init__(self, *, malicious: bool = False) -> None:
        self.calls = 0
        self.tools: list[list[Any]] = []
        self.contexts: list[dict[str, Any]] = []
        self.malicious = malicious

    async def __call__(self, messages, tools):
        self.calls += 1
        self.tools.append(tools)
        marker = "IMPORT_CONTEXT_JSON:\n"
        context = json.loads(messages[-1]["content"].split(marker, 1)[1])
        self.contexts.append(context)
        locator = context["excerpts"][0]["locator"]
        if self.malicious:
            payload = {
                "title": "[unsafe](javascript:alert(1))",
                "summary": "![image](javascript:alert(2))",
                "support": [
                    {
                        "text": "Grounded support",
                        "locators": [locator],
                        "memory_paths": ["Memories/M-other/notes/fake.md"],
                    },
                    {
                        "text": "[[forged]]",
                        "locators": [locator],
                        "memory_paths": [],
                    },
                    {
                        "text": "Forged locator",
                        "locators": ["page:999"],
                        "memory_paths": [],
                    },
                ],
                "conflicts": [
                    {
                        "text": '<img src=x onerror="alert(3)">',
                        "locators": [locator],
                        "memory_paths": [],
                    }
                ],
                "gaps": [],
            }
        else:
            payload = {
                "title": "Imported source",
                "summary": "Bounded source summary.",
                "support": [
                    {
                        "text": "The source supports one point.",
                        "locators": [locator],
                        "memory_paths": [],
                    }
                ],
                "conflicts": [],
                "gaps": [
                    {"text": "One question remains.", "locators": [], "memory_paths": []}
                ],
            }
        return {"content": json.dumps(payload)}


def _store(tmp_path: Path) -> MarkdownMemoryStore:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Imports", "M-imports")
    return store


def test_w5_public_contract_smoke_imports() -> None:
    from src.research import (
        MemoryImportDuplicate as PublicDuplicate,
        MemoryImportLimitError as PublicLimitError,
        MemoryImportProposal as PublicProposal,
        build_attachment_wikilink,
        resolve_memory_attachment_path,
        update_memory_home_with_import,
        validate_memory_attachment_path,
    )

    assert PublicDuplicate is MemoryImportDuplicate
    assert PublicProposal is MemoryImportProposal
    assert PublicLimitError is MemoryImportLimitError
    assert all(
        callable(value)
        for value in (
            build_attachment_wikilink,
            resolve_memory_attachment_path,
            update_memory_home_with_import,
            validate_memory_attachment_path,
        )
    )


@pytest.mark.asyncio
async def test_text_preview_is_zero_write_and_commit_is_content_addressed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    policy = _Policy()
    before = _tree(tmp_path)

    proposal = await prepare_memory_text_import(
        store, policy, "M-imports", "Inline source", "alpha\nbeta"
    )

    assert isinstance(proposal, MemoryImportProposal)
    assert _tree(tmp_path) == before
    assert proposal.source_kind == "text"
    assert proposal.locator == "inline"
    assert proposal.attachment_path.endswith(f"Asset-{proposal.content_hash}.txt")
    assert "lines:1-2" in proposal.import_markdown
    assert proposal.note_source_paths == (proposal.import_path,)
    assert proposal.import_wikilink in proposal.note_markdown
    assert policy.tools == [[]]

    result = store.commit_memory_import(proposal)
    assert result["status"] == "committed"
    assert (tmp_path / proposal.attachment_path).read_bytes() == b"alpha\nbeta"
    assert (tmp_path / proposal.import_path).is_file()
    assert (tmp_path / proposal.note_path).is_file()


@pytest.mark.asyncio
async def test_repeat_import_returns_duplicate_before_extract_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first_policy = _Policy()
    proposal = await prepare_memory_text_import(
        store, first_policy, "M-imports", "Repeat", "same bytes"
    )
    assert isinstance(proposal, MemoryImportProposal)
    store.commit_memory_import(proposal)
    after_commit = _tree(tmp_path)

    def forbidden_extract(*args, **kwargs):
        raise AssertionError("duplicate must not be extracted")

    monkeypatch.setattr(import_module, "_extract", forbidden_extract)
    second_policy = _Policy()
    duplicate = await prepare_memory_text_import(
        store, second_policy, "M-imports", "Repeat", "same bytes"
    )

    assert isinstance(duplicate, MemoryImportDuplicate)
    assert duplicate.import_id == proposal.import_id
    assert duplicate.note_path == proposal.note_path
    assert len(duplicate.wikilinks) == 3
    assert second_policy.calls == 0
    assert _tree(tmp_path) == after_commit


@pytest.mark.asyncio
async def test_file_pdf_and_utf8_text_have_deterministic_locators(tmp_path: Path) -> None:
    pdf_store = _store(tmp_path / "pdf")
    pdf_policy = _Policy()
    pdf = await prepare_memory_file_import(
        pdf_store, pdf_policy, "M-imports", "paper.pdf", _pdf()
    )
    assert isinstance(pdf, MemoryImportProposal)
    assert pdf.media_type == "application/pdf"
    assert "page:1" in pdf.import_markdown
    assert pdf.attachment_path.endswith(".pdf")

    text_store = _store(tmp_path / "text")
    text = await prepare_memory_file_import(
        text_store, _Policy(), "M-imports", "notes.md", "你好\nworld".encode()
    )
    assert isinstance(text, MemoryImportProposal)
    assert text.media_type == "text/plain"
    assert "lines:1-2" in text.import_markdown
    assert text.attachment_path.endswith(".txt")


@pytest.mark.asyncio
async def test_url_html_uses_sections_and_safe_code_span_for_locator(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def fetcher(url: str):
        assert url == "https://example.com/source"
        return {
            "final_url": "https://example.com/final`section",
            "media_type": "text/html; charset=utf-8",
            "content": b"<html><body><script>bad()</script><h1>Title</h1><p>Body</p></body></html>",
            "history": [url],
        }

    proposal = await prepare_memory_url_import(
        store,
        _Policy(),
        "M-imports",
        "https://example.com/source",
        _fetcher=fetcher,
    )

    assert isinstance(proposal, MemoryImportProposal)
    assert proposal.source_kind == "url"
    assert "section:1" in proposal.import_markdown
    assert "section:2" in proposal.import_markdown
    assert "bad()" not in proposal.import_markdown
    assert "`` https://example.com/final`section ``" in proposal.import_markdown


@pytest.mark.asyncio
async def test_html_without_semantic_blocks_falls_back_to_visible_text(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def fetcher(url: str):
        return {
            "final_url": url,
            "media_type": "text/html",
            "content": (
                b"<html><body><script>hidden()</script><main><div>Visible "
                b"<span>article text</span></div></main></body></html>"
            ),
        }

    proposal = await prepare_memory_url_import(
        store,
        _Policy(),
        "M-imports",
        "https://example.com/div-only",
        _fetcher=fetcher,
    )

    assert isinstance(proposal, MemoryImportProposal)
    assert "section:1" in proposal.import_markdown
    assert "Visible article text" in proposal.import_markdown
    assert "hidden()" not in proposal.import_markdown


@pytest.mark.asyncio
async def test_model_markdown_html_and_forged_references_are_rendered_or_filtered(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    proposal = await prepare_memory_text_import(
        store,
        _Policy(malicious=True),
        "M-imports",
        "Adversarial",
        "trusted source",
    )
    assert isinstance(proposal, MemoryImportProposal)
    assert "\\[unsafe\\]\\(javascript:alert\\(1\\)\\)" in proposal.import_markdown
    assert "\\!\\[image\\]\\(javascript:alert\\(2\\)\\)" in proposal.import_markdown
    assert "<img" not in proposal.note_markdown
    assert "[[forged]]" not in proposal.note_markdown
    assert "Forged locator" not in proposal.note_markdown
    assert "M-other" not in proposal.note_markdown
    assert proposal.note_source_paths == (proposal.import_path,)


@pytest.mark.asyncio
async def test_raw_extract_pdf_and_redirect_limits_are_distinct_413_errors(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(MemoryImportLimitError, match="raw limit"):
        await prepare_memory_file_import(
            store, _Policy(), "M-imports", "large.txt", b"x" * (MAX_RAW_BYTES + 1)
        )
    with pytest.raises(MemoryImportLimitError, match="extraction"):
        await prepare_memory_text_import(
            store, _Policy(), "M-imports", "large", "x" * (MAX_EXTRACTED_CHARS + 1)
        )

    writer = PdfWriter()
    for _ in range(201):
        writer.add_blank_page(width=10, height=10)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(MemoryImportLimitError, match="page limit"):
        await prepare_memory_file_import(
            store, _Policy(), "M-imports", "pages.pdf", output.getvalue()
        )

    async def redirects(url: str):
        return {
            "final_url": url,
            "media_type": "text/plain",
            "content": b"ok",
            "history": [url] * 4,
        }

    with pytest.raises(MemoryImportLimitError, match="redirects"):
        await prepare_memory_url_import(
            store, _Policy(), "M-imports", "https://example.com", _fetcher=redirects
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "content", "message"),
    [
        ("bad.txt", b"\xff", "UTF-8"),
        ("bad.txt", b"a\x00b", "NUL"),
        ("bad.pdf", b"not-pdf", "signature"),
        ("archive.zip", b"data", "only PDF"),
    ],
)
async def test_invalid_file_formats_are_rejected(
    tmp_path: Path,
    file_name: str,
    content: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await prepare_memory_file_import(
            _store(tmp_path), _Policy(), "M-imports", file_name, content
        )


@pytest.mark.asyncio
async def test_url_ssrf_and_redirect_targets_are_rejected_before_policy(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    called = False

    async def fetcher(url: str):
        nonlocal called
        called = True
        return (url, "text/plain", b"data")

    with pytest.raises(ValueError, match="non-public"):
        await prepare_memory_url_import(
            store, _Policy(), "M-imports", "http://127.0.0.1/secret", _fetcher=fetcher
        )
    assert called is False

    async def private_redirect(url: str):
        return {
            "final_url": "http://169.254.169.254/latest/meta-data",
            "media_type": "text/plain",
            "content": b"secret",
            "history": [url],
        }

    with pytest.raises(ValueError, match="non-public"):
        await prepare_memory_url_import(
            store,
            _Policy(),
            "M-imports",
            "https://example.com",
            _fetcher=private_redirect,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "https://user:secret@example.com/source",
        "https://example.com:8443/source",
    ),
)
async def test_url_rejects_scheme_credentials_and_non_default_port_before_fetch(
    tmp_path: Path,
    url: str,
) -> None:
    store = _store(tmp_path)
    called = False

    async def fetcher(requested_url: str):
        nonlocal called
        called = True
        return (requested_url, "text/plain", b"data")

    with pytest.raises(ValueError):
        await prepare_memory_url_import(
            store, _Policy(), "M-imports", url, _fetcher=fetcher
        )
    assert called is False


@pytest.mark.asyncio
async def test_dns_mixed_public_private_answer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    async def mixed_getaddrinfo(*args, **kwargs):
        del args, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", mixed_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        await import_module._resolve_public("example.com", 443)


@pytest.mark.asyncio
async def test_pinned_resolver_returns_only_validated_addresses_and_rejects_host_swap() -> None:
    resolver = import_module._PinnedResolver(
        "example.com", ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    )
    resolved = await resolver.resolve("example.com", 443)
    assert [item["host"] for item in resolved] == [
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    ]
    with pytest.raises(OSError, match="unvalidated host"):
        await resolver.resolve("attacker.example", 443)


@pytest.mark.asyncio
async def test_one_total_url_timeout_covers_the_whole_fetch_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = False

    async def stalled_chain(url: str):
        nonlocal cancelled
        assert url == "https://example.com/"
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(import_module, "URL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        import_module,
        "_fetch_public_url_within_budget",
        stalled_chain,
    )
    with pytest.raises(MemoryImportLimitError, match="total timeout"):
        await import_module._fetch_public_url("https://example.com/")
    assert cancelled is True


@pytest.mark.asyncio
async def test_network_client_errors_become_stable_source_value_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_chain(url: str):
        del url
        raise aiohttp.ClientConnectionError(
            "sensitive internal endpoint and TLS details"
        )

    monkeypatch.setattr(
        import_module,
        "_fetch_public_url_within_budget",
        failed_chain,
    )
    with pytest.raises(ValueError) as caught:
        await import_module._fetch_public_url("https://example.com/")
    assert type(caught.value) is ValueError
    assert str(caught.value) == "Memory import URL could not be read"
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_policy_context_is_bounded_to_chars_and_locators(tmp_path: Path) -> None:
    store = _store(tmp_path)
    policy = _Policy()
    text = "\n".join(f"line {index} " + "x" * 60 for index in range(2000))
    proposal = await prepare_memory_text_import(
        store, policy, "M-imports", "Bounded", text
    )
    assert isinstance(proposal, MemoryImportProposal)
    context = policy.contexts[0]
    assert len(context["excerpts"]) <= 64
    assert len(json.dumps(context, ensure_ascii=False)) <= MAX_POLICY_CHARS


@pytest.mark.asyncio
async def test_runtime_import_methods_are_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = object.__new__(ResearchRuntime)
    runtime.memory_store = object()
    runtime.policy = object()
    sentinel = object()
    calls: list[tuple[Any, ...]] = []

    async def fake_file(*args):
        calls.append(args)
        return sentinel

    async def fake_text(*args):
        calls.append(args)
        return sentinel

    async def fake_url(*args):
        calls.append(args)
        return sentinel

    class CommitStore:
        def commit_memory_import(self, proposal):
            calls.append((proposal,))
            return {"status": "committed"}

    monkeypatch.setattr(runtime_module, "prepare_file_import", fake_file)
    monkeypatch.setattr(runtime_module, "prepare_text_import", fake_text)
    monkeypatch.setattr(runtime_module, "prepare_url_import", fake_url)
    runtime.memory_store = CommitStore()

    assert await runtime.prepare_memory_file_import("M-id", "a.txt", b"a") is sentinel
    assert await runtime.prepare_memory_text_import("M-id", "title", "text") is sentinel
    assert await runtime.prepare_memory_url_import("M-id", "https://example.com") is sentinel
    assert runtime.commit_memory_import(sentinel) == {"status": "committed"}
    assert calls[-1] == (sentinel,)

"""Contracts and rollout switches for full-content extraction."""

from __future__ import annotations

import pytest

from src.tools.content_extraction import (
    ContentExtractionConfig,
    ExtractedBlock,
    ExtractedDocument,
    content_extraction_config_from_config,
    validate_content_extraction_dependencies,
)
from src.research.runtime import build_research_tools
from src.tools.browser import BrowserTool


def test_content_extraction_defaults_to_legacy() -> None:
    settings = content_extraction_config_from_config({})

    assert settings == ContentExtractionConfig()
    assert settings.mode == "legacy"
    assert settings.tavily_extract_fallback is False
    assert settings.docling_enabled is False
    assert settings.ocr_enabled is False


def test_structured_content_extraction_settings_are_strictly_parsed() -> None:
    settings = content_extraction_config_from_config(
        {
            "content_extraction": {
                "mode": "structured",
                "tavily_extract_fallback": True,
                "docling_enabled": True,
                "ocr_enabled": False,
                "max_download_bytes": 4_000_000,
                "max_blocks": 12,
                "max_output_chars": 16_000,
            }
        }
    )

    assert settings.mode == "structured"
    assert settings.tavily_extract_fallback is True
    assert settings.docling_enabled is True
    assert settings.max_download_bytes == 4_000_000
    assert settings.max_blocks == 12
    assert settings.max_output_chars == 16_000


@pytest.mark.parametrize("value", [None, True, 1, [], "structured_v2"])
def test_invalid_content_extraction_mode_is_rejected(value) -> None:
    with pytest.raises(ValueError, match="content_extraction.mode"):
        content_extraction_config_from_config({"content_extraction": {"mode": value}})


def test_unknown_content_extraction_setting_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown content_extraction settings"):
        content_extraction_config_from_config({"content_extraction": {"firecrawl_enabled": True}})


def test_ocr_is_explicitly_unsupported() -> None:
    with pytest.raises(ValueError, match="OCR is unsupported"):
        content_extraction_config_from_config({"content_extraction": {"ocr_enabled": True}})


def test_extracted_document_serializes_real_block_locators() -> None:
    document = ExtractedDocument(
        url="https://example.com/guide.pdf",
        title="Guide",
        format="pdf",
        extractor="pypdf",
        blocks=(
            ExtractedBlock(
                locator="page:3",
                heading="Eligibility",
                text="Eligible projects must meet the stated criteria.",
                relevance_score=3.5,
            ),
        ),
        quality_score=0.9,
        warnings=("sample warning",),
    )

    payload = document.to_dict()

    assert payload["status"] == "ok"
    assert payload["blocks"][0]["locator"] == "page:3"
    assert payload["blocks"][0]["heading"] == "Eligibility"
    assert payload["warnings"] == ["sample warning"]


@pytest.mark.parametrize(
    "block",
    [
        ExtractedBlock(locator="", heading="Title", text="body"),
        ExtractedBlock(locator="section:1", heading="Title", text=""),
        ExtractedBlock(locator="section:1", heading="Title", text="body", relevance_score=-1),
    ],
)
def test_extracted_blocks_reject_unlocatable_or_invalid_content(block) -> None:
    with pytest.raises(ValueError):
        block.validate()


def test_runtime_passes_structured_extraction_settings_to_browser() -> None:
    tools = build_research_tools(
        {
            "tools": {"enabled": ["browser"]},
            "content_extraction": {
                "mode": "structured",
                "max_blocks": 7,
            },
        }
    )

    assert len(tools) == 1
    assert isinstance(tools[0], BrowserTool)
    assert tools[0].extraction_config.mode == "structured"
    assert tools[0].extraction_config.max_blocks == 7


def test_structured_dependency_check_fails_before_research_starts(monkeypatch) -> None:
    from src.tools import content_extraction

    original_import = content_extraction.importlib.import_module

    def fake_import(name: str):
        if name == "markdownify":
            error = ModuleNotFoundError("No module named 'markdownify'")
            error.name = "markdownify"
            raise error
        return original_import(name)

    monkeypatch.setattr(content_extraction.importlib, "import_module", fake_import)

    with pytest.raises(RuntimeError, match="missing packages: markdownify"):
        validate_content_extraction_dependencies(
            ContentExtractionConfig(mode="structured")
        )


def test_legacy_dependency_check_does_not_import_structured_packages(monkeypatch) -> None:
    from src.tools import content_extraction

    def forbidden(name: str):
        raise AssertionError(f"legacy mode must not import {name}")

    monkeypatch.setattr(content_extraction.importlib, "import_module", forbidden)

    validate_content_extraction_dependencies(ContentExtractionConfig())

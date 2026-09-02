"""Phase 5 tests for constrained citation repair and stable rendering."""

from __future__ import annotations

import json

import pytest

from src.research.citation_audit import repair_citations
from src.research.models import EvidenceItem
from src.research.rendering import render_evidence_references
from src.research.v2_contracts import CitationIssue


def _evidence():
    return (
        EvidenceItem("evidence-a", "A", "web", "Shared source", "https://example.com/shared", "p.1", "A"),
        EvidenceItem("evidence-b", "B", "web", "Shared source", "https://example.com/shared", "p.2", "B"),
        EvidenceItem("evidence-c", "C", "web", "Other source", "https://example.com/other", "p.3", "C"),
    )


def _issue():
    return CitationIssue.create(
        "The result is universal.", "Findings", ("evidence-a",),
        "overclaim", "high", "qualify",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        "Qualified. [[EVIDENCE:evidence-forged]]",
        "Qualified. https://forged.example/source",
    ],
)
async def test_repair_cannot_add_unknown_evidence_or_url(replacement: str) -> None:
    original = "# Findings\n\nThe result is universal. [[EVIDENCE:evidence-a]]"

    async def policy(messages, *, tools=None):
        return {"content": json.dumps({
            "edits": [{
                "operation": "QUALIFY",
                "target": "The result is universal.",
                "replacement": replacement,
            }],
            "report_markdown": original.replace("The result is universal.", replacement),
        })}

    with pytest.raises(ValueError):
        await repair_citations(policy, original, _evidence(), (_issue(),))


@pytest.mark.asyncio
async def test_repair_replays_declared_qualification_and_removes_overclaim() -> None:
    original = "# Findings\n\nThe result is universal. [[EVIDENCE:evidence-a]]"
    replacement = "In the reported test, the result was observed."
    revised = original.replace("The result is universal.", replacement)

    async def policy(messages, *, tools=None):
        assert tools == []
        return {"content": json.dumps({
            "edits": [{
                "operation": "QUALIFY",
                "target": "The result is universal.",
                "replacement": replacement,
            }],
            "report_markdown": revised,
        })}

    outcome = await repair_citations(policy, original, _evidence(), (_issue(),))

    assert outcome.status == "repaired"
    assert outcome.repaired_markdown == revised
    assert "universal" not in outcome.repaired_markdown


def test_reference_rendering_is_stable_and_deduplicates_same_source_url() -> None:
    markdown = (
        "Claim C [[EVIDENCE:evidence-c]] then A [[EVIDENCE:evidence-a]] "
        "and B [[EVIDENCE:evidence-b]]."
    )
    notes = {
        "evidence-a": "Evidence-a",
        "evidence-b": "Evidence-b",
        "evidence-c": "Evidence-c",
    }

    first = render_evidence_references(markdown, _evidence(), notes)
    second = render_evidence_references(markdown, _evidence(), notes)

    assert first == second
    assert first.count("1. [[evidence/Evidence-c") == 1
    assert first.count("2. [[evidence/Evidence-a") == 1
    assert "Evidence-b" not in first
    assert first.split("## References", 1)[0].count("[2]") == 2
    assert "[[EVIDENCE:" not in first

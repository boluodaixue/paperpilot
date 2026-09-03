"""Bounded quick Web answers and Memory-to-Core Evidence projection."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.conversation import answer_quick_search, memory_hits_to_prior_evidence


class Acquisition:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class Policy:
    def __init__(self, payload):
        self.payload = payload

    def __call__(self, messages, *, tools=None):
        assert tools == []
        assert "WEB_DOCUMENTS_JSON" in messages[-1]["content"]
        return {"content": json.dumps(self.payload), "tool_calls": []}


def _documents():
    return {
        "documents": [
            {
                "title": "Official release",
                "url": "https://example.com/release",
                "blocks": [{
                    "heading": "Release",
                    "locator": "section 1",
                    "text": "The current release adds long-running task support.",
                }],
            },
            {
                "title": "Technical paper",
                "url": "https://example.org/paper",
                "blocks": [{
                    "heading": "Evaluation",
                    "locator": "page 4",
                    "text": "The evaluation measures reliability over long horizons.",
                }],
            },
        ]
    }


@pytest.mark.asyncio
async def test_quick_answer_uses_three_source_cap_and_validated_citations() -> None:
    acquisition = Acquisition(_documents())
    answer = await answer_quick_search(
        acquisition,
        Policy({
            "claims": [{
                "text": "Long-running support is available.",
                "source_ids": ["S1"],
            }],
            "insufficient_evidence": [],
        }),
        "What changed in the current release?",
    )

    assert acquisition.calls == [{
        "query": "What changed in the current release?",
        "top_n": 6,
        "max_sources": 3,
    }]
    assert "[S1](https://example.com/release)" in answer.markdown
    assert [item.source_id for item in answer.citations] == ["S1"]


@pytest.mark.asyncio
async def test_quick_answer_drops_claims_without_known_source_ids() -> None:
    answer = await answer_quick_search(
        Acquisition(_documents()),
        Policy({
            "claims": [{"text": "Unsupported.", "source_ids": ["S99"]}],
            "insufficient_evidence": [],
        }),
        "Current status?",
    )

    assert answer.citations == ()
    assert answer.insufficient_evidence
    assert answer.markdown.startswith("证据不足")


@pytest.mark.asyncio
async def test_quick_answer_returns_evidence_shortage_without_documents() -> None:
    answer = await answer_quick_search(
        Acquisition({"status": "error", "error": "search unavailable"}),
        Policy({}),
        "Current status?",
    )

    assert answer.citations == ()
    assert answer.insufficient_evidence == ("search unavailable",)


def test_memory_hits_become_opaque_prior_evidence_with_external_bindings() -> None:
    projection = memory_hits_to_prior_evidence((
        SimpleNamespace(
            relative_path="Memories/M-secret/notes/Agent.md",
            title="Agent notes",
            summary="Long-horizon tasks need durable state and recovery.",
        ),
    ))

    item = projection.bundle.items[0]
    assert item.source_ref.startswith("prior://")
    assert "M-secret" not in item.source_ref
    assert "Memories/" not in item.source_ref
    assert item.provenance == "selected_memory"
    assert projection.source_bindings == ((
        item.evidence_id,
        "Memories/M-secret/notes/Agent.md",
    ),)

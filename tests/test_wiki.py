from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.research.memory import MarkdownMemoryStore, MemoryWriteConflictError
from src.research.models import (
    EvidenceItem,
    ExecutionIdentity,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
)
from src.research.vault_write_queue import VaultWriteQueue
from src.research.vault_write_service import VaultWriteService
from src.research.vault_writer import VaultWriter
from src.research.wiki import generate_wiki_draft, list_wiki_pages, validate_wiki_draft

MEMORY_ID = "M-wiki"


class WikiPolicy:
    def __call__(self, messages, *, tools=None):
        assert tools == []
        payload = json.loads(str(messages[-1]["content"]).split("\n", 1)[1])
        evidence_id = payload["evidence"][0]["evidence_id"]
        return {
            "content": json.dumps(
                {
                    "title": "Grounded topic",
                    "sections": [
                        {
                            "heading": "Current understanding",
                            "claims": [
                                {
                                    "text": "The finding is supported by the selected research.",
                                    "evidence_ids": [evidence_id],
                                }
                            ],
                        }
                    ],
                }
            ),
            "tool_calls": [],
        }


def _published_report(store: MarkdownMemoryStore) -> str:
    evidence = EvidenceItem(
        evidence_id="E-wiki",
        finding="A grounded finding.",
        source_type="web",
        title="Primary source",
        source_ref="https://example.test/wiki",
        locator="section 1",
        excerpt="The primary source supports the finding.",
        excerpt_type="quote",
    )
    brief = ResearchBrief(
        question="What is known?",
        objective="Build grounded knowledge",
        scope=("topic",),
        directions=("primary sources",),
        constraints=("cite evidence",),
        expected_output="report",
        memory_id=MEMORY_ID,
    )
    result = ResearchResult(
        task_id="wiki-task",
        status=ResearchStatus.COMPLETED,
        summary="A grounded summary.",
        findings=(evidence.finding,),
        evidence=(evidence,),
    )
    _markdown, manifest = store.persist_research(
        brief,
        result,
        ExecutionIdentity("wiki-root", None, "wiki-root", 0),
        memory_id=MEMORY_ID,
    )
    return manifest.report_path


def _service(tmp_path: Path, store: MarkdownMemoryStore) -> VaultWriteService:
    queue = VaultWriteQueue(tmp_path / "runtime.db", vault_scope="wiki-tests", poll_interval_seconds=0.005)
    return VaultWriteService(
        store,
        queue,
        VaultWriter(store.root, queue),
        coordination_interval_seconds=0.005,
        wait_timeout_seconds=2,
        startup_timeout_seconds=2,
    )


@pytest.mark.asyncio
async def test_generate_and_publish_grounded_wiki_page(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("Wiki", MEMORY_ID)
    report_path = _published_report(store)

    draft = await generate_wiki_draft(store, WikiPolicy(), MEMORY_ID, report_path)

    assert draft.action == "create"
    assert draft.evidence_ids == ("E-wiki",)
    assert "[[Memories/M-wiki/evidence/E-wiki|Evidence]]" in draft.markdown
    result = _service(tmp_path, store).commit_wiki_page(draft)
    assert result["target_path"] == draft.target_path
    assert store.read_text(draft.target_path) == draft.markdown
    assert "Grounded topic" in store.read_text("Memories/M-wiki/wiki/Index.md")
    assert list_wiki_pages(store, MEMORY_ID)[0]["title"] == "Grounded topic"


@pytest.mark.asyncio
async def test_update_rejects_page_changed_after_preview(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("Wiki", MEMORY_ID)
    report_path = _published_report(store)
    service = _service(tmp_path, store)
    created = await generate_wiki_draft(store, WikiPolicy(), MEMORY_ID, report_path)
    service.commit_wiki_page(created)
    update = await generate_wiki_draft(
        store,
        WikiPolicy(),
        MEMORY_ID,
        report_path,
        target_path=created.target_path,
    )
    (store.root / created.target_path).write_text(created.markdown + "\nUser edit.\n", encoding="utf-8")

    with pytest.raises(MemoryWriteConflictError, match="changed after"):
        validate_wiki_draft(store, update)


@pytest.mark.asyncio
async def test_save_rejects_client_forged_evidence_identity(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("Wiki", MEMORY_ID)
    report_path = _published_report(store)
    draft = await generate_wiki_draft(store, WikiPolicy(), MEMORY_ID, report_path)
    forged = replace(draft, evidence_ids=("Evidence-invented",))

    with pytest.raises(ValueError, match="evidence_ids|Evidence links"):
        validate_wiki_draft(store, forged)

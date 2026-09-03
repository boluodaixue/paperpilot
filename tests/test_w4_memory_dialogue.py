"""W4 acceptance tests for grounded Memory answers and controlled note proposals."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.research import (
    MarkdownMemoryStore,
    MemoryAnswer,
    MemoryNoteProposal,
    build_research_runtime,
)
from src.research.memory_dialogue import answer_memory, propose_memory_note


def _write_note(
    root: Path,
    memory_id: str,
    *,
    name: str,
    title: str,
    body: str,
) -> str:
    relative_path = f"Memories/{memory_id}/notes/{name}.md"
    (root / relative_path).write_text(
        (
            "---\n"
            f'id: "Note-{name}"\n'
            'type: "note"\n'
            f'memory_id: "{memory_id}"\n'
            f'title: "{title}"\n'
            'created_at: "2026-08-28T00:00:00+08:00"\n'
            'updated_at: "2026-08-28T00:00:00+08:00"\n'
            'origin: "user"\n'
            'status: "confirmed"\n'
            "tags:\n  - research\n"
            "---\n\n"
            f"# {title}\n\n{body}\n"
        ),
        encoding="utf-8",
    )
    return relative_path


def _vault_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _Policy:
    def __init__(self, *, invalid_proposal: bool = False) -> None:
        self.calls = 0
        self.tools: list[list[dict[str, Any]]] = []
        self.invalid_proposal = invalid_proposal

    def __call__(self, messages, *, tools=None):
        self.calls += 1
        self.tools.append(list(tools or []))
        system = str(messages[0].get("content", ""))
        if "Answer only from the supplied selected-Memory notes" in system:
            context = json.loads(
                str(messages[-1]["content"]).split("MEMORY_CONTEXT_JSON:\n", 1)[1]
            )
            path = context["hits"][0]["path"]
            return {
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The selected note supports the grounded claim.",
                                "source_paths": [
                                    path,
                                    "Memories/M-other/notes/Forged.md",
                                ],
                            },
                            {
                                "text": "This claim cites only a forged path.",
                                "source_paths": ["Memories/M-other/notes/Forged.md"],
                            },
                            {
                                "text": "The model tried [[Memories/M-dialogue/notes/Forged]].",
                                "source_paths": [path],
                            },
                        ],
                        "insufficient_evidence": ["A second question remains open."],
                    }
                )
            }

        contract_text = str(messages[-1]["content"]).split(
            "FIXED_NOTE_CONTRACT_JSON:\n", 1
        )[1].split("\n\nMEMORY_ANSWER:", 1)[0]
        contract = json.loads(contract_text)
        fixed = contract["frontmatter"]
        if self.invalid_proposal:
            return {"content": json.dumps({"markdown": "not a complete note"})}
        title = "Grounded Memory Answer"
        source_lines = "\n".join(
            f"- [[{path[:-3]}]]" for path in contract["allowed_source_paths"]
        ) or "- None"
        markdown = (
            "---\n"
            f'id: {json.dumps(fixed["id"])}\n'
            f'type: {json.dumps(fixed["type"])}\n'
            f'memory_id: {json.dumps(fixed["memory_id"])}\n'
            f'title: {json.dumps(title)}\n'
            f'created_at: {json.dumps(fixed["created_at"])}\n'
            f'updated_at: {json.dumps(fixed["updated_at"])}\n'
            f'origin: {json.dumps(fixed["origin"])}\n'
            f'status: {json.dumps(fixed["status"])}\n'
            "tags:\n  - paperpilot\n"
            "---\n\n"
            f"# {title}\n\n"
            "The selected note supports the grounded claim.\n\n"
            f"## Sources\n\n{source_lines}\n"
        )
        return {"content": json.dumps({"markdown": markdown})}


def _prepared_store(tmp_path: Path) -> tuple[MarkdownMemoryStore, str]:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Dialogue", memory_id="M-dialogue")
    store.create_memory("Other", memory_id="M-other")
    path = _write_note(
        tmp_path,
        "M-dialogue",
        name="Grounded-source",
        title="Grounded source",
        body="The selected note supports a grounded memory claim.",
    )
    _write_note(
        tmp_path,
        "M-other",
        name="Private-source",
        title="Private source",
        body="This other Memory must not support the answer.",
    )
    return store, path


@pytest.mark.asyncio
async def test_answer_filters_forged_paths_and_model_wikilinks_without_writing(
    tmp_path: Path,
) -> None:
    store, source_path = _prepared_store(tmp_path)
    policy = _Policy()
    before = _vault_files(tmp_path)

    answer = await answer_memory(
        store,
        policy,
        "M-dialogue",
        "What supports the grounded memory claim?",
    )

    assert isinstance(answer, MemoryAnswer)
    assert answer.answer_id.startswith("Answer-")
    assert answer.memory_id == "M-dialogue"
    assert answer.question == "What supports the grounded memory claim?"
    assert len(answer.citations) == 1
    assert answer.citations[0].relative_path == source_path
    assert answer.citations[0].wikilink == f"[[{source_path[:-3]}]]"
    assert f"[[{source_path[:-3]}]]" in answer.markdown
    assert "M-other" not in answer.markdown
    assert "model tried" not in answer.markdown
    assert "## Insufficient evidence" in answer.markdown
    assert "A second question remains open." in answer.markdown
    assert any("valid citation" in reason for reason in answer.insufficient_evidence)
    assert any("WikiLink syntax" in reason for reason in answer.insufficient_evidence)
    assert policy.tools == [[]]
    assert _vault_files(tmp_path) == before


@pytest.mark.asyncio
async def test_no_hits_returns_insufficient_without_policy_or_writes(
    tmp_path: Path,
) -> None:
    store, _ = _prepared_store(tmp_path)
    policy = _Policy()
    before = _vault_files(tmp_path)

    answer = await answer_memory(
        store,
        policy,
        "M-dialogue",
        "Zygomorphic spectroheliograph",
    )

    assert answer.citations == ()
    assert answer.insufficient_evidence == (
        "当前 Memory 中没有找到与问题相关的内容。",
    )
    assert "证据不足" in answer.markdown
    assert policy.calls == 0
    assert _vault_files(tmp_path) == before


@pytest.mark.asyncio
async def test_complete_proposal_is_validated_but_not_written_until_commit(
    tmp_path: Path,
) -> None:
    store, source_path = _prepared_store(tmp_path)
    policy = _Policy()
    answer = await answer_memory(
        store,
        policy,
        "M-dialogue",
        "What supports the grounded memory claim?",
    )
    before = _vault_files(tmp_path)

    proposal = await propose_memory_note(store, policy, answer)

    assert isinstance(proposal, MemoryNoteProposal)
    assert proposal.proposal_id.startswith("Proposal-")
    assert proposal.note_id.startswith("Note-")
    assert proposal.target_path == (
        f"Memories/M-dialogue/notes/{proposal.note_id}.md"
    )
    assert proposal.wikilink == f"[[{proposal.target_path[:-3]}]]"
    assert proposal.title == "Grounded Memory Answer"
    assert proposal.source_paths == (source_path,)
    assert proposal.home_path == "Memories/M-dialogue/Home.md"
    assert len(proposal.home_content_hash) == 64
    assert proposal.target_content_hash is None
    assert f'id: "{proposal.note_id}"' in proposal.markdown
    assert 'memory_id: "M-dialogue"' in proposal.markdown
    assert f"[[{source_path[:-3]}]]" in proposal.markdown
    assert proposal.wikilink in proposal.home_markdown
    assert _vault_files(tmp_path) == before
    assert policy.tools == [[], []]

    committed = store.commit_memory_note(proposal)
    assert committed == {
        "memory_id": "M-dialogue",
        "target_path": proposal.target_path,
        "home_path": proposal.home_path,
        "wikilink": proposal.wikilink,
    }
    assert (tmp_path / proposal.target_path).read_text(encoding="utf-8") == proposal.markdown
    assert (tmp_path / proposal.home_path).read_text(encoding="utf-8") == proposal.home_markdown


@pytest.mark.asyncio
async def test_invalid_policy_proposal_fails_with_zero_writes(tmp_path: Path) -> None:
    store, _ = _prepared_store(tmp_path)
    answer_policy = _Policy()
    answer = await answer_memory(
        store,
        answer_policy,
        "M-dialogue",
        "What supports the grounded memory claim?",
    )
    proposal_policy = _Policy(invalid_proposal=True)
    before = _vault_files(tmp_path)

    with pytest.raises(ValueError, match="frontmatter"):
        await propose_memory_note(store, proposal_policy, answer)

    assert proposal_policy.tools == [[]]
    assert _vault_files(tmp_path) == before


@pytest.mark.asyncio
async def test_runtime_exposes_answer_propose_and_commit_thin_entries(
    tmp_path: Path,
) -> None:
    store, _ = _prepared_store(tmp_path)
    policy = _Policy()
    runtime = build_research_runtime(
        {},
        policy=policy,
        tools=[],
        memory_store=store,
    )

    answer = await runtime.answer_memory(
        "M-dialogue",
        "What supports the grounded memory claim?",
    )
    proposal = await runtime.propose_memory_note(answer)
    committed = runtime.commit_memory_note(proposal)

    assert committed["target_path"] == proposal.target_path
    assert (tmp_path / proposal.target_path).is_file()

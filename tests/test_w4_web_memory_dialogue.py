"""W4 Web acceptance tests for Memory Q&A and confirmed note writes."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import web.server as server
from src.research.memory import MarkdownMemoryStore, MemoryWriteConflictError
from src.research.models import MemoryAnswer, MemoryCitation, MemoryNoteProposal


class DialogueRuntime:
    def __init__(self, root: Path) -> None:
        self.memory_store = MarkdownMemoryStore(root)
        self.answer_calls = 0
        self.proposal_calls = 0
        self.commit_calls = 0
        self.research_calls = 0
        self.force_conflict = False

    def create_memory(self, title: str, *, memory_id: str | None = None):
        return self.memory_store.create_memory(title, memory_id)

    def list_memories(self):
        return self.memory_store.list_memories()

    def get_memory(self, memory_id: str):
        return self.memory_store.get_memory(memory_id)

    async def answer_memory(self, memory_id: str, question: str) -> MemoryAnswer:
        self.answer_calls += 1
        home_path = f"Memories/{memory_id}/Home.md"
        return MemoryAnswer(
            answer_id=f"Answer-{self.answer_calls}",
            memory_id=memory_id,
            question=question,
            markdown=f"Grounded answer for {question} [[{home_path[:-3]}]]",
            citations=(MemoryCitation(
                relative_path=home_path,
                title="Dialogue Home",
                wikilink=f"[[{home_path[:-3]}]]",
            ),),
            insufficient_evidence=(),
        )

    async def propose_memory_note(self, answer: MemoryAnswer) -> MemoryNoteProposal:
        self.proposal_calls += 1
        suffix = str(self.proposal_calls)
        target_path = f"Memories/{answer.memory_id}/notes/Note-{suffix}.md"
        home_path = f"Memories/{answer.memory_id}/Home.md"
        home = self.memory_store.read_text(home_path)
        wikilink = f"[[{target_path[:-3]}]]"
        markdown = (
            "---\n"
            f'id: "Note-{suffix}"\n'
            'type: "note"\n'
            f'memory_id: "{answer.memory_id}"\n'
            'title: "Saved answer"\n'
            "---\n"
            "# Saved answer\n\n"
            f"{answer.markdown}\n"
        )
        return MemoryNoteProposal(
            proposal_id=f"Proposal-{suffix}",
            answer_id=answer.answer_id,
            memory_id=answer.memory_id,
            note_id=f"Note-{suffix}",
            title="Saved answer",
            target_path=target_path,
            markdown=markdown,
            wikilink=wikilink,
            source_paths=tuple(item.relative_path for item in answer.citations),
            home_path=home_path,
            home_content_hash=hashlib.sha256(home.encode("utf-8")).hexdigest(),
            target_content_hash=None,
            home_markdown=f"{home.rstrip()}\n\n- {wikilink}\n",
        )

    def commit_memory_note(self, proposal: MemoryNoteProposal) -> dict[str, str]:
        self.commit_calls += 1
        home = self.memory_store.read_text(proposal.home_path)
        current_hash = hashlib.sha256(home.encode("utf-8")).hexdigest()
        target = self.memory_store.root / proposal.target_path
        if self.force_conflict or current_hash != proposal.home_content_hash or target.exists():
            raise MemoryWriteConflictError("Memory changed after proposal")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal.markdown, encoding="utf-8", newline="\n")
        (self.memory_store.root / proposal.home_path).write_text(
            proposal.home_markdown,
            encoding="utf-8",
            newline="\n",
        )
        return {
            "memory_id": proposal.memory_id,
            "target_path": proposal.target_path,
            "home_path": proposal.home_path,
            "wikilink": proposal.wikilink,
        }

    async def close(self, *, shutdown: bool = False) -> None:
        return None


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    runtime = DialogueRuntime(tmp_path / "Vault")
    runtime.create_memory("Dialogue", memory_id="M-dialogue")
    runtime.create_memory("Other", memory_id="M-other")
    monkeypatch.setattr(server, "CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(server, "_config", {"research": {}})
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = runtime
    server._TASKS.clear()
    server._MEMORY_ANSWERS.clear()
    server._MEMORY_NOTE_PROPOSALS.clear()
    with TestClient(server.app) as client:
        yield client, runtime
    server._TASKS.clear()
    server._MEMORY_ANSWERS.clear()
    server._MEMORY_NOTE_PROPOSALS.clear()
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = None


def _answer(client: TestClient) -> dict:
    response = client.post(
        "/api/memories/M-dialogue/answers",
        json={"question": "What is already known?"},
    )
    assert response.status_code == 200
    return response.json()


def _proposal(client: TestClient, answer_id: str) -> dict:
    response = client.post(
        "/api/memories/M-dialogue/note-proposals",
        json={"answer_id": answer_id},
    )
    assert response.status_code == 200
    return response.json()


def test_answer_is_read_only_cited_and_does_not_start_research_or_write_chat(web_client):
    client, runtime = web_client
    files_before = sorted(path.relative_to(runtime.memory_store.root) for path in runtime.memory_store.root.rglob("*"))
    home_before = runtime.memory_store.read_text("Memories/M-dialogue/Home.md")

    answer = _answer(client)

    assert answer["memory_id"] == "M-dialogue"
    assert answer["question"] == "What is already known?"
    assert answer["citations"][0]["relative_path"] == "Memories/M-dialogue/Home.md"
    uri_query = parse_qs(urlsplit(answer["citations"][0]["obsidian_uri"]).query)
    assert uri_query["path"][0].endswith("/Memories/M-dialogue/Home.md")
    assert runtime.answer_calls == 1
    assert runtime.research_calls == 0
    assert server._TASKS == {}
    assert server.get_chat_store().get_messages("any-session") == []
    assert runtime.memory_store.read_text("Memories/M-dialogue/Home.md") == home_before
    files_after = sorted(
        path.relative_to(runtime.memory_store.root)
        for path in runtime.memory_store.root.rglob("*")
    )
    assert files_after == files_before


def test_proposal_is_transient_until_confirm_then_writes_and_is_consumed(web_client):
    client, runtime = web_client
    answer = _answer(client)
    home_before = runtime.memory_store.read_text("Memories/M-dialogue/Home.md")
    files_before = set(runtime.memory_store.root.rglob("*.md"))

    proposal = _proposal(client, answer["answer_id"])

    target = runtime.memory_store.root / proposal["target_path"]
    assert proposal["markdown"].startswith("---\n")
    assert target.exists() is False
    assert runtime.memory_store.read_text(proposal["home_path"]) == home_before
    assert set(runtime.memory_store.root.rglob("*.md")) == files_before
    assert runtime.commit_calls == 0

    confirmed = client.post(
        f"/api/memory-note-proposals/{proposal['proposal_id']}/confirm"
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {
        "memory_id": proposal["memory_id"],
        "target_path": proposal["target_path"],
        "home_path": proposal["home_path"],
        "wikilink": proposal["wikilink"],
    }
    assert target.read_text(encoding="utf-8") == proposal["markdown"]
    assert proposal["wikilink"] in runtime.memory_store.read_text(proposal["home_path"])
    assert proposal["proposal_id"] not in server._MEMORY_NOTE_PROPOSALS
    assert client.post(
        f"/api/memory-note-proposals/{proposal['proposal_id']}/confirm"
    ).status_code == 404


def test_memory_answer_proposal_matching_and_commit_conflict_are_enforced(web_client):
    client, runtime = web_client
    answer = _answer(client)
    mismatch = client.post(
        "/api/memories/M-other/note-proposals",
        json={"answer_id": answer["answer_id"]},
    )
    assert mismatch.status_code == 409
    assert client.post(
        "/api/memories/M-dialogue/note-proposals",
        json={"answer_id": "Answer-missing"},
    ).status_code == 404

    proposal = _proposal(client, answer["answer_id"])
    runtime.force_conflict = True
    conflict = client.post(
        f"/api/memory-note-proposals/{proposal['proposal_id']}/confirm"
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Memory changed after proposal"
    assert proposal["proposal_id"] in server._MEMORY_NOTE_PROPOSALS
    assert not (runtime.memory_store.root / proposal["target_path"]).exists()


def test_memory_dialogue_rejects_empty_or_unknown_inputs(web_client):
    client, _runtime = web_client
    assert client.post(
        "/api/memories/M-dialogue/answers",
        json={"question": "   "},
    ).status_code == 400
    assert client.post(
        "/api/memories/M-missing/answers",
        json={"question": "Question"},
    ).status_code == 404
    assert client.post(
        "/api/memory-note-proposals/Proposal-missing/confirm"
    ).status_code == 404


def test_static_page_requires_explicit_mode_preview_and_confirmation():
    source = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    for required in (
        'value="research">基于此 Memory 研究',
        'value="memory-answer">Memory 问答',
        "sendMemoryQuestion()",
        "/answers`,",
        "/note-proposals`,",
        "/confirm`,",
        "link.href = citation.obsidian_uri",
        "body.innerHTML = renderSafeMarkdown(answer.markdown",
        "const allowedTags = new Set([",
        "isSafeMarkdownLink(attribute.value)",
        "answer.insufficient_evidence.length > 0",
        "保存回答为笔记",
        "Markdown 完整预览",
        "$('noteProposalMarkdown').textContent = proposal.markdown",
        "cancelMemoryNoteProposal",
        "confirmMemoryNoteProposal",
    ):
        assert required in source
    for forbidden in (
        "/api/import",
        "/api/memories/tree",
        "contenteditable",
        "home_markdown =",
        "autoSave",
        "window.open(",
        "body.innerHTML = marked.parse(answer.markdown",
    ):
        assert forbidden not in source


def test_web_inline_javascript_remains_valid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    page = Path(server.STATIC_DIR) / "index.html"
    checker = (
        "const fs=require('fs');"
        "const html=fs.readFileSync(process.argv[1],'utf8');"
        "const match=html.match(/<script>([\\s\\S]*)<\\/script>/);"
        "if(!match)throw new Error('inline script missing');"
        "new Function(match[1]);"
    )
    result = subprocess.run(
        [node, "-e", checker, str(page)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

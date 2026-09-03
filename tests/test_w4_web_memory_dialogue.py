"""W4 Web acceptance tests for Memory Q&A and confirmed note writes."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import web.server as server
from tests._checkpoint_web_runtime import (
    CheckpointWebPolicy,
    build_checkpointed_web_runtime,
    checkpoint_values,
)


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    policy = CheckpointWebPolicy()
    runtime = build_checkpointed_web_runtime(tmp_path / "Vault", policy=policy)
    runtime.create_memory("Dialogue", memory_id="M-dialogue")
    runtime.create_memory("Other", memory_id="M-other")
    (runtime.memory_store.root / "Memories/M-dialogue/notes/N-known.md").write_text(
        "# Already known\n\nWhat is already known is grounded here.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(server, "_config", {"research": {}})
    server.get_chat_store._store = None
    server.get_runtime_registry._registry = None
    server.get_research_runtime._runtime = runtime
    with TestClient(server.app) as client:
        yield client, runtime, policy
    server.get_chat_store._store = None
    server.get_runtime_registry._registry = None
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
    client, runtime, policy = web_client
    files_before = sorted(path.relative_to(runtime.memory_store.root) for path in runtime.memory_store.root.rglob("*"))
    home_before = runtime.memory_store.read_text("Memories/M-dialogue/Home.md")

    answer = _answer(client)

    assert answer["memory_id"] == "M-dialogue"
    assert answer["question"] == "What is already known?"
    assert answer["citations"][0]["relative_path"] == "Memories/M-dialogue/notes/N-known.md"
    uri_query = parse_qs(urlsplit(answer["citations"][0]["obsidian_uri"]).query)
    assert uri_query["path"][0].endswith("/Memories/M-dialogue/notes/N-known.md")
    assert policy.answer_calls == 1
    assert server.get_chat_store().get_messages("any-session") == []
    assert runtime.memory_store.read_text("Memories/M-dialogue/Home.md") == home_before
    files_after = sorted(
        path.relative_to(runtime.memory_store.root)
        for path in runtime.memory_store.root.rglob("*")
    )
    assert files_after == files_before


def test_answer_can_finish_without_creating_a_note(web_client):
    client, runtime, _policy = web_client
    answer = _answer(client)

    dismissed = client.delete(
        f"/api/memory-answers/{answer['answer_id']}",
        params={
            "session_id": answer["session_id"],
            "memory_id": answer["memory_id"],
        },
    )

    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "cancelled"
    state = asyncio.run(
        checkpoint_values(runtime, "memory_note", answer["workflow_id"])
    )
    assert state["workflow_status"] == "cancelled"
    assert client.delete(
        f"/api/memory-answers/{answer['answer_id']}"
    ).status_code == 409


def test_proposal_is_transient_until_confirm_then_writes_and_is_consumed(web_client):
    client, runtime, _policy = web_client
    answer = _answer(client)
    home_before = runtime.memory_store.read_text("Memories/M-dialogue/Home.md")
    files_before = set(runtime.memory_store.root.rglob("*.md"))

    proposal = _proposal(client, answer["answer_id"])

    target = runtime.memory_store.root / proposal["target_path"]
    assert proposal["markdown"].startswith("---\n")
    assert target.exists() is False
    assert runtime.memory_store.read_text(proposal["home_path"]) == home_before
    assert set(runtime.memory_store.root.rglob("*.md")) == files_before
    state = asyncio.run(
        checkpoint_values(runtime, "memory_note", proposal["workflow_id"])
    )
    assert state["workflow_status"] == "waiting_confirmation"

    confirmed = client.post(
        f"/api/memory-note-proposals/{proposal['proposal_id']}/confirm"
    )
    assert confirmed.status_code == 200
    confirmed_payload = confirmed.json()
    assert confirmed_payload["memory_id"] == proposal["memory_id"]
    assert confirmed_payload["target_path"] == proposal["target_path"]
    assert confirmed_payload["home_path"] == proposal["home_path"]
    assert confirmed_payload["wikilink"] == proposal["wikilink"]
    assert confirmed_payload["workflow_id"] == proposal["workflow_id"]
    assert target.read_text(encoding="utf-8") == proposal["markdown"]
    assert proposal["wikilink"] in runtime.memory_store.read_text(proposal["home_path"])
    state = asyncio.run(
        checkpoint_values(runtime, "memory_note", proposal["workflow_id"])
    )
    assert state["workflow_status"] == "committed"
    assert client.post(
        f"/api/memory-note-proposals/{proposal['proposal_id']}/confirm"
    ).status_code == 409


def test_memory_answer_proposal_matching_and_commit_conflict_are_enforced(web_client):
    client, runtime, _policy = web_client
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
    home = runtime.memory_store.root / proposal["home_path"]
    home.write_text(
        home.read_text(encoding="utf-8") + "\nExternally changed.\n",
        encoding="utf-8",
    )
    conflict = client.post(
        f"/api/memory-note-proposals/{proposal['proposal_id']}/confirm"
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Memory 操作已经失败"
    state = asyncio.run(
        checkpoint_values(runtime, "memory_note", proposal["workflow_id"])
    )
    assert state["workflow_status"] == "failed"
    assert "Memory Home.md changed after the proposal" in state["result"]["error"]
    assert not (runtime.memory_store.root / proposal["target_path"]).exists()


def test_memory_dialogue_rejects_empty_or_unknown_inputs(web_client):
    client, _runtime, _policy = web_client
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
        'value="auto">自动判断',
        'value="research">深度研究',
        'value="memory-answer">只查当前 Memory',
        'value="quick-search">联网查一下',
        "sendQuickQuestion(decision.query || text, false)",
        "sendResearchRequest(decision.query || text, false)",
        "/api/conversation/route",
        "/api/conversation/quick-answer",
        "renderQuickAnswer(answer)",
        "升级为深度研究",
        "sendMemoryQuestion()",
        "/answers`,",
        "/note-proposals`,",
        "/confirm`,",
        "link.href = citation.obsidian_uri",
        "body.innerHTML = renderSafeMarkdown(answer.markdown",
        "card.querySelector('.rc-report').innerHTML = renderSafeMarkdown(",
        "renderSafeMarkdown(stripFrontmatter(ev.markdown))",
        "stripFrontmatter(data.report_md",
        "stripFrontmatter(ev.markdown)",
        'role="tablist"',
        'role="tab"',
        "const allowedTags = new Set([",
        "isSafeMarkdownLink(attribute.value)",
        "answer.insufficient_evidence.length > 0",
        "保存回答为笔记",
        "完成，不保存",
        "基于缺口继续研究",
        "continueFromMemoryAnswer",
        "没有可保存的证据回答",
        "/api/memory-answers/",
        "report-status",
        "termination_reason",
        "@media (max-width: 760px)",
        "Markdown 完整预览",
        "$('noteProposalMarkdown').textContent = proposal.markdown",
        "cancelMemoryNoteProposal",
        "confirmMemoryNoteProposal",
    ):
        assert required in source
    automatic = source[
        source.index("async function sendAutomaticMessage"):
        source.index("async function sendResearchRequest")
    ]
    assert automatic.index("appendBubble('user', text)") < automatic.index(
        "fetch('/api/conversation/route'"
    )
    assert "sendMemoryQuestion(text, false)" in automatic
    assert "sendResearchRequest(decision.query || text, false)" in automatic
    assert "sendQuickQuestion(decision.query || text, false)" in automatic
    for forbidden in (
        "/api/import",
        "/api/memories/tree",
        "contenteditable",
        "home_markdown =",
        "autoSave",
        "window.open(",
        "body.innerHTML = marked.parse(answer.markdown",
        "marked.parse(data.report_md",
        "marked.parse(ev.markdown",
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

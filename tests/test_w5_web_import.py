"""W5 Web acceptance tests for explicit, confirmed Memory imports."""
from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from pathlib import Path

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
    runtime.create_memory("Import", memory_id="M-import")
    runtime.create_memory("Other", memory_id="M-other")
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


def _file_proposal(client: TestClient, content: bytes = b"source text") -> dict:
    response = client.post(
        "/api/memories/M-import/import-proposals",
        json={
            "kind": "file",
            "file_name": "source.txt",
            "media_type": "text/plain",
            "size_bytes": len(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_three_import_dtos_are_explicit_and_preview_is_zero_write(web_client):
    client, runtime, policy = web_client
    home_before = runtime.memory_store.read_text("Memories/M-import/Home.md")
    files_before = sorted(runtime.memory_store.root.rglob("*"))

    file_payload = _file_proposal(client, b"private attachment bytes")
    text_response = client.post(
        "/api/memories/M-import/import-proposals",
        json={"kind": "text", "title": "Pasted", "text": "Pasted body"},
    )
    original_url = " https://example.com/article "
    url_response = client.post(
        "/api/memories/M-import/import-proposals",
        json={"kind": "url", "url": original_url},
    )

    assert text_response.status_code == 200
    assert url_response.status_code == 200
    records = server.get_runtime_registry().list()
    sources = [
        asyncio.run(
            checkpoint_values(runtime, "memory_import", record.thread_id)
        )["source"]
        for record in records
    ]
    assert sources[:3] == [
        {"kind": "file", "file_name": "source.txt", "content": b"private attachment bytes"},
        {"kind": "text", "title": "Pasted", "text": "Pasted body"},
        {"kind": "url", "url": original_url},
    ]
    assert file_payload["status"] == "proposed"
    assert file_payload["can_confirm"] is True
    assert file_payload["import_markdown"].startswith("---\n")
    assert file_payload["note_markdown"].startswith("---\n")
    assert file_payload["import_wikilink"].startswith("[[")
    assert file_payload["attachment_obsidian_uri"].startswith("obsidian://open?")
    assert file_payload["import_obsidian_uri"].startswith("obsidian://open?")
    assert "attachment_bytes" not in file_payload
    assert "content_base64" not in file_payload
    assert base64.b64encode(b"private attachment bytes").decode("ascii") not in client.post(
        "/api/memories/M-import/import-proposals",
        json={"kind": "text", "title": "More", "text": "More text"},
    ).text
    assert policy.import_calls == 4
    assert server.get_chat_store().get_messages("any-session") == []
    assert runtime.memory_store.read_text("Memories/M-import/Home.md") == home_before
    assert sorted(runtime.memory_store.root.rglob("*")) == files_before


def test_file_payload_rejects_invalid_base64_sizes_and_mixed_kinds(web_client):
    client, _runtime, policy = web_client
    endpoint = "/api/memories/M-import/import-proposals"
    assert client.post(endpoint, json={
        "kind": "file", "file_name": "source.txt", "size_bytes": 3,
        "content_base64": "%%%",
    }).status_code == 400
    assert client.post(endpoint, json={
        "kind": "file", "file_name": "source.txt", "size_bytes": 2,
        "content_base64": base64.b64encode(b"abc").decode("ascii"),
    }).status_code == 400
    assert client.post(endpoint, json={
        "kind": "file", "file_name": "source.txt",
        "size_bytes": 10 * 1024 * 1024 + 1, "content_base64": "YQ==",
    }).status_code == 413
    assert client.post(endpoint, json={
        "kind": "url", "url": "https://example.com", "text": "mixed",
    }).status_code == 400
    assert client.post(endpoint, json={
        "kind": "text", "title": " ", "text": "body",
    }).status_code == 400
    assert client.post(
        "/api/memories/M-missing/import-proposals",
        json={"kind": "url", "url": "https://example.com"},
    ).status_code == 404
    assert policy.import_calls == 0


def test_duplicate_is_display_only_and_never_stored(web_client):
    client, runtime, _policy = web_client
    first = client.post(
        "/api/memories/M-import/import-proposals",
        json={"kind": "text", "title": "Existing", "text": "same"},
    ).json()
    assert client.post(
        f"/api/memories/M-import/import-proposals/{first['proposal_id']}/confirm"
    ).status_code == 200
    files_before = {
        path.relative_to(runtime.memory_store.root).as_posix(): path.read_bytes()
        for path in runtime.memory_store.root.rglob("*")
        if path.is_file()
    }
    response = client.post(
        "/api/memories/M-import/import-proposals",
        json={"kind": "text", "title": "Existing", "text": "same"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "duplicate"
    assert payload["can_confirm"] is False
    assert payload["import_path"] == first["import_path"]
    assert payload["import_wikilink"] == first["import_wikilink"]
    assert payload["import_obsidian_uri"].startswith("obsidian://open?")
    assert payload["note_obsidian_uri"].startswith("obsidian://open?")
    assert "attachment_bytes" not in payload
    state = asyncio.run(
        checkpoint_values(runtime, "memory_import", payload["workflow_id"])
    )
    assert state["workflow_status"] == "duplicate"
    assert "proposal" not in state
    assert {
        path.relative_to(runtime.memory_store.root).as_posix(): path.read_bytes()
        for path in runtime.memory_store.root.rglob("*")
        if path.is_file()
    } == files_before


def test_confirm_commits_once_returns_targets_and_consumes_proposal(web_client):
    client, runtime, _policy = web_client
    proposal = _file_proposal(client)
    proposal_id = proposal["proposal_id"]
    response = client.post(
        f"/api/memories/M-import/import-proposals/{proposal_id}/confirm"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "committed"
    assert payload["memory_id"] == "M-import"
    assert payload["attachment_path"] == proposal["attachment_path"]
    assert payload["import_path"] == proposal["import_path"]
    assert payload["note_path"] == proposal["note_path"]
    assert payload["home_path"] == proposal["home_path"]
    assert payload["attachment_obsidian_uri"].startswith("obsidian://open?")
    assert payload["import_obsidian_uri"].startswith("obsidian://open?")
    assert (runtime.memory_store.root / proposal["attachment_path"]).read_bytes() == b"source text"
    state = asyncio.run(
        checkpoint_values(runtime, "memory_import", proposal["workflow_id"])
    )
    assert state["workflow_status"] == "committed"
    assert client.post(
        f"/api/memories/M-import/import-proposals/{proposal_id}/confirm"
    ).status_code == 409


def test_confirm_cancel_strict_ids_conflict_and_concurrent_duplicate(web_client):
    client, runtime, _policy = web_client
    proposal = _file_proposal(client)
    proposal_id = proposal["proposal_id"]
    base = f"/api/memories/M-import/import-proposals/{proposal_id}"

    mismatch = client.post(
        f"/api/memories/M-other/import-proposals/{proposal_id}/confirm"
    )
    assert mismatch.status_code == 409

    home = runtime.memory_store.root / proposal["home_path"]
    home.write_text(
        home.read_text(encoding="utf-8") + "\nExternal change.\n",
        encoding="utf-8",
    )
    conflict = client.post(f"{base}/confirm")
    assert conflict.status_code == 409
    failed = asyncio.run(
        checkpoint_values(runtime, "memory_import", proposal["workflow_id"])
    )
    assert failed["workflow_status"] == "failed"
    assert client.post(f"{base}/confirm").status_code == 409

    cancelled = _file_proposal(client, b"second")
    cancel_id = cancelled["proposal_id"]
    cancel_path = f"/api/memories/M-import/import-proposals/{cancel_id}"
    cancelled_payload = client.delete(cancel_path).json()
    assert cancelled_payload["status"] == "cancelled"
    assert cancelled_payload["proposal_id"] == cancel_id
    assert cancelled_payload["memory_id"] == "M-import"
    cancelled_state = asyncio.run(
        checkpoint_values(runtime, "memory_import", cancelled["workflow_id"])
    )
    assert cancelled_state["workflow_status"] == "cancelled"
    assert client.delete(cancel_path).status_code == 409


def test_unknown_commit_value_error_is_recorded_as_failed_workflow(web_client, monkeypatch):
    client, runtime, _policy = web_client
    proposal = _file_proposal(client)
    monkeypatch.setattr(
        runtime.memory_store,
        "commit_memory_import",
        lambda _proposal: (_ for _ in ()).throw(ValueError("unexpected commit failure")),
    )
    response = client.post(
        f"/api/memories/M-import/import-proposals/{proposal['proposal_id']}/confirm"
    )
    assert response.status_code == 409
    state = asyncio.run(
        checkpoint_values(runtime, "memory_import", proposal["workflow_id"])
    )
    assert state["workflow_status"] == "failed"
    assert state["result"]["error"] == "unexpected commit failure"


def test_static_page_has_explicit_import_preview_without_reader_or_auto_import():
    source = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    for required in (
        'id="importMemoryBtn"',
        '>导入资料<',
        'accept=".pdf,.txt,.md,.markdown"',
        'value="file"',
        'value="text"',
        'value="url"',
        "MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024",
        "MAX_IMPORT_TEXT_BYTES = 10 * 1024 * 1024",
        "reader.readAsDataURL(file)",
        "生成导入预览",
        "/import-proposals`,",
        "/confirm`,",
        "method: 'DELETE'",
        "$('importMarkdownPreview').textContent =",
        "$('importNoteMarkdownPreview').textContent =",
        "该资料已存在，不会重复写入附件或笔记",
    ):
        assert required in source
    import_section = source.split("// Memory 资料导入：显式预览后确认", 1)[1].split(
        "// 发送消息 → 用户对齐",
        1,
    )[0]
    for forbidden in (
        "FormData(",
        "multipart/form-data",
        "/api/alignment",
        "contenteditable",
        "pdfReader",
        "autoImport",
        ".innerHTML = payload.import_markdown",
        ".innerHTML = payload.note_markdown",
    ):
        assert forbidden not in import_section


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

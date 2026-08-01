"""W5 Web acceptance tests for explicit, confirmed Memory imports."""
from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web.server as server
from src.research.memory import MarkdownMemoryStore, MemoryWriteConflictError
from src.research.models import MemoryImportDuplicate, MemoryImportProposal


class ImportRuntime:
    def __init__(self, root: Path) -> None:
        self.memory_store = MarkdownMemoryStore(root)
        self.prepare_calls: list[tuple] = []
        self.commit_calls: list[MemoryImportProposal] = []
        self.research_calls = 0
        self.duplicate_next = False
        self.concurrent_duplicate = False
        self.force_conflict = False
        self.force_value_error = False
        self._counter = 0

    def create_memory(self, title: str, *, memory_id: str | None = None):
        return self.memory_store.create_memory(title, memory_id)

    def list_memories(self):
        return self.memory_store.list_memories()

    def get_memory(self, memory_id: str):
        return self.memory_store.get_memory(memory_id)

    def _duplicate(
        self,
        memory_id: str,
        source_kind: str,
        source_ref: str,
        content: bytes,
    ) -> MemoryImportDuplicate:
        prefix = f"Memories/{memory_id}"
        content_hash = hashlib.sha256(content).hexdigest()
        return MemoryImportDuplicate(
            memory_id=memory_id,
            import_id="Import-existing",
            source_kind=source_kind,
            source_ref=source_ref,
            locator="document" if source_kind == "file" else "inline",
            content_hash=content_hash,
            attachment_path=f"{prefix}/attachments/Asset-{content_hash}.txt",
            import_path=f"{prefix}/imports/Import-existing.md",
            note_path=f"{prefix}/notes/Note-existing.md",
            wikilinks=(
                f"[[{prefix}/imports/Import-existing]]",
                f"[[{prefix}/notes/Note-existing]]",
            ),
        )

    def _proposal(
        self,
        memory_id: str,
        source_kind: str,
        source_ref: str,
        locator: str,
        content: bytes,
    ) -> MemoryImportProposal | MemoryImportDuplicate:
        if self.duplicate_next:
            self.duplicate_next = False
            return self._duplicate(memory_id, source_kind, source_ref, content)
        self._counter += 1
        suffix = str(self._counter)
        prefix = f"Memories/{memory_id}"
        content_hash = hashlib.sha256(content).hexdigest()
        home_path = f"{prefix}/Home.md"
        home = self.memory_store.read_text(home_path)
        import_path = f"{prefix}/imports/Import-{suffix}.md"
        note_path = f"{prefix}/notes/Note-{suffix}.md"
        import_wikilink = f"[[{import_path[:-3]}]]"
        note_wikilink = f"[[{note_path[:-3]}]]"
        return MemoryImportProposal(
            proposal_id=f"ImportProposal-{suffix}",
            import_id=f"Import-{suffix}",
            note_id=f"Note-{suffix}",
            memory_id=memory_id,
            source_kind=source_kind,
            source_ref=source_ref,
            locator=locator,
            media_type="application/pdf" if source_ref.endswith(".pdf") else "text/plain",
            byte_size=len(content),
            content_hash=content_hash,
            attachment_path=f"{prefix}/attachments/Asset-{content_hash}.txt",
            attachment_bytes=content,
            import_path=import_path,
            import_markdown=f"---\nid: Import-{suffix}\n---\n# Imported\n\n{locator}\n",
            import_wikilink=import_wikilink,
            note_path=note_path,
            note_markdown=f"---\nid: Note-{suffix}\n---\n# Organized note\n",
            note_wikilink=note_wikilink,
            note_source_paths=(import_path,),
            home_path=home_path,
            home_content_hash=hashlib.sha256(home.encode("utf-8")).hexdigest(),
            home_markdown=f"{home.rstrip()}\n\n- {import_wikilink}\n- {note_wikilink}\n",
        )

    async def prepare_memory_file_import(
        self,
        memory_id: str,
        file_name: str,
        content: bytes,
    ) -> MemoryImportProposal | MemoryImportDuplicate:
        self.prepare_calls.append(("file", memory_id, file_name, content))
        return self._proposal(memory_id, "file", file_name, "document", content)

    async def prepare_memory_text_import(
        self,
        memory_id: str,
        title: str,
        text: str,
    ) -> MemoryImportProposal | MemoryImportDuplicate:
        self.prepare_calls.append(("text", memory_id, title, text))
        return self._proposal(memory_id, "text", title, "inline", text.encode("utf-8"))

    async def prepare_memory_url_import(
        self,
        memory_id: str,
        url: str,
    ) -> MemoryImportProposal | MemoryImportDuplicate:
        self.prepare_calls.append(("url", memory_id, url))
        return self._proposal(memory_id, "url", url, url.strip(), b"url content")

    def commit_memory_import(self, proposal: MemoryImportProposal):
        self.commit_calls.append(proposal)
        if self.force_conflict:
            raise MemoryWriteConflictError("Memory changed after import preview")
        if self.force_value_error:
            raise ValueError("unexpected commit failure")
        if self.concurrent_duplicate:
            return self._duplicate(
                proposal.memory_id,
                proposal.source_kind,
                proposal.source_ref,
                proposal.attachment_bytes,
            )
        return {
            "status": "committed",
            "memory_id": proposal.memory_id,
            "attachment_path": proposal.attachment_path,
            "import_path": proposal.import_path,
            "note_path": proposal.note_path,
            "home_path": proposal.home_path,
            "wikilinks": [proposal.import_wikilink, proposal.note_wikilink],
        }

    async def close(self, *, shutdown: bool = False) -> None:
        return None


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    runtime = ImportRuntime(tmp_path / "Vault")
    runtime.create_memory("Import", memory_id="M-import")
    runtime.create_memory("Other", memory_id="M-other")
    monkeypatch.setattr(server, "CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(server, "_config", {"research": {}})
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = runtime
    server._TASKS.clear()
    server._MEMORY_ANSWERS.clear()
    server._MEMORY_NOTE_PROPOSALS.clear()
    server._MEMORY_IMPORT_PROPOSALS.clear()
    with TestClient(server.app) as client:
        yield client, runtime
    server._TASKS.clear()
    server._MEMORY_ANSWERS.clear()
    server._MEMORY_NOTE_PROPOSALS.clear()
    server._MEMORY_IMPORT_PROPOSALS.clear()
    server.get_chat_store._store = None
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
    client, runtime = web_client
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
    assert runtime.prepare_calls == [
        ("file", "M-import", "source.txt", b"private attachment bytes"),
        ("text", "M-import", "Pasted", "Pasted body"),
        ("url", "M-import", original_url),
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
    assert runtime.commit_calls == []
    assert runtime.research_calls == 0
    assert server._TASKS == {}
    assert server.get_chat_store().get_messages("any-session") == []
    assert runtime.memory_store.read_text("Memories/M-import/Home.md") == home_before
    assert sorted(runtime.memory_store.root.rglob("*")) == files_before


def test_file_payload_rejects_invalid_base64_sizes_and_mixed_kinds(web_client):
    client, runtime = web_client
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
    assert runtime.prepare_calls == []


def test_duplicate_is_display_only_and_never_stored(web_client):
    client, runtime = web_client
    runtime.duplicate_next = True
    response = client.post(
        "/api/memories/M-import/import-proposals",
        json={"kind": "text", "title": "Existing", "text": "same"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "duplicate"
    assert payload["can_confirm"] is False
    assert payload["import_path"].endswith("Import-existing.md")
    assert payload["import_wikilink"].endswith("Import-existing]]")
    assert payload["import_obsidian_uri"].startswith("obsidian://open?")
    assert payload["note_obsidian_uri"].startswith("obsidian://open?")
    assert "attachment_bytes" not in payload
    assert server._MEMORY_IMPORT_PROPOSALS == {}
    assert runtime.commit_calls == []


def test_confirm_commits_once_returns_targets_and_consumes_proposal(web_client):
    client, runtime = web_client
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
    assert runtime.commit_calls[0].attachment_bytes == b"source text"
    assert proposal_id not in server._MEMORY_IMPORT_PROPOSALS
    assert client.post(
        f"/api/memories/M-import/import-proposals/{proposal_id}/confirm"
    ).status_code == 404


def test_confirm_cancel_strict_ids_conflict_and_concurrent_duplicate(web_client):
    client, runtime = web_client
    proposal = _file_proposal(client)
    proposal_id = proposal["proposal_id"]
    base = f"/api/memories/M-import/import-proposals/{proposal_id}"

    mismatch = client.post(
        f"/api/memories/M-other/import-proposals/{proposal_id}/confirm"
    )
    assert mismatch.status_code == 409
    assert proposal_id in server._MEMORY_IMPORT_PROPOSALS

    runtime.force_conflict = True
    conflict = client.post(f"{base}/confirm")
    assert conflict.status_code == 409
    assert proposal_id in server._MEMORY_IMPORT_PROPOSALS
    runtime.force_conflict = False

    runtime.concurrent_duplicate = True
    duplicate = client.post(f"{base}/confirm")
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert proposal_id not in server._MEMORY_IMPORT_PROPOSALS
    assert client.post(f"{base}/confirm").status_code == 404

    cancelled = _file_proposal(client, b"second")
    cancel_id = cancelled["proposal_id"]
    cancel_path = f"/api/memories/M-import/import-proposals/{cancel_id}"
    assert client.delete(cancel_path).json() == {
        "status": "cancelled",
        "proposal_id": cancel_id,
        "memory_id": "M-import",
    }
    assert cancel_id not in server._MEMORY_IMPORT_PROPOSALS
    assert client.delete(cancel_path).status_code == 404


def test_unknown_commit_value_error_is_not_mapped_to_conflict(web_client):
    client, runtime = web_client
    proposal = _file_proposal(client)
    runtime.force_value_error = True
    with pytest.raises(ValueError, match="unexpected commit failure"):
        client.post(
            f"/api/memories/M-import/import-proposals/{proposal['proposal_id']}/confirm"
        )
    assert proposal["proposal_id"] in server._MEMORY_IMPORT_PROPOSALS


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

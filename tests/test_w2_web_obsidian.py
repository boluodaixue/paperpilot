"""W2 Web acceptance tests for minimal Obsidian Memory access."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from tests._research_assessment import assessment_response
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

import web.server as server
from src.research.memory import MarkdownMemoryStore
from src.research.runtime import build_research_runtime


class FixedTool:
    name = "web_search"

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed W2 search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        return {"results": [{
            "title": "W2 source",
            "url": "https://example.test/w2",
            "snippet": "Obsidian installation is not required for research.",
        }]}


class FixedPolicy:
    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        system = str(messages[0].get("content", ""))
        if "before research begins" in system:
            return {"content": json.dumps({
                "objective": "Verify W2 remains optional",
                "scope": ["selected Memory"],
                "directions": ["fixed direction"],
                "constraints": ["no desktop dependency"],
                "expected_output": "Evidence-backed Markdown report",
            }), "tool_calls": []}
        if messages[-1]["role"] == "tool" or tools == []:
            return {"content": json.dumps({
                "status": "completed",
                "summary": "W2 research completed without Obsidian.",
                "findings": ["Obsidian installation is not required for research."],
                "unresolved": [],
            }), "tool_calls": []}
        return {"content": "", "tool_calls": [{
            "id": "call-search",
            "type": "function",
            "function": {"name": "web_search", "arguments": "{}"},
        }]}


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    runtime = build_research_runtime(
        config={},
        policy=FixedPolicy(),
        tools=[FixedTool()],
        memory_store=MarkdownMemoryStore(tmp_path / "Vault With 空格"),
        checkpointer=InMemorySaver(),
    )
    monkeypatch.setattr(server, "CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(server, "_config", {"research": {}})
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = runtime
    server._TASKS.clear()
    with TestClient(server.app) as client:
        yield client, runtime
    server._TASKS.clear()
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = None


def _wait_result(client: TestClient, task_id: str) -> dict[str, Any]:
    for _ in range(100):
        response = client.get(f"/api/tasks/{task_id}/result")
        if response.status_code == 200:
            return response.json()
        time.sleep(0.02)
    raise AssertionError("research task did not finish")


def test_memory_api_creates_lists_and_refreshes_external_home_title(web_client):
    client, runtime = web_client
    assert client.get("/api/memories").json() == []

    response = client.post("/api/memories", json={"title": "Original title"})
    assert response.status_code == 200
    created = response.json()
    assert created["title"] == "Original title"
    assert created["home_relative_path"] == (
        f"{created['relative_path']}Home.md"
    )
    assert Path(created["home_absolute_path"]).is_file()

    uri = urlsplit(created["obsidian_uri"])
    assert uri.scheme == "obsidian"
    assert uri.netloc == "open"
    assert parse_qs(uri.query)["path"] == [
        Path(created["home_absolute_path"]).as_posix()
    ]
    assert client.get("/api/memories").json() == [created]

    home = runtime.memory_store.root / created["home_relative_path"]
    markdown = home.read_text(encoding="utf-8")
    home.write_text(
        markdown.replace(
            'title: "Original title"',
            'title: "Externally edited title"',
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    refreshed = client.get("/api/memories")
    assert refreshed.status_code == 200
    assert refreshed.json()[0]["title"] == "Externally edited title"


def test_memory_api_rejects_invalid_and_duplicate_backend_inputs(web_client, monkeypatch):
    client, runtime = web_client
    assert client.post("/api/memories", json={"title": "   "}).status_code == 400
    assert client.post("/api/memories", json={"title": None}).status_code == 422

    def duplicate(_title: str):
        raise FileExistsError("Memory already exists")

    monkeypatch.setattr(runtime, "create_memory", duplicate)
    duplicate_response = client.post("/api/memories", json={"title": "Duplicate"})
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["detail"] == "Memory already exists"


def test_explicit_vault_name_uses_file_uri_and_invalid_config_is_500(web_client, monkeypatch):
    client, _runtime = web_client
    monkeypatch.setattr(server, "_config", {
        "research": {"vault_name": "  Paper Vault 中文  "},
    })
    created = client.post("/api/memories", json={"title": "Named Vault"}).json()
    query = parse_qs(urlsplit(created["obsidian_uri"]).query)
    assert query["vault"] == ["Paper Vault 中文"]
    assert query["file"] == [created["home_relative_path"]]
    assert "path" not in query

    monkeypatch.setattr(server, "_config", {"research": {"vault_name": "  "}})
    invalid = client.get("/api/memories")
    assert invalid.status_code == 500
    assert "research.vault_name" in invalid.json()["detail"]
    assert client.post(
        "/api/memories",
        json={"title": "Must not be created"},
    ).status_code == 500

    monkeypatch.setattr(server, "_config", {"research": {}})
    assert [item["title"] for item in client.get("/api/memories").json()] == [
        "Named Vault"
    ]


def test_research_does_not_depend_on_obsidian_installation(web_client):
    client, _runtime = web_client
    memory = client.post("/api/memories", json={"title": "No Obsidian"}).json()
    aligned = client.post("/api/alignment", json={
        "session_id": "no-obsidian",
        "memory_id": memory["memory_id"],
        "message": "Run research without opening another application",
    })
    assert aligned.status_code == 200
    task_id = aligned.json()["task_id"]
    assert client.post("/api/research", json={
        "task_id": task_id,
        "session_id": "no-obsidian",
    }).status_code == 200
    result = _wait_result(client, task_id)
    assert result["memory_id"] == memory["memory_id"]
    assert result["research_status"] == "completed"


def test_static_page_uses_memory_selector_and_plain_obsidian_link_only():
    source = (Path(server.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    server_source = Path(server.__file__).read_text(encoding="utf-8")
    for required in (
        'id="memorySelect"',
        'id="createMemoryBtn"',
        'id="memoryCreateCard"',
        'id="memoryCreateTitle"',
        'id="confirmCreateMemoryBtn"',
        'id="openObsidianBtn"',
        "fetch('/api/memories')",
        "memory_id: selectedMemoryId",
        "open.href = selected.obsidian_uri",
        "setMemorySelection(task.memory_id, true)",
        "lastReport.memory_id",
    ):
        assert required in source
    for forbidden in (
        "window.open(",
        "os.startfile",
        "/api/obsidian/open",
        "/api/memories/tree",
        "/api/memories/notes",
        "save-note",
        "prompt('新 Memory 标题')",
    ):
        assert forbidden not in source
    for forbidden in ("os.startfile", "subprocess", "webbrowser.open"):
        assert forbidden not in server_source
    paths = {route.path for route in server.app.routes}
    assert "/api/memories/{memory_id}/open" not in paths
    assert "/api/memories/{memory_id}/notes" not in paths

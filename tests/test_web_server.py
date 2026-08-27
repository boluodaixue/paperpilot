"""Offline Web acceptance tests for the N5 Research Workflow migration."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

import web.server as server
from src.research.memory import MarkdownMemoryStore
from src.research.runtime import build_research_runtime


class FixedTool:
    name = "web_search"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": "fixed search",
            "parameters": {"type": "object", "properties": {}},
        }}

    async def execute(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"results": [{
            "title": "Fixed source", "url": "https://example.test/source",
            "snippet": "A source-locatable fixed finding.",
        }]}


class FixedPolicy:
    def __init__(self) -> None:
        self.alignment_calls = 0
        self.research_calls = 0

    def __call__(self, messages, *, tools=None):
        system = str(messages[0].get("content", ""))
        if "before research begins" in system:
            self.alignment_calls += 1
            revision = max(0, self.alignment_calls - 1)
            return {"content": json.dumps({
                "objective": f"Fixed objective revision {revision}",
                "scope": ["fixed scope"],
                "directions": ["fixed direction"],
                "constraints": ["cite locations"],
                "expected_output": "Evidence-backed Markdown report",
            }), "tool_calls": []}
        self.research_calls += 1
        if messages[-1]["role"] == "tool":
            return {"content": json.dumps({
                "status": "completed", "summary": "Fixed summary.",
                "findings": ["A source-locatable fixed finding."],
                "unresolved": [],
            }), "tool_calls": []}
        return {"content": "", "tool_calls": [{
            "id": "call-search", "type": "function",
            "function": {"name": "web_search", "arguments": "{}"},
        }]}


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    policy, tool = FixedPolicy(), FixedTool()
    runtime = build_research_runtime(
        config={}, policy=policy, tools=[tool],
        memory_store=MarkdownMemoryStore(tmp_path / "memory"),
        checkpointer=InMemorySaver(),
    )
    monkeypatch.setattr(server, "CHAT_DB_PATH", str(tmp_path / "chat.db"))
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = runtime
    server._TASKS.clear()
    with TestClient(server.app) as client:
        yield client, runtime, policy, tool
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


def test_first_request_pauses_without_research_tools(web_client):
    client, runtime, policy, tool = web_client
    response = client.post("/api/alignment", json={"message": "Research fixed topic"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "waiting_confirmation"
    assert data["task_id"] == data["thread_id"]
    assert data["brief"]["revision"] == 0
    assert policy.research_calls == 0
    assert tool.calls == []
    task = server._TASKS[data["task_id"]]
    assert task.status == "waiting_confirmation"


def test_modify_and_confirm_resume_same_root_thread(web_client):
    client, runtime, policy, tool = web_client
    first = client.post("/api/alignment", json={"session_id": "s1", "message": "Question"}).json()
    revised = client.post("/api/alignment", json={
        "session_id": "s1", "task_id": first["task_id"],
        "message": "Narrow the scope",
    }).json()
    assert revised["task_id"] == first["task_id"]
    assert revised["thread_id"] == first["thread_id"]
    assert revised["brief"]["revision"] == 1
    assert tool.calls == []

    started = client.post("/api/research", json={
        "task_id": first["task_id"], "session_id": "s1",
    })
    assert started.status_code == 200
    result = _wait_result(client, first["task_id"])
    assert result["transport_status"] == "done"
    assert result["thread_id"] == first["thread_id"]
    assert result["research_status"] == "completed"
    assert len(tool.calls) == 1


@pytest.mark.asyncio
async def test_event_buffer_deduplicates_cumulative_events_and_replays_by_cursor():
    task = server.ResearchTask("root-events", "session-events", "q")
    event = {
        "kind": "agent_started", "thread_id": "root-events",
        "parent_thread_id": None, "root_thread_id": "root-events", "depth": 0,
    }
    await task.publish_execution_events([event])
    await task.publish_execution_events([event])
    second = {**event, "kind": "agent_finished", "status": "completed"}
    await task.publish_execution_events([event, second])
    assert [item["type"] for item in task.events] == ["agent_started", "agent_finished"]
    assert [item["sequence"] for item in task.events] == [1, 2]
    assert (await task.wait_after(1)) == [task.events[1]]
    assert all(item["root_thread_id"] == "root-events" for item in task.events)


def test_sse_cursor_replays_only_unseen_events(web_client):
    client, *_ = web_client
    task = server.ResearchTask("root-replay", "s", "q")
    task.status = "done"
    task.events = [
        {"type": "agent_started", "sequence": 1},
        {"type": "done", "sequence": 2},
    ]
    server._TASKS[task.task_id] = task
    response = client.get(f"/api/tasks/{task.task_id}/events?cursor=1")
    assert response.status_code == 200
    assert '"sequence": 1' not in response.text
    assert '"sequence": 2' in response.text


def test_two_sessions_keep_threads_isolated(web_client):
    client, runtime, *_ = web_client
    alpha = client.post("/api/alignment", json={"session_id": "alpha", "message": "A"}).json()
    beta = client.post("/api/alignment", json={"session_id": "beta", "message": "B"}).json()
    assert alpha["thread_id"] != beta["thread_id"]
    assert server._TASKS[alpha["task_id"]].session_id == "alpha"
    assert server._TASKS[beta["task_id"]].session_id == "beta"


def test_chat_stores_manifest_pointer_and_history_reads_markdown(web_client):
    client, runtime, *_ = web_client
    paused = client.post("/api/alignment", json={"session_id": "history", "message": "Q"}).json()
    client.post("/api/research", json={"task_id": paused["task_id"], "session_id": "history"})
    result = _wait_result(client, paused["task_id"])

    raw = server.get_chat_store().get_messages("history")[-1]
    pointer = json.loads(raw["content"])
    assert raw["kind"] == "report"
    assert pointer["manifest"]["report_path"] == result["manifest"]["report_path"]
    assert "Fixed summary" not in raw["content"]

    expanded = client.get("/api/sessions/history/messages").json()[-1]
    assert "Fixed summary" in expanded["content"]
    evidence = client.get("/api/sessions/history/evidence").json()["evidence"]
    assert evidence and "Fixed source" in evidence[0]["markdown"]


def test_session_delete_only_removes_chat_not_shared_memory(web_client):
    client, runtime, *_ = web_client
    server.get_chat_store().add("delete-me", "user", "chat", "hello")
    response = client.delete("/api/sessions/delete-me")
    assert response.status_code == 200
    assert response.json()["deleted"] == {"chat": 1}


def test_server_has_no_legacy_research_imports_or_graph_routes():
    source = Path(server.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "src.core.runner", "SharedMemoryStore", "EvidenceStore", "EvidenceGraph",
        "web.clarifier", "export_session_vault",
    ):
        assert forbidden not in source
    paths = {route.path for route in server.app.routes}
    assert "/api/sessions/{sid}/graph" not in paths
    assert "/api/sessions/{sid}/export-obsidian" not in paths

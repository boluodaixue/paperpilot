"""Offline Web acceptance tests for the N5 Research Workflow migration."""
from __future__ import annotations

import asyncio
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
from tests._research_assessment import assessment_response


_MEMORY_ID = "M-web"


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
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        system = str(messages[0].get("content", ""))
        if "Conversation Orchestrator" in system:
            request = json.loads(messages[-1]["content"])
            message = request["message"]
            if "Memory" in message:
                action, response, query = "memory_answer", "", message
            elif "研究" in message:
                action, response, query = "propose_research", "", message
            else:
                action, response, query = "reply", "我是 PaperPilot。", ""
            return {"content": json.dumps({
                "action": action,
                "confidence": 0.95,
                "response": response,
                "query": query,
                "reason_code": "fixture",
            }), "tool_calls": []}
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
        if messages[-1]["role"] == "tool" or tools == []:
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
    memory_store = MarkdownMemoryStore(tmp_path / "memory")
    memory_store.create_memory("Web regression", _MEMORY_ID)
    runtime = build_research_runtime(
        config={}, policy=policy, tools=[tool],
        memory_store=memory_store,
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


def test_conversation_route_allows_casual_chat_without_memory(web_client):
    client, runtime, policy, tool = web_client

    response = client.post(
        "/api/conversation/route",
        json={"message": "你是什么？"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "reply"
    assert payload["memory_id"] is None
    assert payload["response"] == "我是 PaperPilot。"
    assert server._TASKS == {}
    messages = server.get_chat_store().get_messages(payload["session_id"])
    assert [item["role"] for item in messages] == ["user", "assistant"]


def test_conversation_route_only_proposes_research(web_client):
    client, runtime, policy, tool = web_client

    response = client.post(
        "/api/conversation/route",
        json={
            "message": "研究 Transformer 的发展",
            "memory_id": _MEMORY_ID,
            "explicit_action": "deep_research",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "propose_research"
    assert payload["requires_confirmation"] is True
    assert payload["memory_id"] == _MEMORY_ID
    assert server._TASKS == {}
    assert policy.alignment_calls == 0
    assert policy.research_calls == 0


def test_first_request_pauses_without_research_tools(web_client):
    client, runtime, policy, tool = web_client
    response = client.post(
        "/api/alignment",
        json={"memory_id": _MEMORY_ID, "message": "Research fixed topic"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "waiting_confirmation"
    assert data["task_id"] == data["thread_id"]
    assert data["brief"]["revision"] == 0
    assert data["memory_id"] == _MEMORY_ID
    assert policy.research_calls == 0
    assert tool.calls == []
    task = server._TASKS[data["task_id"]]
    assert task.status == "waiting_confirmation"


def test_modify_and_confirm_resume_same_root_thread(web_client):
    client, runtime, policy, tool = web_client
    first = client.post("/api/alignment", json={
        "session_id": "s1", "memory_id": _MEMORY_ID, "message": "Question",
    }).json()
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


def test_sse_terminal_snapshot_is_followed_by_durable_outbox(web_client):
    client, runtime, *_ = web_client
    paused = client.post(
        "/api/alignment",
        json={
            "session_id": "sse-replay",
            "memory_id": _MEMORY_ID,
            "message": "Checkpointed SSE replay",
        },
    )
    assert paused.status_code == 200, paused.text
    task_id = paused.json()["task_id"]
    registry = server.get_runtime_registry()
    record = registry.get(task_id)
    assert record is not None
    cancelled = asyncio.run(
        runtime.review(
            record.thread_id,
            "cancel",
            session_id=record.session_id,
            memory_id=record.memory_id,
        )
    )
    assert cancelled["workflow_status"] == "cancelled"
    server._TASKS[task_id].status = "error"

    response = client.get(f"/api/tasks/{task_id}/events?cursor=0")
    assert response.status_code == 200
    assert '"type": "snapshot", "status": "cancelled"' in response.text
    assert '"type": "cancelled", "sequence": 1' in response.text
    assert response.text.index('"type": "snapshot"') < response.text.index(
        '"type": "cancelled"'
    )


def test_two_sessions_keep_threads_isolated(web_client):
    client, runtime, *_ = web_client
    alpha = client.post("/api/alignment", json={
        "session_id": "alpha", "memory_id": _MEMORY_ID, "message": "A",
    }).json()
    beta = client.post("/api/alignment", json={
        "session_id": "beta", "memory_id": _MEMORY_ID, "message": "B",
    }).json()
    assert alpha["thread_id"] != beta["thread_id"]
    assert server._TASKS[alpha["task_id"]].session_id == "alpha"
    assert server._TASKS[beta["task_id"]].session_id == "beta"


def test_chat_stores_manifest_pointer_and_history_reads_markdown(web_client):
    client, runtime, *_ = web_client
    paused = client.post("/api/alignment", json={
        "session_id": "history", "memory_id": _MEMORY_ID, "message": "Q",
    }).json()
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

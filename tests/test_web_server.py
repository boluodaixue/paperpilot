"""Web 服务器 API 测试（TestClient，不真调 LLM）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import web.server as server


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # 指向临时数据库，避免触碰真实 data/memory.db
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(server, "DB_PATH", db)
    return TestClient(server.app)


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "PaperPilot" in r.text
    assert "证据" in r.text


def test_list_sessions_returns_list(client):
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_start_research_returns_ids(client, monkeypatch):
    # 不真跑研究：替换后台任务为 no-op
    async def fake_run(task):
        task.status = "done"
        task.result = {"report_md": "# done"}

    monkeypatch.setattr(server, "_run_research_task", fake_run)

    r = client.post("/api/research", json={"query": "test question"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert data["task_id"]
    assert data["session_id"].startswith("web-")


def test_start_research_empty_query_rejected(client):
    r = client.post("/api/research", json={"query": "   "})
    assert r.status_code == 400


def test_start_research_reuses_session(client, monkeypatch):
    async def fake_run(task):
        task.status = "done"
        task.result = {}

    monkeypatch.setattr(server, "_run_research_task", fake_run)
    r = client.post("/api/research", json={"query": "q", "session_id": "my-session"})
    assert r.status_code == 200
    assert r.json()["session_id"] == "my-session"


def test_task_result_done(client):
    from web.server import ResearchTask

    task = ResearchTask("t123", "s1", "q")
    task.status = "done"
    task.result = {"session_id": "s1", "report_md": "# report", "evidence": []}
    server._TASKS["t123"] = task

    r = client.get("/api/tasks/t123/result")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "done"
    assert data["report_md"] == "# report"


def test_task_result_not_found(client):
    r = client.get("/api/tasks/nonexistent/result")
    assert r.status_code == 404


def test_session_graph_not_found(client):
    r = client.get("/api/sessions/no-such-session/graph")
    assert r.status_code == 404

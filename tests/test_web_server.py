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
    server.get_chat_store._store = None  # 重置 ChatStore 缓存，指向新 db
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


# ---------------------------------------------------------------------------
# 澄清 / 会话消息 / 报告落盘
# ---------------------------------------------------------------------------

def _stub_policy(json_str: str):
    class P:
        def __call__(self, messages):
            return {"content": json_str}
    return P()


def test_clarify_confirm_writes_proposal(client, monkeypatch):
    monkeypatch.setattr(
        server, "_get_clarifier_policy",
        lambda: _stub_policy(
            '{"action":"confirm","plan":{"topic":"T","scope":"S","angle":"A",'
            '"depth":"D","focus_areas":["x","y"]},"research_query":"final query"}'
        ),
    )
    r = client.post("/api/clarify", json={"message": "研究transformer"})
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "confirm"
    assert data["session_id"].startswith("web-")
    assert data["research_query"] == "final query"
    assert data["plan"]["focus_areas"] == ["x", "y"]

    msgs = client.get(f"/api/sessions/{data['session_id']}/messages").json()
    assert [m["kind"] for m in msgs] == ["chat", "proposal"]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_clarify_ask_stores_question(client, monkeypatch):
    monkeypatch.setattr(
        server, "_get_clarifier_policy",
        lambda: _stub_policy('{"action":"ask","question":"想侧重哪个阶段？"}'),
    )
    r = client.post("/api/clarify", json={"session_id": "s1", "message": "研究transformer"})
    assert r.status_code == 200
    assert r.json()["action"] == "ask"
    msgs = client.get("/api/sessions/s1/messages").json()
    assert msgs[-1]["role"] == "assistant"
    assert "阶段" in msgs[-1]["content"]


def test_sessions_list_uses_chat_title(client):
    r = client.post("/api/clarify", json={"message": "这是第一条会话消息"})
    sid = r.json()["session_id"]
    sessions = client.get("/api/sessions").json()
    titles = {s["session_id"]: s.get("title", "") for s in sessions}
    assert sid in titles
    assert titles[sid].startswith("这是第一条会话消息")


def test_session_evidence_endpoint_empty_ok(client):
    r = client.get("/api/sessions/nonexistent/evidence")
    assert r.status_code == 200
    data = r.json()
    assert data["evidence"] == []
    assert data["evidence_relations"] == []


def test_research_persists_report_to_chat(tmp_path, monkeypatch):
    """研究完成后报告以 kind='report' 落盘 chat_messages。"""
    import asyncio

    from web.server import ResearchTask

    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "t.db"))
    server.get_chat_store._store = None

    async def fake_run_research(query, config, modules, progress_callback=None):
        return "# 报告正文\n\n" + "证据内容 " * 80  # 超过 300 字符阈值，视为正常报告

    def fake_initialize(config, session_id=""):
        return {}  # initialize_modules 是同步函数

    monkeypatch.setattr("src.core.runner.run_research", fake_run_research)
    monkeypatch.setattr("src.core.runner.initialize_modules", fake_initialize)

    task = ResearchTask("tid", "sess-x", "q")
    asyncio.run(server._run_research_task(task))
    assert task.status == "done"
    msgs = server.get_chat_store().get_messages("sess-x")
    assert msgs[-1]["kind"] == "report"
    assert "报告正文" in msgs[-1]["content"]

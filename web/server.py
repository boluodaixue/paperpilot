#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web/server.py
================================================================================
PaperPilot Web 服务器：FastAPI + SSE 进度流 + 单页前端。

研究任务是长任务（数分钟），采用"后台 asyncio 任务 + SSE 流式进度"模式：
  1. POST /api/research     提交问题，立即返回 task_id + session_id
  2. GET  /api/tasks/{id}/events   SSE 实时进度流
  3. GET  /api/tasks/{id}/result   完成后取完整结果
  4. GET  /api/sessions            历史 session 列表
  5. GET  /api/sessions/{id}/graph 证据关系图数据（export_json）

单用户本地工具：任务状态只存内存，不落盘；研究任务串行执行
（WebSearchTool 共享连接池，且火山 key 有速率限制）。
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

# 将项目根目录加入 sys.path，确保 src 包可导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 锚定工作目录到项目根：configs/db_path/outputs 都是相对路径，
# 无论服务器从哪里启动都正确解析（否则 data/memory.db 会落到别的目录）
os.chdir(PROJECT_ROOT)

# 中文 Windows 控制台默认 GBK：orchestrator 打印的 ✓/✗ 字符会触发
# UnicodeEncodeError，导致整个研究任务崩溃（和 run_single.py 同因）。
# 统一重配为 UTF-8，errors='replace' 兜底。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from src.utils.env_config import ensure_env_loaded  # noqa: E402

ensure_env_loaded()
from src.core.runner import load_config  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
_config = load_config()
DB_PATH = _config.get("memory", {}).get("db_path", "data/memory.db")

from src.memory.chat_store import KIND_PROPOSAL, KIND_REPORT, ChatStore  # noqa: E402

app = FastAPI(title="PaperPilot", version="0.1.0")


def get_chat_store() -> ChatStore:
    """惰性单例 ChatStore（db 锚定到项目根，chdir 已在上方执行）。"""
    store = getattr(get_chat_store, "_store", None)
    if store is None:
        store = ChatStore(DB_PATH)
        get_chat_store._store = store
    return store


def _get_clarifier_policy():
    """澄清用的 LLM policy（短 prompt，max_tokens 收紧）。"""
    from src.models.model_router import ModelRouter

    return ModelRouter.create_backend("deepseek", max_tokens=800, temperature=0.1)


# ---------------------------------------------------------------------------
# 任务管理器（内存态）
# ---------------------------------------------------------------------------

class ResearchTask:
    """单个研究任务：事件队列 + 状态 + 结果。"""

    def __init__(self, task_id: str, session_id: str, query: str) -> None:
        self.task_id = task_id
        self.session_id = session_id
        self.query = query
        self.events: asyncio.Queue = asyncio.Queue()
        self.status: str = "running"  # running | done | error
        self.result: dict | None = None
        self.error: str | None = None
        self.created_at = time.time()


_TASKS: dict[str, ResearchTask] = {}
# 研究任务串行执行（WebSearchTool 共享连接池 + API 速率限制）
_RUN_SEMAPHORE = asyncio.Semaphore(1)


class ResearchRequest(BaseModel):
    query: str
    session_id: str | None = None


def _collect_evidence_data(modules: dict) -> tuple[list, list]:
    """从 store/graph 收集结构化证据与关系（公开 API，不依赖私有状态）。"""
    evidence: list = []
    store = modules.get("evidence_store")
    if store is not None:
        evidence = [ev.to_report_dict() for ev in store.get_all()]
    relations: list = []
    graph = modules.get("evidence_graph")
    if graph is not None:
        for r in graph.get_contradictions(limit=20):
            relations.append(r.to_dict())
        for r in graph.get_supports(limit=10):
            relations.append(r.to_dict())
        for r in graph.get_extends(limit=10):
            relations.append(r.to_dict())
    return evidence, relations


async def _run_research_task(task: ResearchTask) -> None:
    """后台执行完整研究流程，进度事件推入 task.events。"""
    try:
        async with _RUN_SEMAPHORE:
            from src.core.runner import initialize_modules, run_research

            config = load_config()
            modules = initialize_modules(config, session_id=task.session_id)

            start = time.time()
            report_md = await run_research(
                task.query,
                config,
                modules,
                progress_callback=lambda ev: task.events.put_nowait(ev),
            )
            elapsed = time.time() - start

            # 合成失败会产出降级短报告（如 "Synthesis failed." 或超时兜底），
            # 不应当作成功报告落盘展示
            if len((report_md or "").strip()) < 300:
                raise RuntimeError(
                    f"研究报告生成失败（内容过短，{len(report_md or '')} 字符；"
                    f"可能是合成超时或模型限流）"
                )

            evidence, relations = _collect_evidence_data(modules)
            result = {
                "task_id": task.task_id,
                "session_id": task.session_id,
                "query": task.query,
                "elapsed": round(elapsed, 1),
                "report_md": report_md,
                "evidence": evidence,
                "evidence_relations": relations,
            }

            task.result = result
            task.status = "done"
            await task.events.put({"type": "done", "session_id": task.session_id})
            # 研究报告持久化到会话消息（重启服务器后可回溯历史）
            try:
                get_chat_store().add(task.session_id, "assistant", KIND_REPORT, report_md)
            except Exception as e:
                print(f"[Chat] 报告落盘失败: {e}")
    except Exception as e:
        task.status = "error"
        task.error = str(e)
        await task.events.put({"type": "error", "message": str(e)[:500]})


def _get_task(task_id: str) -> ResearchTask:
    task = _TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return task


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="前端页面缺失")
    return html_path.read_text(encoding="utf-8")


@app.post("/api/research")
async def start_research(req: ResearchRequest) -> dict:
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")

    session_id = (req.session_id or "").strip() or f"web-{uuid.uuid4().hex[:8]}"
    task_id = uuid.uuid4().hex[:12]

    task = ResearchTask(task_id, session_id, query)
    _TASKS[task_id] = task
    asyncio.create_task(_run_research_task(task))

    return {"task_id": task_id, "session_id": session_id, "status": "running"}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str) -> StreamingResponse:
    """SSE 进度流：推送 state/task_done/evidence/synthesis/done/error 事件。"""
    task = _get_task(task_id)

    async def event_stream():
        # 任务已完成才连接：直接发最终状态后结束
        if task.status in ("done", "error") and task.events.empty():
            ev = {"type": "done"} if task.status == "done" else {"type": "error", "message": task.error or ""}
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            return
        while True:
            try:
                ev = await asyncio.wait_for(task.events.get(), timeout=30)
            except asyncio.TimeoutError:
                # 心跳，保持连接
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") in ("done", "error"):
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/tasks/{task_id}/result")
async def task_result(task_id: str) -> dict:
    task = _get_task(task_id)
    if task.status == "running":
        raise HTTPException(status_code=202, detail="任务仍在执行")
    if task.status == "error":
        return {"task_id": task_id, "status": "error", "message": task.error or ""}
    return {"status": "done", **task.result}


@app.get("/api/sessions/{sid}/active-task")
async def session_active_task(sid: str) -> dict:
    """查询某会话是否有正在运行的研究任务（前端切换会话后重连 SSE 用）。"""
    for task in _TASKS.values():
        if task.session_id == sid and task.status == "running":
            return {"task_id": task.task_id, "status": "running"}
    return {"task_id": None, "status": "idle"}


@app.get("/api/sessions")
async def list_sessions() -> list:
    """侧边栏会话列表：优先聊天记录（含首条消息预览），补老会话（仅有记忆/证据）。"""
    sessions = get_chat_store().list_sessions()
    known = {s["session_id"] for s in sessions}
    try:
        from src.memory.memory_store import SharedMemoryStore

        legacy = SharedMemoryStore(db_path=DB_PATH, session_id="")
        for s in legacy.list_sessions():
            if s["session_id"] not in known:
                sessions.append({
                    "session_id": s["session_id"],
                    "title": s["session_id"],
                    "pinned": False,
                    "sort_order": 0.0,
                    "last_update": s["last_update"],
                    "count": s["count"],
                })
    except Exception:
        pass
    # 与 ChatStore.list_sessions 同款排序：置顶优先 → 拖拽顺序 → 更新时间兜底
    sessions.sort(key=lambda s: (
        not s.get("pinned", False),
        s.get("sort_order", 0.0),
        -s.get("last_update", 0),
    ))
    return sessions


class ClarifyRequest(BaseModel):
    session_id: str | None = None
    message: str


@app.post("/api/clarify")
async def clarify(req: ClarifyRequest) -> dict:
    """研究前澄清：追加用户消息，调 clarifier，返回 ask（追问）或 confirm（研究方案）。"""
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = (req.session_id or "").strip() or f"web-{uuid.uuid4().hex[:8]}"
    store = get_chat_store()
    store.add(session_id, "user", "chat", message)
    history = store.get_messages(session_id)

    from web.clarifier import run_clarifier

    try:
        result = await asyncio.to_thread(
            run_clarifier, _get_clarifier_policy(), history, message
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"澄清失败: {e}")

    if result["action"] == "confirm":
        plan = result.get("plan") or {}
        proposal = {
            "topic": plan.get("topic", ""),
            "scope": plan.get("scope", ""),
            "angle": plan.get("angle", ""),
            "depth": plan.get("depth", ""),
            "focus_areas": plan.get("focus_areas") or [],
        }
        research_query = result.get("research_query", message)
        store.add(
            session_id,
            "assistant",
            KIND_PROPOSAL,
            json.dumps({"plan": proposal, "research_query": research_query}, ensure_ascii=False),
        )
        return {
            "session_id": session_id,
            "action": "confirm",
            "plan": proposal,
            "research_query": research_query,
        }

    store.add(session_id, "assistant", "chat", result["question"])
    return {"session_id": session_id, "action": "ask", "question": result["question"]}


@app.get("/api/sessions/{sid}/messages")
async def session_messages(sid: str) -> list:
    """返回某会话的完整消息历史（含研究报告留档）。"""
    return get_chat_store().get_messages(sid)


@app.get("/api/sessions/{sid}/evidence")
async def session_evidence(sid: str) -> dict:
    """某会话的结构化证据 + 关系（供历史报告卡渲染，不依赖内存任务态）。"""
    from src.evidence.graph import EvidenceGraph
    from src.evidence.store import EvidenceStore

    try:
        store = EvidenceStore(db_path=DB_PATH, session_id=sid)
        evidence = [ev.to_report_dict() for ev in store.get_all()]
    except Exception:
        evidence = []
    relations: list = []
    try:
        graph = EvidenceGraph(db_path=DB_PATH, session_id=sid)
        for r in graph.get_contradictions(limit=20):
            relations.append(r.to_dict())
        for r in graph.get_supports(limit=10):
            relations.append(r.to_dict())
        for r in graph.get_extends(limit=10):
            relations.append(r.to_dict())
    except Exception:
        pass
    return {"evidence": evidence, "evidence_relations": relations, "session_id": sid}


class SessionRename(BaseModel):
    title: str


@app.patch("/api/sessions/{sid}")
async def session_rename(sid: str, req: SessionRename) -> dict:
    """重命名会话（侧边栏标题）。"""
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    get_chat_store().set_meta(sid, title=title)
    return {"session_id": sid, "title": title}


class SessionPin(BaseModel):
    pinned: bool


@app.post("/api/sessions/{sid}/pin")
async def session_pin(sid: str, req: SessionPin) -> dict:
    """置顶 / 取消置顶会话。"""
    get_chat_store().set_meta(sid, pinned=bool(req.pinned))
    return {"session_id": sid, "pinned": bool(req.pinned)}


class SessionOrder(BaseModel):
    session_ids: list[str]


@app.post("/api/sessions/order")
async def session_order(req: SessionOrder) -> dict:
    """拖拽排序：session_ids 为新的展示顺序。"""
    get_chat_store().set_sort_order(req.session_ids)
    return {"ok": True, "count": len(req.session_ids)}


@app.delete("/api/sessions/{sid}")
async def session_delete(sid: str) -> dict:
    """全量删除会话：聊天记录 + 报告 + 证据 + 关系边 + 记忆 + 冲突。"""
    import sqlite3 as _sqlite3

    deleted = {"chat": 0, "evidence": 0, "edges": 0, "entries": 0, "conflicts": 0}
    with _sqlite3.connect(DB_PATH) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "entries" in tables:
            entry_ids = [r[0] for r in conn.execute(
                "SELECT entry_id FROM entries WHERE session_id = ?", (sid,)
            )]
            if entry_ids and "conflicts" in tables:
                placeholders = ",".join("?" * len(entry_ids))
                deleted["conflicts"] = conn.execute(
                    f"DELETE FROM conflicts WHERE entry_id_1 IN ({placeholders}) "
                    f"OR entry_id_2 IN ({placeholders})",
                    [*entry_ids, *entry_ids],
                ).rowcount
            deleted["entries"] = conn.execute(
                "DELETE FROM entries WHERE session_id = ?", (sid,)
            ).rowcount
        if "evidence" in tables:
            deleted["evidence"] = conn.execute(
                "DELETE FROM evidence WHERE session_id = ?", (sid,)
            ).rowcount
        if "evidence_edges" in tables:
            deleted["edges"] = conn.execute(
                "DELETE FROM evidence_edges WHERE session_id = ?", (sid,)
            ).rowcount
        conn.commit()
    deleted["chat"] = get_chat_store().delete_session(sid)
    return {"session_id": sid, "deleted": deleted}


@app.get("/api/sessions/{sid}/graph")
async def session_graph(sid: str) -> dict:
    from src.evidence.graph import EvidenceGraph

    graph = EvidenceGraph(db_path=DB_PATH, session_id=sid)
    data = graph.export_json()
    if not data["nodes"]:
        raise HTTPException(status_code=404, detail=f"session '{sid}' 没有证据数据")
    return {"nodes": data["nodes"], "edges": data["edges"], "stats": graph.graph_stats()}

#!/usr/bin/env python3
"""PaperPilot Web server backed only by the homogeneous Research Workflow."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.memory.chat_store import KIND_PROPOSAL, KIND_REPORT, ChatStore  # noqa: E402
from src.research.memory import MemoryWriteConflictError  # noqa: E402
from src.research.models import (  # noqa: E402
    MemoryAnswer,
    MemoryDescriptor,
    MemoryNoteProposal,
    ResearchWorkflowResult,
)
from src.research.obsidian import build_obsidian_open_uri  # noqa: E402
from src.research.runtime import ResearchRuntime, build_research_runtime, load_config  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
_config = load_config()
_configured_chat_db = Path(str(_config.get("chat", {}).get("db_path", "data/chat.db")))
CHAT_DB_PATH = str(
    _configured_chat_db if _configured_chat_db.is_absolute()
    else PROJECT_ROOT / _configured_chat_db
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    runtime = getattr(get_research_runtime, "_runtime", None)
    if runtime is not None:
        try:
            await runtime.close(shutdown=True)
        except Exception as exc:
            print(f"[Runtime] 关闭失败: {exc}")


app = FastAPI(title="PaperPilot", version="0.2.0", lifespan=_lifespan)


def get_chat_store() -> ChatStore:
    store = getattr(get_chat_store, "_store", None)
    if store is None:
        store = ChatStore(CHAT_DB_PATH)
        get_chat_store._store = store
    return store


def get_research_runtime() -> ResearchRuntime:
    """Return one process-level runtime, graph, and checkpointer."""
    runtime = getattr(get_research_runtime, "_runtime", None)
    if runtime is None:
        runtime = build_research_runtime()
        get_research_runtime._runtime = runtime
    return runtime


class ResearchTask:
    def __init__(
        self,
        task_id: str,
        session_id: str,
        query: str,
        memory_id: str | None = None,
    ) -> None:
        self.task_id = self.thread_id = task_id
        self.session_id, self.query = session_id, query
        self.memory_id = memory_id
        self.status = "waiting_confirmation"
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.events: list[dict[str, Any]] = []
        self._condition = asyncio.Condition()
        self._emitted: Counter[str] = Counter()

    async def publish(self, event: dict[str, Any]) -> None:
        async with self._condition:
            self.events.append({**event, "sequence": len(self.events) + 1})
            self._condition.notify_all()

    async def publish_execution_events(self, events: Iterable[dict[str, Any]]) -> None:
        occurrences: Counter[str] = Counter()
        for raw in events:
            if not isinstance(raw, dict) or not raw.get("kind"):
                continue
            key = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
            occurrences[key] += 1
            if occurrences[key] <= self._emitted[key]:
                continue
            self._emitted[key] = occurrences[key]
            await self.publish({"type": raw["kind"], **raw})

    async def wait_after(self, cursor: int) -> list[dict[str, Any]]:
        async with self._condition:
            if len(self.events) <= cursor and self.status not in {"done", "error"}:
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=30)
                except asyncio.TimeoutError:
                    return []
            return [event for event in self.events if event["sequence"] > cursor]


_TASKS: dict[str, ResearchTask] = {}
_RUN_SEMAPHORE = asyncio.Semaphore(1)
_MEMORY_ANSWERS: dict[str, MemoryAnswer] = {}
_MEMORY_NOTE_PROPOSALS: dict[str, MemoryNoteProposal] = {}


class AlignmentRequest(BaseModel):
    session_id: str | None = None
    task_id: str | None = None
    memory_id: str | None = None
    message: str


class ResearchRequest(BaseModel):
    task_id: str
    session_id: str | None = None


class MemoryCreateRequest(BaseModel):
    title: str


class MemoryAnswerRequest(BaseModel):
    question: str


class MemoryNoteProposalRequest(BaseModel):
    answer_id: str


class SessionRename(BaseModel):
    title: str


class SessionPin(BaseModel):
    pinned: bool


class SessionOrder(BaseModel):
    session_ids: list[str]


def _get_task(task_id: str) -> ResearchTask:
    if task_id not in _TASKS:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _TASKS[task_id]


def _active_task(session_id: str) -> ResearchTask | None:
    matches = [
        task for task in _TASKS.values()
        if task.session_id == session_id
        and task.status in {"waiting_confirmation", "running"}
    ]
    return max(matches, key=lambda item: item.created_at) if matches else None


def _interrupt_brief(state: dict[str, Any]) -> dict[str, Any]:
    interrupts = state.get("__interrupt__") or ()
    if len(interrupts) != 1:
        raise RuntimeError("workflow did not pause for one brief review")
    payload = interrupts[0].value
    if not isinstance(payload, dict) or payload.get("kind") != "research_brief_confirmation":
        raise RuntimeError("workflow returned an invalid review payload")
    return payload["brief"]


def _proposal_pointer(task: ResearchTask, brief: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task_id": task.task_id,
            "thread_id": task.thread_id,
            "memory_id": task.memory_id,
            "brief": brief,
        },
        ensure_ascii=False,
    )


def _report_pointer(task: ResearchTask, result: ResearchWorkflowResult) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "thread_id": task.thread_id,
        "memory_id": result.memory_id,
        "manifest": asdict(result.memory_manifest),
    }


def _latest_report_pointer(session_id: str) -> dict[str, Any] | None:
    for message in reversed(get_chat_store().get_messages(session_id)):
        if message.get("kind") == KIND_REPORT:
            try:
                value = json.loads(message.get("content") or "")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("manifest"), dict):
                return value
    return None


def _configured_vault_name() -> str | None:
    research = _config.get("research", {})
    value = research.get("vault_name") if isinstance(research, dict) else None
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=500,
            detail="research.vault_name 必须是非空字符串",
        )
    return value.strip()


def _memory_response(
    runtime: ResearchRuntime,
    descriptor: MemoryDescriptor,
) -> dict[str, Any]:
    home_relative_path = f"{descriptor.relative_path}Home.md"
    vault_root = runtime.memory_store.root
    home_absolute_path = (vault_root / home_relative_path).resolve(strict=False)
    return {
        **asdict(descriptor),
        "home_relative_path": home_relative_path,
        "home_absolute_path": str(home_absolute_path),
        "obsidian_uri": build_obsidian_open_uri(
            vault_root,
            home_relative_path,
            vault_name=_configured_vault_name(),
        ),
    }


def _answer_response(
    runtime: ResearchRuntime,
    answer: MemoryAnswer,
) -> dict[str, Any]:
    payload = asdict(answer)
    payload["citations"] = [
        {
            **asdict(citation),
            "obsidian_uri": build_obsidian_open_uri(
                runtime.memory_store.root,
                citation.relative_path,
                vault_name=_configured_vault_name(),
            ),
        }
        for citation in answer.citations
    ]
    return payload


def _commit_response(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    elif is_dataclass(value):
        payload = asdict(value)
    else:
        fields = ("memory_id", "target_path", "home_path", "wikilink")
        try:
            payload = {field: getattr(value, field) for field in fields}
        except AttributeError as exc:
            raise RuntimeError("Memory note commit returned an invalid result") from exc
    required = {"memory_id", "target_path", "home_path", "wikilink"}
    if not required.issubset(payload):
        raise RuntimeError("Memory note commit returned an invalid result")
    return payload


def _expanded_messages(session_id: str) -> list[dict[str, Any]]:
    messages = get_chat_store().get_messages(session_id)
    runtime: ResearchRuntime | None = None
    for message in messages:
        if message.get("kind") != KIND_REPORT:
            continue
        try:
            pointer = json.loads(message["content"])
            manifest = pointer["manifest"]
            runtime = runtime or get_research_runtime()
            message["content"] = runtime.read_memory(manifest["report_path"])
            message["manifest"] = manifest
            message["thread_id"] = pointer.get("thread_id")
            message["memory_id"] = pointer.get("memory_id")
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            message["content"] = "报告文件暂时不可读取。"
    return messages


def _event_lists(value: Any) -> Iterable[list[dict[str, Any]]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "execution_events" and isinstance(child, list):
                yield child
            else:
                yield from _event_lists(child)
    elif isinstance(value, tuple):
        for child in value:
            yield from _event_lists(child)


async def _stream_confirm(task: ResearchTask) -> dict[str, Any]:
    runtime = get_research_runtime()
    config = {"configurable": {"thread_id": task.thread_id}}
    async for update in runtime.graph.astream(
        Command(resume={"action": "confirm"}),
        config=config,
        stream_mode="updates",
        subgraphs=True,
    ):
        for events in _event_lists(update):
            await task.publish_execution_events(events)
    state = await runtime.get_state(task.thread_id)
    await task.publish_execution_events(state.get("execution_events", []))
    return state


async def _run_research_task(task: ResearchTask) -> None:
    try:
        async with _RUN_SEMAPHORE:
            started = time.time()
            state = await _stream_confirm(task)
            workflow_result = state.get("workflow_result")
            if not isinstance(workflow_result, ResearchWorkflowResult):
                raise RuntimeError("workflow ended without a structured result")
            research_result = workflow_result.research_result
            task.result = {
                "task_id": task.task_id,
                "thread_id": task.thread_id,
                "session_id": task.session_id,
                "memory_id": workflow_result.memory_id,
                "query": task.query,
                "elapsed": round(time.time() - started, 1),
                "research_status": research_result.status.value,
                "stop_reason": research_result.stop_reason,
                "report_md": workflow_result.report_markdown,
                "evidence": [asdict(item) for item in research_result.evidence],
                "manifest": asdict(workflow_result.memory_manifest),
            }
            try:
                get_chat_store().add(
                    task.session_id, "assistant", KIND_REPORT,
                    json.dumps(_report_pointer(task, workflow_result), ensure_ascii=False),
                )
            except Exception as exc:
                print(f"[Chat] 报告引用落盘失败: {exc}")
            task.status = "done"
            await task.publish({
                "type": "done", "thread_id": task.thread_id,
                "parent_thread_id": None, "root_thread_id": task.thread_id,
                "session_id": task.session_id, "memory_id": workflow_result.memory_id,
            })
    except Exception as exc:
        task.status, task.error = "error", str(exc)
        await task.publish({
            "type": "error", "message": str(exc)[:500],
            "thread_id": task.thread_id, "parent_thread_id": None,
            "root_thread_id": task.thread_id, "memory_id": task.memory_id,
        })


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    path = STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=500, detail="前端页面缺失")
    return path.read_text(encoding="utf-8")


@app.post("/api/alignment")
async def align_research(req: AlignmentRequest) -> dict[str, Any]:
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")
    runtime, store = get_research_runtime(), get_chat_store()
    if req.task_id:
        task = _get_task(req.task_id)
        if req.session_id and req.session_id != task.session_id:
            raise HTTPException(status_code=409, detail="task 与 session 不匹配")
        if req.memory_id is not None and req.memory_id != task.memory_id:
            raise HTTPException(status_code=409, detail="task 与 memory 不匹配")
        if task.status != "waiting_confirmation":
            raise HTTPException(status_code=409, detail="任务当前不等待方案修改")
        store.add(task.session_id, "user", "chat", message)
        try:
            state = await runtime.review(task.thread_id, "modify", feedback=message)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"方案修改失败: {exc}") from exc
    else:
        session_id = (req.session_id or "").strip() or f"web-{uuid.uuid4().hex[:8]}"
        if _active_task(session_id):
            raise HTTPException(status_code=409, detail="该会话已有待确认或运行中的研究")
        thread_id = runtime.new_thread_id()
        task = ResearchTask(thread_id, session_id, message, req.memory_id)
        _TASKS[thread_id] = task
        store.add(session_id, "user", "chat", message)
        try:
            state = await runtime.start(
                message,
                thread_id=thread_id,
                memory_id=req.memory_id,
            )
        except Exception as exc:
            task.status, task.error = "error", str(exc)
            raise HTTPException(status_code=500, detail=f"研究说明生成失败: {exc}") from exc
    brief = _interrupt_brief(state)
    store.add(task.session_id, "assistant", KIND_PROPOSAL, _proposal_pointer(task, brief))
    task.status = "waiting_confirmation"
    return {
        "session_id": task.session_id, "task_id": task.task_id,
        "thread_id": task.thread_id, "status": task.status,
        "memory_id": task.memory_id, "action": "confirm", "brief": brief,
    }


@app.post("/api/research")
async def start_research(req: ResearchRequest) -> dict[str, Any]:
    task = _get_task(req.task_id)
    if req.session_id and req.session_id != task.session_id:
        raise HTTPException(status_code=409, detail="task 与 session 不匹配")
    if task.status != "waiting_confirmation":
        raise HTTPException(status_code=409, detail="任务当前不能确认启动")
    task.status = "running"
    await task.publish({
        "type": "confirmed", "thread_id": task.thread_id,
        "parent_thread_id": None, "root_thread_id": task.thread_id,
        "memory_id": task.memory_id,
    })
    asyncio.create_task(_run_research_task(task))
    return {"task_id": task.task_id, "thread_id": task.thread_id,
            "session_id": task.session_id, "memory_id": task.memory_id,
            "status": task.status}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, cursor: int = 0,
                      last_event_id: str | None = Header(None, alias="Last-Event-ID")) -> StreamingResponse:
    task = _get_task(task_id)
    try:
        cursor = max(0, cursor, int(last_event_id or 0))
    except ValueError:
        cursor = max(0, cursor)

    async def stream():
        current = cursor
        while True:
            events = await task.wait_after(current)
            if not events:
                if task.status in {"done", "error"}:
                    return
                yield ": keep-alive\n\n"
                continue
            for event in events:
                current = event["sequence"]
                yield f"id: {current}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if task.status in {"done", "error"} and current >= len(task.events):
                return
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/tasks/{task_id}/result")
async def task_result(task_id: str) -> dict[str, Any]:
    task = _get_task(task_id)
    if task.status in {"waiting_confirmation", "running"}:
        raise HTTPException(status_code=202, detail="任务尚未完成")
    if task.status == "error":
        return {
            "task_id": task_id,
            "memory_id": task.memory_id,
            "status": "error",
            "message": task.error or "",
        }
    return {"transport_status": "done", **(task.result or {})}


@app.get("/api/sessions/{sid}/active-task")
async def session_active_task(sid: str) -> dict[str, Any]:
    task = _active_task(sid)
    return ({"task_id": None, "memory_id": None, "status": "idle"} if task is None else
            {"task_id": task.task_id, "thread_id": task.thread_id,
             "memory_id": task.memory_id, "status": task.status})


@app.get("/api/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return get_chat_store().list_sessions()


@app.get("/api/memories")
async def list_memories() -> list[dict[str, Any]]:
    runtime = get_research_runtime()
    return [_memory_response(runtime, item) for item in runtime.list_memories()]


@app.post("/api/memories")
async def create_memory(req: MemoryCreateRequest) -> dict[str, Any]:
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Memory 标题不能为空")
    _configured_vault_name()
    runtime = get_research_runtime()
    try:
        descriptor = runtime.create_memory(title)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _memory_response(runtime, descriptor)


@app.post("/api/memories/{memory_id}/answers")
async def answer_memory(
    memory_id: str,
    req: MemoryAnswerRequest,
) -> dict[str, Any]:
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Memory 问题不能为空")
    runtime = get_research_runtime()
    try:
        runtime.get_memory(memory_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"Memory 不存在: {memory_id}") from exc
    answer = await runtime.answer_memory(memory_id, question)
    if (
        not isinstance(answer, MemoryAnswer)
        or answer.memory_id != memory_id
        or answer.question != question
    ):
        raise RuntimeError("Memory answer does not match the request")
    payload = _answer_response(runtime, answer)
    _MEMORY_ANSWERS[answer.answer_id] = answer
    return payload


@app.post("/api/memories/{memory_id}/note-proposals")
async def propose_memory_note(
    memory_id: str,
    req: MemoryNoteProposalRequest,
) -> dict[str, Any]:
    answer_id = (req.answer_id or "").strip()
    if not answer_id:
        raise HTTPException(status_code=400, detail="answer_id 不能为空")
    answer = _MEMORY_ANSWERS.get(answer_id)
    if answer is None:
        raise HTTPException(status_code=404, detail=f"Memory answer 不存在: {answer_id}")
    if answer.memory_id != memory_id:
        raise HTTPException(status_code=409, detail="answer 与 memory 不匹配")
    proposal = await get_research_runtime().propose_memory_note(answer)
    if (
        not isinstance(proposal, MemoryNoteProposal)
        or proposal.answer_id != answer.answer_id
        or proposal.memory_id != memory_id
    ):
        raise RuntimeError("Memory note proposal does not match the answer")
    _MEMORY_NOTE_PROPOSALS[proposal.proposal_id] = proposal
    return asdict(proposal)


@app.post("/api/memory-note-proposals/{proposal_id}/confirm")
async def confirm_memory_note(proposal_id: str) -> dict[str, Any]:
    proposal = _MEMORY_NOTE_PROPOSALS.get(proposal_id)
    if proposal is None:
        raise HTTPException(
            status_code=404,
            detail=f"Memory note proposal 不存在: {proposal_id}",
        )
    answer = _MEMORY_ANSWERS.get(proposal.answer_id)
    if answer is None:
        raise HTTPException(status_code=409, detail="proposal 的原始 answer 已不可用")
    if answer.answer_id != proposal.answer_id or answer.memory_id != proposal.memory_id:
        raise HTTPException(status_code=409, detail="proposal 与 answer 不匹配")
    try:
        result = get_research_runtime().commit_memory_note(proposal)
    except MemoryWriteConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = _commit_response(result)
    if (
        payload["memory_id"] != proposal.memory_id
        or payload["target_path"] != proposal.target_path
        or payload["home_path"] != proposal.home_path
        or payload["wikilink"] != proposal.wikilink
    ):
        raise RuntimeError("Memory note commit does not match the proposal")
    _MEMORY_NOTE_PROPOSALS.pop(proposal_id, None)
    return payload


@app.get("/api/sessions/{sid}/messages")
async def session_messages(sid: str) -> list[dict[str, Any]]:
    return _expanded_messages(sid)


@app.get("/api/sessions/{sid}/evidence")
async def session_evidence(sid: str) -> dict[str, Any]:
    pointer = _latest_report_pointer(sid)
    if pointer is None:
        return {"session_id": sid, "memory_id": None, "evidence": [], "sources": []}
    manifest, runtime = pointer["manifest"], get_research_runtime()

    def read_many(paths: Iterable[str]) -> list[dict[str, str]]:
        result = []
        for path in paths:
            try:
                result.append({"path": str(path), "markdown": runtime.read_memory(str(path))})
            except (OSError, ValueError):
                pass
        return result
    return {"session_id": sid, "memory_id": pointer.get("memory_id"),
            "evidence": read_many(manifest.get("evidence_paths", [])),
            "sources": read_many(manifest.get("source_paths", []))}


@app.patch("/api/sessions/{sid}")
async def session_rename(sid: str, req: SessionRename) -> dict[str, Any]:
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    get_chat_store().set_meta(sid, title=title)
    return {"session_id": sid, "title": title}


@app.post("/api/sessions/{sid}/pin")
async def session_pin(sid: str, req: SessionPin) -> dict[str, Any]:
    get_chat_store().set_meta(sid, pinned=bool(req.pinned))
    return {"session_id": sid, "pinned": bool(req.pinned)}


@app.post("/api/sessions/order")
async def session_order(req: SessionOrder) -> dict[str, Any]:
    get_chat_store().set_sort_order(req.session_ids)
    return {"ok": True, "count": len(req.session_ids)}


@app.delete("/api/sessions/{sid}")
async def session_delete(sid: str) -> dict[str, Any]:
    if _active_task(sid):
        raise HTTPException(status_code=409, detail="请等待当前研究结束后再删除会话")
    deleted = get_chat_store().delete_session(sid)
    for task_id, task in list(_TASKS.items()):
        if task.session_id == sid and task.status in {"done", "error"}:
            _TASKS.pop(task_id, None)
    return {"session_id": sid, "deleted": {"chat": deleted}}

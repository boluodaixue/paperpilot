#!/usr/bin/env python3
"""PaperPilot Web server backed only by the homogeneous Research Workflow."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

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
from src.conversation import (  # noqa: E402
    ActionOverride,
    ConversationMessage,
    ConversationRequest,
    MemorySelection,
    answer_quick_search,
    route_conversation,
)
from src.research.memory import MemoryWriteConflictError  # noqa: E402
from src.research.memory_import import MemoryImportLimitError  # noqa: E402
from src.research.models import (  # noqa: E402
    MemoryAnswer,
    MemoryDescriptor,
    MemoryImportDuplicate,
    MemoryImportProposal,
    MemoryNoteProposal,
    ResearchWorkflowResult,
)
from src.research.obsidian import build_obsidian_open_uri  # noqa: E402
from src.research.runtime import (  # noqa: E402
    ResearchRuntime,
    load_config,
    open_research_runtime,
)
from src.research.runtime_registry import RuntimeRegistry  # noqa: E402
from src.research.workflow_recovery import (  # noqa: E402
    derive_workflow_status,
    startup_reconciliation_action,
    terminal_retention_expired,
    workflow_outbox_events,
)
from src.research.vault import LEGACY_MEMORY_ID  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
_config = load_config()
_configured_chat_db = Path(str(_config.get("chat", {}).get("db_path", "data/chat.db")))
CHAT_DB_PATH = str(
    _configured_chat_db if _configured_chat_db.is_absolute()
    else PROJECT_ROOT / _configured_chat_db
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Chat owns ``session_meta``; initialize it before the registry installs
    # its foreign-keyed locator tables in the same SQLite file.
    get_chat_store()
    get_runtime_registry._registry = RuntimeRegistry(CHAT_DB_PATH)
    injected = getattr(get_research_runtime, "_runtime", None)
    if injected is not None:
        try:
            await _restore_registered_workflows()
            yield
        finally:
            await _stop_background_tasks()
            try:
                await injected.close(shutdown=True)
            except Exception as exc:
                print(f"[Runtime] 关闭失败: {exc}")
        return

    async with open_research_runtime(CHAT_DB_PATH, config=_config) as runtime:
        get_research_runtime._runtime = runtime
        stop_sweeper = asyncio.Event()
        sweeper: asyncio.Task[Any] | None = None
        try:
            await _restore_registered_workflows()
            sweeper = asyncio.create_task(_workflow_sweeper(stop_sweeper))
            yield
        finally:
            stop_sweeper.set()
            if sweeper is not None:
                sweeper.cancel()
                await asyncio.gather(sweeper, return_exceptions=True)
            await _stop_background_tasks()
            if getattr(get_research_runtime, "_runtime", None) is runtime:
                delattr(get_research_runtime, "_runtime")


app = FastAPI(title="PaperPilot", version="0.2.0", lifespan=_lifespan)


def get_chat_store() -> ChatStore:
    store = getattr(get_chat_store, "_store", None)
    if store is None:
        store = ChatStore(CHAT_DB_PATH)
        get_chat_store._store = store
    return store


def get_research_runtime() -> ResearchRuntime:
    """Return the lifespan-owned persistent runtime.

    Tests may inject an explicit Runtime before entering the lifespan.  Product
    code must never manufacture an in-memory fallback merely because it was
    called before application startup.
    """
    runtime = getattr(get_research_runtime, "_runtime", None)
    if runtime is None:
        raise RuntimeError("Research runtime is unavailable outside app lifespan")
    return runtime


def get_runtime_registry() -> RuntimeRegistry:
    registry = getattr(get_runtime_registry, "_registry", None)
    if registry is None:
        get_chat_store()
        registry = RuntimeRegistry(CHAT_DB_PATH)
        get_runtime_registry._registry = registry
    return registry


class ResearchTask:
    def __init__(
        self,
        task_id: str,
        session_id: str,
        query: str,
        memory_id: str | None = None,
        *,
        created_at: float | None = None,
        expires_at: float | None = None,
    ) -> None:
        self.task_id = self.thread_id = task_id
        self.session_id, self.query = session_id, query
        self.memory_id = memory_id
        self.status = "waiting_confirmation"
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.created_at = time.time() if created_at is None else created_at
        self.expires_at = expires_at
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
_BACKGROUND_TASKS: dict[str, asyncio.Task[Any]] = {}
_RUN_SEMAPHORE = asyncio.Semaphore(1)

_MAX_MEMORY_IMPORT_BYTES = 10 * 1024 * 1024
_MAX_MEMORY_IMPORT_BASE64_CHARS = ((_MAX_MEMORY_IMPORT_BYTES + 2) // 3) * 4


def _spawn_background(task_id: str, coroutine: Any) -> None:
    handle = asyncio.create_task(coroutine)
    _BACKGROUND_TASKS[task_id] = handle

    def forget(completed: asyncio.Task[Any]) -> None:
        if _BACKGROUND_TASKS.get(task_id) is completed:
            _BACKGROUND_TASKS.pop(task_id, None)

    handle.add_done_callback(forget)


async def _stop_background_tasks() -> None:
    """Stop live adapters before their checkpoint saver is closed."""
    handles = tuple(_BACKGROUND_TASKS.values())
    for handle in handles:
        handle.cancel()
    if handles:
        await asyncio.gather(*handles, return_exceptions=True)
    _BACKGROUND_TASKS.clear()


class AlignmentRequest(BaseModel):
    session_id: str | None = None
    task_id: str | None = None
    memory_id: str | None = None
    message: str


class ConversationRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    memory_id: str | None = None
    message: str
    explicit_action: ActionOverride = ActionOverride.AUTO


class QuickAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    memory_id: str | None = None
    question: str


class ResearchRequest(BaseModel):
    task_id: str
    session_id: str | None = None


class MemoryCreateRequest(BaseModel):
    title: str


class LegacyMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    target_memory_id: str
    session_id: str | None = None


class MemoryAnswerRequest(BaseModel):
    question: str
    session_id: str | None = None


class MemoryNoteProposalRequest(BaseModel):
    answer_id: str
    session_id: str | None = None


class MemoryImportProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["file", "text", "url"]
    session_id: str | None = None
    file_name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    content_base64: str | None = None
    title: str | None = None
    text: str | None = None
    url: str | None = None


class MemoryOperationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    memory_id: str | None = None
    answer_id: str | None = None
    proposal_id: str | None = None


class SessionRename(BaseModel):
    title: str


class SessionPin(BaseModel):
    pinned: bool


class SessionOrder(BaseModel):
    session_ids: list[str]


def _research_task_from_snapshot(
    record: Any,
    snapshot: Any,
    status: str | None = None,
) -> ResearchTask:
    """Rebuild the disposable Web adapter from its durable locator and State."""
    if record.workflow_type != "research":
        raise ValueError("workflow is not a research task")
    resolved = status or derive_workflow_status(record, snapshot)
    if resolved in {"missing", "orphan"}:
        raise ValueError("research task has no recoverable checkpoint")
    values = dict(snapshot.values)
    task = ResearchTask(
        record.task_id,
        record.session_id,
        str(values.get("question") or ""),
        record.memory_id,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )
    task.status = (
        "waiting_confirmation"
        if resolved == "waiting_confirmation"
        else "running"
        if resolved == "running"
        else "done"
        if resolved == "completed"
        else "error"
    )
    if task.status == "error":
        task.error = str(values.get("failure_code") or resolved)
    workflow_result = values.get("workflow_result")
    if task.status == "done" and isinstance(
        workflow_result, ResearchWorkflowResult
    ):
        task.result = _research_task_result(task, workflow_result, elapsed=0)
    return task


async def _get_task(task_id: str) -> ResearchTask:
    """Return a local adapter, rebuilding it on a cross-worker cache miss."""
    task = _TASKS.get(task_id)
    if task is not None:
        return task
    record = get_runtime_registry().get(task_id)
    if record is None or record.workflow_type != "research":
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    try:
        snapshot = await get_research_runtime().get_workflow_snapshot(
            "research", record.thread_id
        )
        status = derive_workflow_status(record, snapshot)
        task = _research_task_from_snapshot(record, snapshot, status)
        _reconcile_workflow_outbox(record, snapshot, status)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"任务状态暂时无法恢复: {task_id}",
        ) from exc
    _TASKS[task_id] = task
    return task


def _active_task(session_id: str) -> ResearchTask | None:
    matches = [
        task for task in _TASKS.values()
        if task.session_id == session_id
        and task.status in {"waiting_confirmation", "running"}
    ]
    return max(matches, key=lambda item: item.created_at) if matches else None


def _latest_task(session_id: str) -> ResearchTask | None:
    matches = [task for task in _TASKS.values() if task.session_id == session_id]
    return max(matches, key=lambda item: item.created_at) if matches else None


async def _session_research_task(
    session_id: str,
    *,
    active_only: bool,
) -> ResearchTask | None:
    """Find a session task without treating this worker's cache as authority."""
    records = [
        record
        for record in get_runtime_registry().list(session_id=session_id)
        if record.workflow_type == "research"
    ]
    for record in reversed(records):
        try:
            task = await _get_task(record.task_id)
            status = await _authoritative_research_status(task)
        except (HTTPException, ValueError, RuntimeError):
            continue
        if not active_only or status in {"waiting_confirmation", "running"}:
            task.status = status
            return task
    return None


async def _authoritative_research_status(task: ResearchTask) -> str:
    record = get_runtime_registry().get(task.task_id)
    runtime = get_research_runtime()
    if record is None:
        raise RuntimeError("research task has no Runtime Registry locator")
    snapshot = await runtime.get_workflow_snapshot("research", task.thread_id)
    status = derive_workflow_status(record, snapshot)
    return (
        "waiting_confirmation"
        if status == "waiting_confirmation"
        else "running"
        if status == "running"
        else "done"
        if status == "completed"
        else "error"
    )


def _session_id(value: str | None) -> str:
    return (value or "").strip() or f"web-{uuid.uuid4().hex[:8]}"


def _session_memory(
    session_id: str,
    requested_memory_id: str | None,
) -> str | None:
    """Return one durable session binding and reject every later switch."""
    store = get_chat_store()
    existing = store.get_memory_binding(session_id)
    if existing is not None:
        if requested_memory_id is not None and requested_memory_id != existing:
            raise HTTPException(
                status_code=409,
                detail="该会话已绑定到另一个 Memory",
            )
        _validate_memory_option(existing)
        return existing

    if requested_memory_id is None:
        # A W6-capable production Runtime exposes Memory options and must never
        # turn an omitted selection into a writable legacy-root session.  The
        # fallback only keeps older Runtime adapters usable by pre-W6 callers.
        if hasattr(get_research_runtime(), "get_memory_option"):
            raise HTTPException(
                status_code=400,
                detail="请先明确选择一个 managed Memory",
            )
        return None
    if requested_memory_id is not None:
        _validate_memory_option(requested_memory_id)
    try:
        return store.bind_memory(session_id, requested_memory_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _optional_session_memory(
    session_id: str,
    requested_memory_id: str | None,
) -> str | None:
    """Allow casual chat unbound while preserving one-Memory session identity."""

    store = get_chat_store()
    existing = store.get_memory_binding(session_id)
    if existing is not None:
        if requested_memory_id is not None and requested_memory_id != existing:
            raise HTTPException(
                status_code=409,
                detail="该会话已绑定到另一个 Memory",
            )
        _validate_memory_option(existing)
        return existing
    if requested_memory_id is None:
        store.ensure_session(session_id)
        return None
    _validate_memory_option(requested_memory_id)
    try:
        return store.bind_memory(session_id, requested_memory_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _validate_memory_option(memory_id: str) -> None:
    runtime = get_research_runtime()
    try:
        if hasattr(runtime, "get_memory_option"):
            runtime.get_memory_option(memory_id)
        elif memory_id != LEGACY_MEMORY_ID:
            runtime.get_memory(memory_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Memory 不存在或不可读取: {memory_id}",
        ) from exc


def _require_writable_memory(memory_id: str | None) -> str:
    if memory_id is None:
        raise HTTPException(status_code=400, detail="请先明确选择一个 Memory")
    if memory_id == LEGACY_MEMORY_ID:
        raise HTTPException(
            status_code=409,
            detail="M-legacy 为只读；请先显式迁移或选择 managed Memory",
        )
    return memory_id


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
        },
        ensure_ascii=False,
    )


def _automatic_memory_title(task: ResearchTask, state: Mapping[str, Any]) -> str:
    brief = state.get("brief")
    objective = getattr(brief, "objective", "")
    title = " ".join(str(objective or task.query or "新研究").split())
    return title[:120] or "新研究"


async def _ensure_research_memory(
    task: ResearchTask,
    lease_token: str,
) -> tuple[MemoryDescriptor, bool]:
    """Create and bind the deterministic managed Memory for an unbound proposal."""

    runtime = get_research_runtime()
    registry = get_runtime_registry()
    if task.memory_id is not None:
        return runtime.get_memory(task.memory_id), False

    state = await runtime.get_state(task.thread_id)
    suffix = task.task_id.rsplit("-", 1)[-1]
    memory_id = f"M-{suffix}"
    title = _automatic_memory_title(task, state)
    try:
        descriptor = runtime.create_memory(title, memory_id=memory_id)
        created = True
    except FileExistsError:
        descriptor = runtime.get_memory(memory_id)
        created = False

    await runtime.bind_research_memory(
        task.thread_id,
        descriptor.memory_id,
        session_id=task.session_id,
    )
    record = registry.bind_memory(
        task.task_id,
        descriptor.memory_id,
        lease_token=lease_token,
    )
    task.memory_id = record.memory_id
    return descriptor, created


def _report_pointer(task: ResearchTask, result: ResearchWorkflowResult) -> dict[str, Any]:
    research_result = result.research_result
    return {
        "task_id": task.task_id,
        "thread_id": task.thread_id,
        "memory_id": result.memory_id,
        "manifest": asdict(result.memory_manifest),
        "research_status": research_result.status.value,
        "termination_reason": (
            research_result.termination_reason.value
            if research_result.termination_reason is not None
            else None
        ),
        "output_status": research_result.output_status.value,
        "stop_reason": research_result.stop_reason,
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
        "read_only": False,
        "can_migrate": False,
        "file_count": None,
        "home_relative_path": home_relative_path,
        "home_absolute_path": str(home_absolute_path),
        "obsidian_uri": build_obsidian_open_uri(
            vault_root,
            home_relative_path,
            vault_name=_configured_vault_name(),
        ),
    }


def _memory_option_response(
    runtime: ResearchRuntime,
    option: Mapping[str, Any],
) -> dict[str, Any]:
    if option.get("memory_id") != LEGACY_MEMORY_ID:
        return _memory_response(runtime, runtime.get_memory(str(option["memory_id"])))
    return {
        **dict(option),
        "home_relative_path": None,
        "home_absolute_path": None,
        "obsidian_uri": None,
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


_MEMORY_WORKFLOW_TERMINAL = frozenset(
    {"committed", "duplicate", "cancelled", "expired", "failed"}
)


class WorkflowLeaseLostError(RuntimeError):
    """Raised when this executor no longer owns a workflow lease."""


class _LeaseGuard:
    def __init__(self, record: Any, token: str, lease_seconds: float) -> None:
        self.record = record
        self.token = token
        self.lease_seconds = lease_seconds

    async def verify(self) -> None:
        if not get_runtime_registry().renew_lease(
            self.record.task_id,
            self.token,
            lease_seconds=self.lease_seconds,
        ):
            raise WorkflowLeaseLostError(
                f"workflow lease lost: {self.record.thread_id}"
            )


@asynccontextmanager
async def _workflow_lease(record: Any, token: str, lease_seconds: float):
    """Renew one execution lease and cancel local work if ownership is lost."""
    guard = _LeaseGuard(record, token, lease_seconds)
    await guard.verify()
    owner = asyncio.current_task()
    lost = asyncio.Event()

    async def heartbeat() -> None:
        interval = max(0.05, min(5.0, lease_seconds / 3.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await guard.verify()
            except Exception:
                lost.set()
                if owner is not None:
                    owner.cancel()
                return

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        yield guard
    except asyncio.CancelledError:
        if lost.is_set():
            raise WorkflowLeaseLostError(
                f"workflow lease lost: {record.thread_id}"
            ) from None
        raise
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        get_runtime_registry().release_lease(record.task_id, token)


def _workflow_response_identity(record: Any) -> dict[str, Any]:
    return {
        "workflow_id": record.thread_id,
        "task_id": record.task_id,
        "thread_id": record.thread_id,
        "expires_at": record.expires_at,
    }


def _workflow_value_identity(value: Any, field_name: str) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get(field_name)
    else:
        candidate = getattr(value, field_name, None)
    return candidate if isinstance(candidate, str) and candidate else None


def _validate_workflow_snapshot(record: Any, values: Mapping[str, Any]) -> None:
    expected = {
        "thread_id": record.thread_id,
        "session_id": record.session_id,
        "memory_id": record.memory_id,
        "workflow_type": record.workflow_type,
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Runtime Registry 与 workflow checkpoint 身份不一致")
    expires_at = values.get("expires_at")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        raise RuntimeError("workflow checkpoint 缺少有效 expires_at")
    if record.expires_at is None or float(expires_at) != record.expires_at:
        raise RuntimeError("Runtime Registry 与 workflow checkpoint TTL 不一致")


async def _settle_workflow_start_failure(record: Any, code: str) -> None:
    """Close a checkpointed start failure or remove only an empty locator."""
    runtime = get_research_runtime()
    registry = get_runtime_registry()
    try:
        snapshot = await runtime.get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        values = dict(snapshot.values)
        if not values:
            registry.delete(record.task_id)
            return
        _validate_workflow_snapshot(record, values)
        await runtime.mark_workflow_failed(
            record.workflow_type, record.thread_id, code
        )
        failed_snapshot = await runtime.get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        _reconcile_workflow_outbox(record, failed_snapshot)
    except Exception as exc:
        # Identity disagreement is quarantined by deleting only the locator;
        # the authoritative checkpoint is never destroyed on this path.
        print(f"[Runtime] 工作流启动失败收口异常，隔离 locator: {exc}")
        registry.delete(record.task_id)


async def _find_memory_workflow(
    workflow_type: str,
    *,
    value_key: str,
    identity_key: str,
    identity_value: str,
) -> tuple[Any, dict[str, Any]]:
    runtime = get_research_runtime()
    matches: list[tuple[Any, dict[str, Any]]] = []
    for record in get_runtime_registry().list():
        if record.workflow_type != workflow_type:
            continue
        snapshot = await runtime.get_workflow_snapshot(
            workflow_type,
            record.thread_id,
        )
        values = dict(snapshot.values)
        if not values:
            continue
        try:
            _validate_workflow_snapshot(record, values)
        except RuntimeError as exc:
            print(f"[Runtime] 隔离身份不一致的 workflow locator: {exc}")
            get_runtime_registry().delete(record.task_id)
            continue
        if _workflow_value_identity(values.get(value_key), identity_key) == identity_value:
            matches.append((record, values))
    if not matches:
        raise HTTPException(
            status_code=404,
            detail=f"Memory workflow 对象不存在: {identity_value}",
        )
    if len(matches) != 1:
        raise HTTPException(
            status_code=409,
            detail=f"Memory workflow 对象身份不唯一: {identity_value}",
        )
    return matches[0]


def _validate_optional_decision_identity(
    req: MemoryOperationDecisionRequest | None,
    record: Any,
    *,
    proposal_id: str | None = None,
    answer_id: str | None = None,
) -> None:
    if req is None:
        return
    checks = (
        (req.session_id, record.session_id, "session"),
        (req.memory_id, record.memory_id, "memory"),
        (req.proposal_id, proposal_id, "proposal"),
        (req.answer_id, answer_id, "answer"),
    )
    for supplied, expected, label in checks:
        if supplied is not None and supplied.strip() != expected:
            raise HTTPException(status_code=409, detail=f"{label} 身份不匹配")


def _claim_memory_workflow(record: Any) -> str:
    token = get_runtime_registry().claim_lease(
        record.task_id,
        lease_seconds=get_research_runtime().lease_seconds,
    )
    if token is None:
        raise HTTPException(status_code=409, detail="该 Memory 操作正在被处理")
    return token


def _reconcile_workflow_outbox(
    record: Any,
    snapshot: Any,
    status: str | None = None,
) -> None:
    """Idempotently derive durable adapter events from authoritative State."""
    registry = get_runtime_registry()
    for event_type, payload in workflow_outbox_events(record, snapshot, status):
        registry.append_event(record.thread_id, event_type, payload)


async def _settle_execution_failure(record: Any, code: str) -> str | None:
    """Mark only non-terminal graph work failed; never overwrite a terminal State."""
    runtime = get_research_runtime()
    try:
        snapshot = await runtime.get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        status = derive_workflow_status(record, snapshot)
        if status not in {"completed", "failed", "cancelled", "expired"}:
            await runtime.mark_workflow_failed(
                record.workflow_type, record.thread_id, code
            )
            snapshot = await runtime.get_workflow_snapshot(
                record.workflow_type, record.thread_id
            )
            status = derive_workflow_status(record, snapshot)
        _reconcile_workflow_outbox(record, snapshot, status)
        return status
    except Exception as state_exc:
        print(f"[Runtime] 执行失败状态暂未收口: {state_exc}")
        return None


def _raise_for_workflow_status(status: str) -> None:
    if status == "expired":
        raise HTTPException(status_code=410, detail="Memory 操作已过期")
    if status == "cancelled":
        raise HTTPException(status_code=409, detail="Memory 操作已取消")
    if status in {"committed", "duplicate"}:
        raise HTTPException(status_code=409, detail="Memory 操作已经完成")
    if status == "failed":
        raise HTTPException(status_code=409, detail="Memory 操作已经失败")


async def _resume_registered_memory_workflow(
    record: Any,
    values: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    status = str(values.get("workflow_status") or "")
    if status in _MEMORY_WORKFLOW_TERMINAL:
        snapshot = await get_research_runtime().get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        _reconcile_workflow_outbox(record, snapshot)
        _raise_for_workflow_status(status)
    token = _claim_memory_workflow(record)
    try:
        async with _workflow_lease(
            record, token, get_research_runtime().lease_seconds
        ) as lease:
            latest = await get_research_runtime().get_workflow_snapshot(
                record.workflow_type,
                record.thread_id,
            )
            latest_values = dict(latest.values)
            _validate_workflow_snapshot(record, latest_values)
            latest_status = str(latest_values.get("workflow_status") or "")
            if latest_status in _MEMORY_WORKFLOW_TERMINAL:
                _reconcile_workflow_outbox(record, latest)
                _raise_for_workflow_status(latest_status)
            result = await get_research_runtime().resume_memory_operation(
                record.workflow_type,
                record.thread_id,
                decision,
            )
            await lease.verify()
            result_status = str(result.get("workflow_status") or "")
            if result_status in _MEMORY_WORKFLOW_TERMINAL:
                final_snapshot = await get_research_runtime().get_workflow_snapshot(
                    record.workflow_type, record.thread_id
                )
                _reconcile_workflow_outbox(record, final_snapshot)
            return result
    except HTTPException:
        raise
    except (MemoryWriteConflictError, FileExistsError) as exc:
        await _settle_execution_failure(record, "memory_write_conflict")
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _import_wikilink_for_path(wikilinks: Iterable[str], path: str | None) -> str | None:
    if not path:
        return None
    target = path[:-3] if path.endswith(".md") else path
    for wikilink in wikilinks:
        if not isinstance(wikilink, str) or not wikilink.startswith("[["):
            continue
        link_target = wikilink[2:-2].split("|", 1)[0]
        if link_target == target:
            return wikilink
    return None


def _add_import_obsidian_uris(
    runtime: ResearchRuntime,
    payload: dict[str, Any],
) -> None:
    vault_name = _configured_vault_name()
    for field in ("attachment_path", "import_path", "note_path", "home_path"):
        path = payload.get(field)
        if not path:
            continue
        payload[field.replace("_path", "_obsidian_uri")] = build_obsidian_open_uri(
            runtime.memory_store.root,
            path,
            vault_name=vault_name,
        )


def _import_preview_response(
    runtime: ResearchRuntime,
    value: MemoryImportProposal | MemoryImportDuplicate,
) -> dict[str, Any]:
    payload = asdict(value)
    payload.pop("attachment_bytes", None)
    if isinstance(value, MemoryImportProposal):
        payload.update({"status": "proposed", "can_confirm": True})
    else:
        payload.update({"status": "duplicate", "can_confirm": False})
        payload["import_wikilink"] = _import_wikilink_for_path(
            value.wikilinks,
            value.import_path,
        )
        payload["note_wikilink"] = _import_wikilink_for_path(
            value.wikilinks,
            value.note_path,
        )
    _add_import_obsidian_uris(runtime, payload)
    return payload


def _memory_import_commit_response(
    runtime: ResearchRuntime,
    value: Any,
) -> dict[str, Any]:
    if isinstance(value, MemoryImportDuplicate):
        return _import_preview_response(runtime, value)
    if isinstance(value, Mapping):
        payload = dict(value)
    elif is_dataclass(value):
        payload = asdict(value)
    else:
        raise RuntimeError("Memory import commit returned an invalid result")
    payload.pop("attachment_bytes", None)
    required = {
        "status", "memory_id", "attachment_path", "import_path",
        "note_path", "home_path", "wikilinks",
    }
    if (
        not required.issubset(payload)
        or payload.get("status") not in {"committed", "duplicate"}
        or not isinstance(payload.get("memory_id"), str)
    ):
        raise RuntimeError("Memory import commit returned an invalid result")
    payload["can_confirm"] = False
    _add_import_obsidian_uris(runtime, payload)
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
            for key in (
                "research_status",
                "termination_reason",
                "output_status",
                "stop_reason",
            ):
                message[key] = pointer.get(key)
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
    confirmed_published = False
    async for update in runtime.stream_confirm(task.thread_id):
        if not confirmed_published:
            current = await runtime.get_state(task.thread_id)
            if current.get("confirmed") is True:
                get_runtime_registry().append_event(task.thread_id, "confirmed")
                await task.publish({
                    "type": "confirmed", "thread_id": task.thread_id,
                    "parent_thread_id": None, "root_thread_id": task.thread_id,
                    "memory_id": task.memory_id,
                })
                confirmed_published = True
        for events in _event_lists(update):
            await task.publish_execution_events(events)
    state = await runtime.get_state(task.thread_id)
    if not confirmed_published and state.get("confirmed") is True:
        get_runtime_registry().append_event(task.thread_id, "confirmed")
        await task.publish({
            "type": "confirmed", "thread_id": task.thread_id,
            "parent_thread_id": None, "root_thread_id": task.thread_id,
            "memory_id": task.memory_id,
        })
    await task.publish_execution_events(state.get("execution_events", []))
    return state


def _research_task_result(
    task: ResearchTask,
    workflow_result: ResearchWorkflowResult,
    *,
    elapsed: float,
) -> dict[str, Any]:
    research_result = workflow_result.research_result
    return {
        "task_id": task.task_id,
        "thread_id": task.thread_id,
        "session_id": task.session_id,
        "memory_id": workflow_result.memory_id,
        "query": task.query,
        "elapsed": round(elapsed, 1),
        "research_status": research_result.status.value,
        "termination_reason": (
            research_result.termination_reason.value
            if research_result.termination_reason is not None
            else None
        ),
        "output_status": research_result.output_status.value,
        "stop_reason": research_result.stop_reason,
        "tool_alerts": [asdict(item) for item in research_result.tool_alerts],
        "report_md": workflow_result.report_markdown,
        "evidence": [asdict(item) for item in research_result.evidence],
        "manifest": asdict(workflow_result.memory_manifest),
        "research_architecture": workflow_result.research_architecture,
        "challenges": list(workflow_result.challenges),
        "citation_issues": list(workflow_result.citation_issues),
        "supplemental_wave_count": workflow_result.supplemental_wave_count,
        "finalization_token_reserve": workflow_result.finalization_token_reserve,
    }


def _ensure_report_pointer(
    task: ResearchTask, workflow_result: ResearchWorkflowResult
) -> None:
    store = get_chat_store()
    for message in store.get_messages(task.session_id):
        if message.get("kind") != KIND_REPORT:
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and payload.get("task_id") == task.task_id:
            return
    store.add(
        task.session_id,
        "assistant",
        KIND_REPORT,
        json.dumps(_report_pointer(task, workflow_result), ensure_ascii=False),
    )


async def _run_research_task(
    task: ResearchTask,
    *,
    resume_existing: bool = False,
    lease_token: str | None = None,
) -> None:
    async def execute(lease: _LeaseGuard | None) -> None:
        async with _RUN_SEMAPHORE:
            started = time.time()
            if resume_existing:
                state = await get_research_runtime().continue_research(task.thread_id)
                await task.publish_execution_events(state.get("execution_events", []))
            else:
                state = await _stream_confirm(task)
            workflow_result = state.get("workflow_result")
            if not isinstance(workflow_result, ResearchWorkflowResult):
                raise RuntimeError("workflow ended without a structured result")
            if lease is not None:
                await lease.verify()
            task.result = _research_task_result(
                task, workflow_result, elapsed=time.time() - started
            )
            try:
                _ensure_report_pointer(task, workflow_result)
            except Exception as exc:
                print(f"[Chat] 报告引用落盘失败: {exc}")
            if lease is not None:
                await lease.verify()
            task.status = "done"
            get_runtime_registry().append_event(task.thread_id, "completed")
            await task.publish({
                "type": "done", "thread_id": task.thread_id,
                "parent_thread_id": None, "root_thread_id": task.thread_id,
                "session_id": task.session_id, "memory_id": workflow_result.memory_id,
            })

    try:
        if lease_token is None:
            await execute(None)
        else:
            record = get_runtime_registry().get(task.task_id)
            if record is None:
                raise WorkflowLeaseLostError(
                    f"workflow locator missing: {task.thread_id}"
                )
            async with _workflow_lease(
                record, lease_token, get_research_runtime().lease_seconds
            ) as lease:
                await execute(lease)
    except WorkflowLeaseLostError as exc:
        print(f"[Runtime] 停止失去租约的本地执行器: {exc}")
    except Exception as exc:
        record = get_runtime_registry().get(task.task_id)
        status = (
            await _settle_execution_failure(record, type(exc).__name__.lower())
            if record is not None
            else None
        )
        if status == "completed":
            snapshot = await get_research_runtime().get_workflow_snapshot(
                "research", task.thread_id
            )
            workflow_result = snapshot.values.get("workflow_result")
            if isinstance(workflow_result, ResearchWorkflowResult):
                task.result = _research_task_result(task, workflow_result, elapsed=0)
            task.status = "done"
            return
        task.status, task.error = "error", str(exc)
        try:
            await task.publish({
                "type": "error", "message": str(exc)[:500],
                "thread_id": task.thread_id, "parent_thread_id": None,
                "root_thread_id": task.thread_id, "memory_id": task.memory_id,
            })
        except Exception as publish_exc:
            print(f"[Runtime] 临时错误事件发送失败: {publish_exc}")


def _interrupt_decision(
    record: Any,
    snapshot: Any,
    *,
    action: str,
) -> dict[str, Any]:
    interrupts = getattr(snapshot, "interrupts", ())
    if len(interrupts) != 1 or not isinstance(interrupts[0].value, Mapping):
        raise ValueError("waiting workflow has no single recoverable interrupt")
    payload = interrupts[0].value
    decision: dict[str, Any] = {
        "action": action,
        "session_id": record.session_id,
        "memory_id": record.memory_id,
    }
    kind = payload.get("kind")
    if kind == "memory_answer_decision":
        decision["answer_id"] = payload["answer"]["answer_id"]
    elif kind in {"memory_note_confirmation", "memory_import_confirmation"}:
        decision["proposal_id"] = payload["proposal"]["proposal_id"]
    elif kind == "legacy_migration_confirmation":
        decision["proposal_id"] = payload["proposal"]["proposal_id"]
    else:
        raise ValueError("waiting workflow interrupt kind is not recoverable")
    return decision


async def _continue_registered_memory_workflow(record: Any, lease: str) -> None:
    try:
        async with _workflow_lease(
            record, lease, get_research_runtime().lease_seconds
        ) as guard:
            state = await get_research_runtime().continue_workflow(
                record.workflow_type, record.thread_id
            )
            await guard.verify()
            status = str(state.get("workflow_status") or "")
            if status in _MEMORY_WORKFLOW_TERMINAL:
                snapshot = await get_research_runtime().get_workflow_snapshot(
                    record.workflow_type, record.thread_id
                )
                _reconcile_workflow_outbox(record, snapshot)
    except WorkflowLeaseLostError as exc:
        print(f"[Runtime] 停止失去租约的 Memory 执行器: {exc}")
    except Exception as exc:
        await _settle_execution_failure(record, type(exc).__name__.lower())


async def _restore_registered_workflows() -> None:
    """Reconcile Registry locators against authoritative checkpoints at startup."""
    runtime = get_research_runtime()
    registry = get_runtime_registry()
    now = time.time()
    for record in registry.list():
        try:
            snapshot = await runtime.get_workflow_snapshot(
                record.workflow_type, record.thread_id
            )
        except Exception as exc:
            # Saver availability/deserialization failures are not evidence that
            # the durable locator is wrong. Keep it for a later sweep/restart.
            print(f"[Runtime] checkpoint 暂时无法读取，保留 locator: {exc}")
            continue
        try:
            action = startup_reconciliation_action(record, snapshot, now=now)
            status = derive_workflow_status(record, snapshot)
        except Exception as exc:
            print(f"[Runtime] 工作流身份校验失败并移除 locator: {exc}")
            registry.delete(record.task_id)
            continue

        if action == "delete_orphan":
            registry.delete(record.task_id)
            continue
        if action == "expire_waiting":
            try:
                if await _expire_registered_workflow(record, snapshot):
                    status = "expired"
            except Exception as exc:
                print(f"[Runtime] 工作流过期收口失败: {exc}")

        try:
            current_snapshot = await runtime.get_workflow_snapshot(
                record.workflow_type, record.thread_id
            )
            current_status = derive_workflow_status(record, current_snapshot)
            _reconcile_workflow_outbox(record, current_snapshot, current_status)
            snapshot, status = current_snapshot, current_status
        except Exception as exc:
            print(f"[Runtime] outbox 对账暂缓: {exc}")

        if record.workflow_type == "research":
            try:
                task = _research_task_from_snapshot(record, snapshot, status)
            except ValueError as exc:
                print(f"[Runtime] research adapter 无法恢复: {exc}")
                continue
            workflow_result = snapshot.values.get("workflow_result")
            if task.status == "done" and isinstance(
                workflow_result, ResearchWorkflowResult
            ):
                try:
                    _ensure_report_pointer(task, workflow_result)
                except Exception as exc:
                    print(f"[Chat] 恢复报告引用失败: {exc}")
            _TASKS[task.task_id] = task
            if action == "resume_running":
                lease = registry.claim_lease(
                    task.task_id, lease_seconds=runtime.lease_seconds, now=now
                )
                if lease is not None:
                    _spawn_background(
                        task.task_id,
                        _run_research_task(
                            task, resume_existing=True, lease_token=lease
                        ),
                    )
        elif action == "resume_running":
            lease = registry.claim_lease(
                record.task_id, lease_seconds=runtime.lease_seconds, now=now
            )
            if lease is not None:
                _spawn_background(
                    record.task_id,
                    _continue_registered_memory_workflow(record, lease),
                )


async def _expire_registered_workflow(record: Any, snapshot: Any) -> bool:
    runtime = get_research_runtime()
    registry = get_runtime_registry()
    lease = registry.claim_lease(
        record.task_id, lease_seconds=runtime.lease_seconds
    )
    if lease is None:
        return False
    async with _workflow_lease(record, lease, runtime.lease_seconds) as guard:
        latest = await runtime.get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        if derive_workflow_status(record, latest) != "waiting_confirmation":
            return False
        if record.workflow_type == "research":
            await runtime.review(
                record.thread_id,
                "expire",
                session_id=record.session_id,
                memory_id=record.memory_id,
            )
            task = _TASKS.get(record.task_id)
            if task is not None:
                task.status = "error"
                task.error = "confirmation expired"
        else:
            await runtime.resume_memory_operation(
                record.workflow_type,
                record.thread_id,
                _interrupt_decision(record, latest, action="expire"),
            )
        await guard.verify()
        registry.append_event(record.thread_id, "expired")
        return True


def _snapshot_timestamp(snapshot: Any) -> float:
    value = getattr(snapshot, "created_at", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    raise ValueError("workflow checkpoint has no terminal timestamp")


async def _workflow_sweeper(stop: asyncio.Event) -> None:
    runtime = get_research_runtime()
    registry = get_runtime_registry()
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=runtime.sweep_interval_seconds
            )
            continue
        except asyncio.TimeoutError:
            pass
        now = time.time()
        for record in registry.list():
            try:
                snapshot = await runtime.get_workflow_snapshot(
                    record.workflow_type, record.thread_id
                )
                status = derive_workflow_status(record, snapshot)
                if (
                    status == "waiting_confirmation"
                    and record.expires_at is not None
                    and now >= record.expires_at
                ):
                    await _expire_registered_workflow(record, snapshot)
                elif status == "running" and record.task_id not in _BACKGROUND_TASKS:
                    task: ResearchTask | None = None
                    if record.workflow_type == "research":
                        # Construct the local executor before claiming.  A worker
                        # with an empty cache must not strand a durable lease.
                        task = _TASKS.get(record.task_id)
                        if task is None:
                            task = _research_task_from_snapshot(
                                record, snapshot, status
                            )
                            _TASKS[record.task_id] = task
                    lease = registry.claim_lease(
                        record.task_id,
                        lease_seconds=runtime.lease_seconds,
                        now=now,
                    )
                    if lease is not None:
                        if record.workflow_type == "research":
                            assert task is not None
                            _spawn_background(
                                record.task_id,
                                _run_research_task(
                                    task,
                                    resume_existing=True,
                                    lease_token=lease,
                                ),
                            )
                        else:
                            _spawn_background(
                                record.task_id,
                                _continue_registered_memory_workflow(record, lease),
                            )
                elif (
                    status in {"completed", "failed", "cancelled", "expired"}
                    and terminal_retention_expired(
                        status,
                        terminal_at=_snapshot_timestamp(snapshot),
                        now=now,
                        retention_seconds=runtime.terminal_retention_seconds,
                    )
                ):
                    await runtime.delete_workflow(record.thread_id)
                    registry.delete(record.task_id)
                    _TASKS.pop(record.task_id, None)
            except Exception as exc:
                print(f"[Runtime] 工作流清理失败: {exc}")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    path = STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=500, detail="前端页面缺失")
    return path.read_text(encoding="utf-8")


@app.post("/api/conversation/route")
async def route_product_conversation(
    req: ConversationRouteRequest,
) -> dict[str, Any]:
    """Route one product message without starting research or writing Memory."""

    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")
    session_id = _session_id(req.session_id)
    memory_id = _optional_session_memory(session_id, req.memory_id)
    runtime = get_research_runtime()
    selection = None
    if memory_id is not None:
        option = runtime.get_memory_option(memory_id)
        selection = MemorySelection(
            memory_id=memory_id,
            title=str(option.get("title") or memory_id),
            read_only=bool(option.get("read_only", False)),
        )
    recent_messages = tuple(
        ConversationMessage(
            role=str(item["role"]),
            content=str(item["content"]),
        )
        for item in get_chat_store().get_messages(session_id)[-8:]
        if item.get("kind") == "chat"
        and item.get("role") in {"user", "assistant"}
        and str(item.get("content") or "").strip()
    )
    try:
        decision = await route_conversation(
            ConversationRequest(
                message=message,
                recent_messages=recent_messages,
                selected_memory=selection,
                explicit_action=req.explicit_action,
            ),
            runtime.policy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"对话路由失败: {exc}") from exc

    if decision.action.value in {"reply", "clarify"}:
        store = get_chat_store()
        store.add(session_id, "user", "chat", message)
        store.add(session_id, "assistant", "chat", decision.response)
    return {
        "session_id": session_id,
        "memory_id": memory_id,
        "action": decision.action.value,
        "confidence": decision.confidence,
        "response": decision.response,
        "query": decision.query,
        "reason_code": decision.reason_code,
        "requires_memory": decision.requires_memory,
        "requires_confirmation": decision.requires_confirmation,
    }


@app.post("/api/conversation/quick-answer")
async def answer_product_quick_search(
    req: QuickAnswerRequest,
) -> dict[str, Any]:
    """Return one bounded cited Web answer without starting Research Core."""

    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="联网问题不能为空")
    session_id = _session_id(req.session_id)
    memory_id = _optional_session_memory(session_id, req.memory_id)
    runtime = get_research_runtime()
    acquisition = next(
        (
            tool
            for tool in runtime.tools
            if str(getattr(tool, "name", "")) == "acquire_evidence"
        ),
        None,
    )
    if acquisition is None:
        raise HTTPException(status_code=503, detail="快速联网能力当前不可用")
    try:
        answer = await answer_quick_search(acquisition, runtime.policy, question)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"快速联网回答失败: {exc}") from exc

    store = get_chat_store()
    store.add(session_id, "user", "chat", question)
    store.add(session_id, "assistant", "chat", answer.markdown)
    return {
        "session_id": session_id,
        "memory_id": memory_id,
        "answer_id": answer.answer_id,
        "question": answer.question,
        "markdown": answer.markdown,
        "citations": [asdict(item) for item in answer.citations],
        "insufficient_evidence": list(answer.insufficient_evidence),
        "source_count": len(answer.citations),
    }


@app.post("/api/alignment")
async def align_research(req: AlignmentRequest) -> dict[str, Any]:
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")
    runtime, store = get_research_runtime(), get_chat_store()
    if req.task_id:
        task = await _get_task(req.task_id)
        if req.session_id and req.session_id != task.session_id:
            raise HTTPException(status_code=409, detail="task 与 session 不匹配")
        if req.memory_id is not None and req.memory_id != task.memory_id:
            raise HTTPException(status_code=409, detail="task 与 memory 不匹配")
        if await _authoritative_research_status(task) != "waiting_confirmation":
            raise HTTPException(status_code=409, detail="任务当前不等待方案修改")
        if task.memory_id is not None:
            _require_writable_memory(task.memory_id)
        bound_memory_id = _optional_session_memory(task.session_id, task.memory_id)
        if bound_memory_id != task.memory_id:
            raise HTTPException(status_code=409, detail="task 与 session Memory 不匹配")
        store.add(task.session_id, "user", "chat", message)
        lease = get_runtime_registry().claim_lease(
            task.task_id, lease_seconds=getattr(runtime, "lease_seconds", 60)
        )
        if lease is None:
            raise HTTPException(status_code=409, detail="该方案正在被另一请求处理")
        record = get_runtime_registry().get(task.task_id)
        if record is None:
            get_runtime_registry().release_lease(task.task_id, lease)
            raise HTTPException(status_code=409, detail="任务 locator 已不可用")
        try:
            async with _workflow_lease(
                record, lease, runtime.lease_seconds
            ) as guard:
                state = await runtime.review(
                    task.thread_id,
                    "modify",
                    feedback=message,
                    session_id=task.session_id,
                    memory_id=task.memory_id,
                )
                await guard.verify()
        except WorkflowLeaseLostError as exc:
            raise HTTPException(status_code=409, detail="方案修改租约已失效") from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"方案修改失败: {exc}") from exc
        if state.get("workflow_status") == "expired":
            get_runtime_registry().append_event(task.thread_id, "expired")
            task.status = "error"
            task.error = "confirmation expired"
            raise HTTPException(status_code=410, detail="研究确认已过期")
    else:
        session_id = _session_id(req.session_id)
        if await _session_research_task(session_id, active_only=True):
            raise HTTPException(status_code=409, detail="该会话已有待确认或运行中的研究")
        memory_id = _optional_session_memory(session_id, req.memory_id)
        if memory_id is not None:
            _require_writable_memory(memory_id)
        thread_id = runtime.new_thread_id()
        task = ResearchTask(thread_id, session_id, message, memory_id)
        _TASKS[thread_id] = task
        store.add(session_id, "user", "chat", message)
        expires_at = task.created_at + getattr(
            runtime, "proposal_ttl_seconds", 86400
        )
        task.expires_at = expires_at
        record = get_runtime_registry().register(
            task_id=task.task_id,
            thread_id=task.thread_id,
            session_id=task.session_id,
            memory_id=memory_id,
            workflow_type="research",
            created_at=task.created_at,
            expires_at=expires_at,
        )
        try:
            state = await runtime.start(
                message,
                thread_id=thread_id,
                memory_id=memory_id,
                session_id=session_id,
                expires_at=expires_at,
            )
        except Exception as exc:
            task.status, task.error = "error", str(exc)
            await _settle_workflow_start_failure(
                record, type(exc).__name__.lower()
            )
            raise HTTPException(status_code=500, detail=f"研究说明生成失败: {exc}") from exc
    brief = _interrupt_brief(state)
    store.add(task.session_id, "assistant", KIND_PROPOSAL, _proposal_pointer(task, brief))
    task.status = "waiting_confirmation"
    return {
        "session_id": task.session_id, "task_id": task.task_id,
        "thread_id": task.thread_id, "status": task.status,
        "memory_id": task.memory_id, "action": "confirm", "brief": brief,
        "expires_at": task.expires_at,
    }


@app.post("/api/research")
async def start_research(req: ResearchRequest) -> dict[str, Any]:
    task = await _get_task(req.task_id)
    if req.session_id and req.session_id != task.session_id:
        raise HTTPException(status_code=409, detail="task 与 session 不匹配")
    if await _authoritative_research_status(task) != "waiting_confirmation":
        raise HTTPException(status_code=409, detail="任务当前不能确认启动")
    if task.memory_id is not None:
        _require_writable_memory(task.memory_id)
    if _optional_session_memory(task.session_id, task.memory_id) != task.memory_id:
        raise HTTPException(status_code=409, detail="task 与 session Memory 不匹配")
    lease = get_runtime_registry().claim_lease(
        task.task_id,
        lease_seconds=getattr(get_research_runtime(), "lease_seconds", 60),
    )
    if lease is None:
        raise HTTPException(status_code=409, detail="该任务正在被另一请求处理")
    if task.expires_at is not None and time.time() >= task.expires_at:
        record = get_runtime_registry().get(task.task_id)
        if record is None:
            get_runtime_registry().release_lease(task.task_id, lease)
            raise HTTPException(status_code=409, detail="任务 locator 已不可用")
        try:
            async with _workflow_lease(
                record, lease, get_research_runtime().lease_seconds
            ) as guard:
                await get_research_runtime().review(
                    task.thread_id,
                    "expire",
                    session_id=task.session_id,
                    memory_id=task.memory_id,
                )
                await guard.verify()
                get_runtime_registry().append_event(task.thread_id, "expired")
        finally:
            task.status = "error"
            task.error = "confirmation expired"
        raise HTTPException(status_code=410, detail="研究确认已过期")
    auto_created_memory = False
    memory_descriptor: MemoryDescriptor | None = None
    try:
        memory_descriptor, auto_created_memory = await _ensure_research_memory(
            task, lease
        )
        state = await get_research_runtime().confirm_research_start(
            task.thread_id,
            session_id=task.session_id,
            memory_id=task.memory_id,
        )
        if not get_runtime_registry().renew_lease(
            task.task_id,
            lease,
            lease_seconds=get_research_runtime().lease_seconds,
        ):
            raise WorkflowLeaseLostError("研究确认期间租约已失效")
    except TimeoutError as exc:
        get_runtime_registry().release_lease(task.task_id, lease)
        raise HTTPException(status_code=410, detail="研究确认已过期") from exc
    except WorkflowLeaseLostError as exc:
        raise HTTPException(status_code=409, detail="研究确认租约已失效") from exc
    except Exception as exc:
        get_runtime_registry().release_lease(task.task_id, lease)
        raise HTTPException(status_code=500, detail=f"研究确认失败: {exc}") from exc
    task.status = "running"
    get_runtime_registry().append_event(task.thread_id, "confirmed")
    await task.publish({
        "type": "confirmed", "thread_id": task.thread_id,
        "parent_thread_id": None, "root_thread_id": task.thread_id,
        "memory_id": task.memory_id,
    })
    _spawn_background(
        task.task_id,
        _run_research_task(task, resume_existing=True, lease_token=lease),
    )
    return {"task_id": task.task_id, "thread_id": task.thread_id,
            "session_id": task.session_id, "memory_id": task.memory_id,
            "memory_title": (
                memory_descriptor.title if memory_descriptor is not None else None
            ),
            "auto_created_memory": auto_created_memory,
            "status": task.status}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, cursor: int = 0,
                      last_event_id: str | None = Header(None, alias="Last-Event-ID")) -> StreamingResponse:
    record = get_runtime_registry().get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    task = _TASKS.get(task_id) if record.workflow_type == "research" else None
    try:
        cursor = max(0, cursor, int(last_event_id or 0))
    except ValueError:
        cursor = max(0, cursor)

    async def stream():
        current = cursor
        live_cursor = 0
        snapshot = await get_research_runtime().get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        try:
            checkpoint_status = derive_workflow_status(record, snapshot)
        except ValueError:
            checkpoint_status = "failed"
        _reconcile_workflow_outbox(record, snapshot, checkpoint_status)
        yield (
            f"data: {json.dumps({'type': 'snapshot', 'status': checkpoint_status, 'thread_id': record.thread_id, 'memory_id': record.memory_id}, ensure_ascii=False)}\n\n"
        )
        last_keepalive = time.monotonic()
        while True:
            for item in get_runtime_registry().list_events(
                record.thread_id, after_sequence=current
            ):
                current = item.sequence
                event = {
                    "type": item.event_type,
                    "sequence": item.sequence,
                    "thread_id": item.thread_id,
                    **item.payload,
                }
                yield (
                    f"id: {current}\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                )
                if item.event_type in {
                    "completed", "failed", "cancelled", "expired"
                }:
                    return
            if checkpoint_status in {"completed", "failed", "cancelled", "expired"}:
                return
            if task is not None:
                for event in tuple(task.events):
                    sequence = int(event.get("sequence", 0))
                    if sequence <= live_cursor:
                        continue
                    live_cursor = sequence
                    transient = dict(event)
                    transient.pop("sequence", None)
                    yield f"data: {json.dumps(transient, ensure_ascii=False)}\n\n"
            if time.monotonic() - last_keepalive >= 30:
                yield ": keep-alive\n\n"
                last_keepalive = time.monotonic()
            await asyncio.sleep(0.25)
            try:
                snapshot = await get_research_runtime().get_workflow_snapshot(
                    record.workflow_type, record.thread_id
                )
                checkpoint_status = derive_workflow_status(record, snapshot)
                _reconcile_workflow_outbox(record, snapshot, checkpoint_status)
            except Exception as exc:
                print(f"[Runtime] SSE checkpoint 对账暂缓: {exc}")
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/tasks/{task_id}/result")
async def task_result(task_id: str) -> dict[str, Any]:
    task = await _get_task(task_id)
    status = await _authoritative_research_status(task)
    if status in {"waiting_confirmation", "running"}:
        raise HTTPException(status_code=202, detail="任务尚未完成")
    record = get_runtime_registry().get(task_id)
    if record is None:
        raise HTTPException(status_code=409, detail="任务 locator 已不可用")
    snapshot = await get_research_runtime().get_workflow_snapshot(
        "research", task.thread_id
    )
    values = dict(snapshot.values)
    if status == "done" and isinstance(
        values.get("workflow_result"), ResearchWorkflowResult
    ):
        return {
            "transport_status": "done",
            **_research_task_result(
                task, values["workflow_result"], elapsed=0
            ),
        }
    failure = str(values.get("failure_code") or task.error or "")
    if status == "error":
        return {
            "task_id": task_id,
            "memory_id": task.memory_id,
            "status": "error",
            "message": failure,
        }
    return {"transport_status": "done", **(task.result or {})}


@app.get("/api/sessions/{sid}/active-task")
async def session_active_task(sid: str) -> dict[str, Any]:
    task = await _session_research_task(sid, active_only=True)
    if task is None:
        task = await _session_research_task(sid, active_only=False)
    memory_id = get_chat_store().get_memory_binding(sid)
    if task is None:
        return {"task_id": None, "memory_id": memory_id, "status": "idle"}
    payload: dict[str, Any] = {
        "task_id": task.task_id,
        "thread_id": task.thread_id,
        "memory_id": task.memory_id,
        "status": task.status,
        "expires_at": task.expires_at,
    }
    record = get_runtime_registry().get(task.task_id)
    if record is None:
        return {"task_id": None, "memory_id": memory_id, "status": "idle"}
    snapshot = await get_research_runtime().get_workflow_snapshot(
        "research", task.thread_id
    )
    status = derive_workflow_status(record, snapshot)
    payload["status"] = (
        "waiting_confirmation"
        if status == "waiting_confirmation"
        else "running"
        if status == "running"
        else "done"
        if status == "completed"
        else "error"
    )
    brief = snapshot.values.get("brief")
    if payload["status"] == "waiting_confirmation" and is_dataclass(brief):
        payload["brief"] = asdict(brief)
    return payload


@app.get("/api/sessions/{sid}/workflows")
async def session_workflows(sid: str) -> list[dict[str, Any]]:
    """Return recoverable non-research confirmation cards from checkpoints."""
    runtime = get_research_runtime()
    result: list[dict[str, Any]] = []
    for record in get_runtime_registry().list(session_id=sid):
        if record.workflow_type == "research":
            continue
        snapshot = await runtime.get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        status = derive_workflow_status(record, snapshot)
        if status != "waiting_confirmation":
            continue
        interrupts = getattr(snapshot, "interrupts", ())
        if len(interrupts) != 1 or not isinstance(interrupts[0].value, Mapping):
            continue
        result.append(
            {
                **_workflow_response_identity(record),
                "workflow_type": record.workflow_type,
                "status": status,
                "memory_id": record.memory_id,
                "session_id": record.session_id,
                "interrupt": dict(interrupts[0].value),
            }
        )
    return result


@app.get("/api/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return get_chat_store().list_sessions()


@app.get("/api/memories")
async def list_memories() -> list[dict[str, Any]]:
    runtime = get_research_runtime()
    if hasattr(runtime, "list_memory_options"):
        return [
            _memory_option_response(runtime, item)
            for item in runtime.list_memory_options()
        ]
    return [_memory_response(runtime, item) for item in runtime.list_memories()]


@app.post("/api/memories")
async def create_memory(req: MemoryCreateRequest) -> dict[str, Any]:
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Memory 标题不能为空")
    _configured_vault_name()
    runtime = get_research_runtime()
    try:
        descriptor = await asyncio.to_thread(runtime.create_memory, title)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _memory_response(runtime, descriptor)


@app.post("/api/legacy-memory/migration-proposals")
async def prepare_legacy_memory_migration(
    req: LegacyMigrationRequest,
) -> dict[str, Any]:
    title = (req.title or "").strip()
    target_memory_id = (req.target_memory_id or "").strip()
    if not title or not target_memory_id:
        raise HTTPException(
            status_code=400,
            detail="迁移标题和目标 memory_id 不能为空",
        )
    runtime = get_research_runtime()
    session_id = _session_id(req.session_id)
    if _session_memory(session_id, LEGACY_MEMORY_ID) != LEGACY_MEMORY_ID:
        raise HTTPException(status_code=409, detail="session 与 legacy Memory 不匹配")
    workflow_id = runtime.new_workflow_id("legacy_migration")
    expires_at = time.time() + runtime.proposal_ttl_seconds
    record = get_runtime_registry().register(
        task_id=workflow_id,
        thread_id=workflow_id,
        session_id=session_id,
        memory_id=LEGACY_MEMORY_ID,
        workflow_type="legacy_migration",
        created_at=time.time(),
        expires_at=expires_at,
    )
    try:
        state = await runtime.start_legacy_migration_workflow(
            session_id=session_id,
            title=title,
            target_memory_id=target_memory_id,
            thread_id=workflow_id,
            expires_at=expires_at,
        )
    except FileNotFoundError as exc:
        await _settle_workflow_start_failure(record, "legacy_source_missing")
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        await _settle_workflow_start_failure(record, "migration_target_exists")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await _settle_workflow_start_failure(record, "invalid_migration_request")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        await _settle_workflow_start_failure(record, "legacy_migration_failed")
        raise
    _validate_workflow_snapshot(record, state)
    proposal = state.get("proposal")
    if not isinstance(proposal, Mapping):
        raise RuntimeError("legacy migration workflow 未返回 checkpoint proposal")
    proposal_id = str(proposal.get("proposal_id") or "")
    if not proposal_id:
        raise RuntimeError("legacy migration proposal has no identity")
    return {
        **dict(proposal),
        "files": [dict(item) for item in proposal["files"]],
        "status": "proposal",
        "switch_is_explicit": True,
        "session_id": session_id,
        **_workflow_response_identity(record),
    }


@app.post("/api/legacy-memory/migration-proposals/{proposal_id}/confirm")
async def confirm_legacy_memory_migration(
    proposal_id: str,
    req: MemoryOperationDecisionRequest | None = None,
) -> dict[str, Any]:
    runtime = get_research_runtime()
    record, values = await _find_memory_workflow(
        "legacy_migration",
        value_key="proposal",
        identity_key="proposal_id",
        identity_value=proposal_id,
    )
    proposal = values["proposal"]
    _validate_optional_decision_identity(req, record, proposal_id=proposal_id)
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "confirm",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "proposal_id": proposal_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status != "committed":
        _raise_for_workflow_status(status)
        raise RuntimeError("legacy migration workflow 未完成")
    result = state.get("result")
    descriptor_value = result.get("descriptor") if isinstance(result, Mapping) else None
    if not isinstance(descriptor_value, Mapping):
        raise RuntimeError("legacy migration workflow 未返回 descriptor")
    descriptor = MemoryDescriptor(**dict(descriptor_value))
    retired = isinstance(proposal, Mapping) and isinstance(
        proposal.get("retirement"), Mapping
    )
    return {
        **_memory_response(runtime, descriptor),
        "status": "committed",
        "source_memory_id": LEGACY_MEMORY_ID,
        "switch_is_explicit": not retired,
        "session_id": record.session_id,
        **_workflow_response_identity(record),
    }


@app.delete("/api/legacy-memory/migration-proposals/{proposal_id}")
async def cancel_legacy_memory_migration(
    proposal_id: str,
    session_id: str | None = None,
    memory_id: str | None = None,
) -> dict[str, Any]:
    get_research_runtime()
    record, values = await _find_memory_workflow(
        "legacy_migration",
        value_key="proposal",
        identity_key="proposal_id",
        identity_value=proposal_id,
    )
    _validate_optional_decision_identity(
        MemoryOperationDecisionRequest(
            session_id=session_id,
            memory_id=memory_id,
            proposal_id=proposal_id,
        ),
        record,
        proposal_id=proposal_id,
    )
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "cancel",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "proposal_id": proposal_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status != "cancelled":
        _raise_for_workflow_status(status)
        raise RuntimeError("legacy migration workflow 未取消")
    return {
        "status": "cancelled",
        "proposal_id": proposal_id,
        "source_memory_id": LEGACY_MEMORY_ID,
        "session_id": record.session_id,
        **_workflow_response_identity(record),
    }


@app.post("/api/memories/{memory_id}/import-proposals")
async def prepare_memory_import(
    memory_id: str,
    req: MemoryImportProposalRequest,
) -> dict[str, Any]:
    session_id = _session_id(req.session_id)
    bound_memory_id = _session_memory(session_id, memory_id)
    _require_writable_memory(bound_memory_id)
    if bound_memory_id != memory_id:
        raise HTTPException(status_code=409, detail="session 与 memory 不匹配")
    runtime = get_research_runtime()

    provided = set(req.model_fields_set)
    allowed = {
        "file": {"kind", "session_id", "file_name", "media_type", "size_bytes", "content_base64"},
        "text": {"kind", "session_id", "title", "text"},
        "url": {"kind", "session_id", "url"},
    }[req.kind]
    required = {
        "file": {"kind", "file_name", "size_bytes", "content_base64"},
        "text": {"kind", "title", "text"},
        "url": {"kind", "url"},
    }[req.kind]
    if not required.issubset(provided) or not provided.issubset(allowed):
        raise HTTPException(status_code=400, detail="导入来源字段不匹配")

    source: dict[str, Any]
    try:
        if req.kind == "file":
            file_name = (req.file_name or "").strip()
            encoded = req.content_base64 or ""
            declared_size = req.size_bytes
            if not file_name or declared_size is None or declared_size < 0 or not encoded:
                raise HTTPException(status_code=400, detail="文件名、大小和内容不能为空")
            if (
                declared_size > _MAX_MEMORY_IMPORT_BYTES
                or len(encoded) > _MAX_MEMORY_IMPORT_BASE64_CHARS
            ):
                raise HTTPException(status_code=413, detail="导入文件不能超过 10 MiB")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=400, detail="content_base64 不是有效的 base64") from exc
            if len(content) != declared_size:
                raise HTTPException(status_code=400, detail="声明的文件大小与解码内容不匹配")
            if len(content) > _MAX_MEMORY_IMPORT_BYTES:
                raise HTTPException(status_code=413, detail="导入文件不能超过 10 MiB")
            source = {"kind": "file", "file_name": file_name, "content": content}
        elif req.kind == "text":
            title = (req.title or "").strip()
            text = req.text or ""
            if not title or not text.strip():
                raise HTTPException(status_code=400, detail="文本标题和内容不能为空")
            if len(text.encode("utf-8")) > _MAX_MEMORY_IMPORT_BYTES:
                raise HTTPException(status_code=413, detail="导入文本不能超过 10 MiB")
            source = {"kind": "text", "title": title, "text": text}
        else:
            url = req.url or ""
            if not url.strip():
                raise HTTPException(status_code=400, detail="URL 不能为空")
            source = {"kind": "url", "url": url}
    except MemoryImportLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    workflow_id = runtime.new_workflow_id("memory_import")
    expires_at = time.time() + runtime.proposal_ttl_seconds
    record = get_runtime_registry().register(
        task_id=workflow_id,
        thread_id=workflow_id,
        session_id=session_id,
        memory_id=memory_id,
        workflow_type="memory_import",
        created_at=time.time(),
        expires_at=expires_at,
    )
    try:
        state = await runtime.start_memory_import_workflow(
            session_id=session_id,
            memory_id=memory_id,
            source=source,
            thread_id=workflow_id,
            expires_at=expires_at,
        )
    except MemoryImportLimitError as exc:
        await _settle_workflow_start_failure(record, "memory_import_too_large")
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        await _settle_workflow_start_failure(record, "invalid_memory_import")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        await _settle_workflow_start_failure(record, "memory_import_failed")
        raise
    _validate_workflow_snapshot(record, state)
    status = str(state.get("workflow_status") or "")
    if status == "duplicate":
        duplicate = state.get("duplicate")
        if not isinstance(duplicate, MemoryImportDuplicate):
            raise RuntimeError("Memory import workflow 未返回 checkpoint duplicate")
        snapshot = await runtime.get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        _reconcile_workflow_outbox(record, snapshot)
        payload = _import_preview_response(runtime, duplicate)
    else:
        proposal = state.get("proposal")
        if not isinstance(proposal, MemoryImportProposal):
            raise RuntimeError("Memory import workflow 未返回 checkpoint proposal")
        payload = _import_preview_response(runtime, proposal)
    payload.update({"session_id": session_id, **_workflow_response_identity(record)})
    return payload


@app.post("/api/memories/{memory_id}/import-proposals/{proposal_id}/confirm")
async def confirm_memory_import(
    memory_id: str,
    proposal_id: str,
    req: MemoryOperationDecisionRequest | None = None,
) -> dict[str, Any]:
    runtime = get_research_runtime()
    record, values = await _find_memory_workflow(
        "memory_import",
        value_key="proposal",
        identity_key="proposal_id",
        identity_value=proposal_id,
    )
    proposal = values["proposal"]
    if not isinstance(proposal, MemoryImportProposal):
        raise RuntimeError("Memory import checkpoint proposal 无效")
    _validate_optional_decision_identity(
        req,
        record,
        proposal_id=proposal_id,
    )
    _require_writable_memory(memory_id)
    if memory_id != record.memory_id or proposal.memory_id != record.memory_id:
        raise HTTPException(status_code=409, detail="proposal 与 memory 不匹配")
    if _session_memory(record.session_id, memory_id) != memory_id:
        raise HTTPException(status_code=409, detail="session 与 memory 不匹配")
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "confirm",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "proposal_id": proposal_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status not in {"committed", "duplicate"}:
        _raise_for_workflow_status(status)
        raise RuntimeError("Memory import workflow 未完成")
    payload = _memory_import_commit_response(runtime, state.get("result"))
    if payload["memory_id"] != memory_id:
        raise RuntimeError("Memory import commit does not match the proposal")
    payload.update(
        {"session_id": record.session_id, **_workflow_response_identity(record)}
    )
    return payload


@app.delete("/api/memories/{memory_id}/import-proposals/{proposal_id}")
async def cancel_memory_import(
    memory_id: str,
    proposal_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    get_research_runtime()
    record, values = await _find_memory_workflow(
        "memory_import",
        value_key="proposal",
        identity_key="proposal_id",
        identity_value=proposal_id,
    )
    proposal = values["proposal"]
    if not isinstance(proposal, MemoryImportProposal):
        raise RuntimeError("Memory import checkpoint proposal 无效")
    _require_writable_memory(memory_id)
    if session_id is not None and session_id.strip() != record.session_id:
        raise HTTPException(status_code=409, detail="session 身份不匹配")
    if memory_id != record.memory_id or proposal.memory_id != record.memory_id:
        raise HTTPException(status_code=409, detail="proposal 与 memory 不匹配")
    if _session_memory(record.session_id, memory_id) != memory_id:
        raise HTTPException(status_code=409, detail="session 与 memory 不匹配")
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "cancel",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "proposal_id": proposal_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status != "cancelled":
        _raise_for_workflow_status(status)
        raise RuntimeError("Memory import workflow 未取消")
    return {
        "status": "cancelled",
        "proposal_id": proposal_id,
        "memory_id": memory_id,
        "session_id": record.session_id,
        **_workflow_response_identity(record),
    }


@app.post("/api/memories/{memory_id}/answers")
async def answer_memory(
    memory_id: str,
    req: MemoryAnswerRequest,
) -> dict[str, Any]:
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Memory 问题不能为空")
    session_id = _session_id(req.session_id)
    if _session_memory(session_id, memory_id) != memory_id:
        raise HTTPException(status_code=409, detail="session 与 memory 不匹配")
    runtime = get_research_runtime()
    workflow_id = runtime.new_workflow_id("memory_note")
    expires_at = time.time() + runtime.proposal_ttl_seconds
    record = get_runtime_registry().register(
        task_id=workflow_id,
        thread_id=workflow_id,
        session_id=session_id,
        memory_id=memory_id,
        workflow_type="memory_note",
        created_at=time.time(),
        expires_at=expires_at,
    )
    try:
        state = await runtime.start_memory_note_workflow(
            session_id=session_id,
            memory_id=memory_id,
            question=question,
            thread_id=workflow_id,
            expires_at=expires_at,
        )
    except ValueError as exc:
        await _settle_workflow_start_failure(record, "invalid_memory_question")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        await _settle_workflow_start_failure(record, "memory_answer_failed")
        raise
    _validate_workflow_snapshot(record, state)
    answer = state.get("answer")
    if (
        not isinstance(answer, MemoryAnswer)
        or answer.memory_id != memory_id
        or answer.question != question
    ):
        raise RuntimeError("Memory answer does not match the request")
    payload = _answer_response(runtime, answer)
    payload.update({"session_id": session_id, **_workflow_response_identity(record)})
    return payload


@app.delete("/api/memory-answers/{answer_id}")
async def dismiss_memory_answer(
    answer_id: str,
    session_id: str | None = None,
    memory_id: str | None = None,
) -> dict[str, Any]:
    """Finish one read-only Memory answer without creating a note proposal."""

    record, values = await _find_memory_workflow(
        "memory_note",
        value_key="answer",
        identity_key="answer_id",
        identity_value=answer_id,
    )
    answer = values["answer"]
    if not isinstance(answer, MemoryAnswer):
        raise RuntimeError("Memory answer checkpoint 内容无效")
    _validate_optional_decision_identity(
        MemoryOperationDecisionRequest(
            session_id=session_id,
            memory_id=memory_id,
            answer_id=answer_id,
        ),
        record,
        answer_id=answer_id,
    )
    if answer.memory_id != record.memory_id:
        raise HTTPException(status_code=409, detail="answer 与 memory 不匹配")
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "cancel",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "answer_id": answer_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status != "cancelled":
        _raise_for_workflow_status(status)
        raise RuntimeError("Memory answer workflow 未结束")
    return {
        "status": "cancelled",
        "answer_id": answer_id,
        "memory_id": record.memory_id,
        "session_id": record.session_id,
        **_workflow_response_identity(record),
    }


@app.post("/api/memories/{memory_id}/note-proposals")
async def propose_memory_note(
    memory_id: str,
    req: MemoryNoteProposalRequest,
) -> dict[str, Any]:
    answer_id = (req.answer_id or "").strip()
    if not answer_id:
        raise HTTPException(status_code=400, detail="answer_id 不能为空")
    runtime = get_research_runtime()
    record, values = await _find_memory_workflow(
        "memory_note",
        value_key="answer",
        identity_key="answer_id",
        identity_value=answer_id,
    )
    answer = values["answer"]
    if not isinstance(answer, MemoryAnswer):
        raise RuntimeError("Memory note checkpoint answer 无效")
    requested_session_id = (req.session_id or "").strip()
    if requested_session_id and requested_session_id != record.session_id:
        raise HTTPException(status_code=409, detail="answer 与 session 不匹配")
    if _session_memory(record.session_id, memory_id) != memory_id:
        raise HTTPException(status_code=409, detail="session 与 memory 不匹配")
    _require_writable_memory(memory_id)
    if answer.memory_id != memory_id or record.memory_id != memory_id:
        raise HTTPException(status_code=409, detail="answer 与 memory 不匹配")
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "propose",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "answer_id": answer_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status != "waiting_confirmation":
        _raise_for_workflow_status(status)
        raise RuntimeError("Memory note workflow 未返回提案确认点")
    proposal = state.get("proposal")
    if (
        not isinstance(proposal, MemoryNoteProposal)
        or proposal.answer_id != answer.answer_id
        or proposal.memory_id != memory_id
    ):
        raise RuntimeError("Memory note proposal does not match the answer")
    payload = asdict(proposal)
    payload.update(
        {"session_id": record.session_id, **_workflow_response_identity(record)}
    )
    return payload


@app.post("/api/memory-note-proposals/{proposal_id}/confirm")
async def confirm_memory_note(
    proposal_id: str,
    req: MemoryOperationDecisionRequest | None = None,
) -> dict[str, Any]:
    runtime = get_research_runtime()
    record, values = await _find_memory_workflow(
        "memory_note",
        value_key="proposal",
        identity_key="proposal_id",
        identity_value=proposal_id,
    )
    proposal = values["proposal"]
    answer = values.get("answer")
    if not isinstance(proposal, MemoryNoteProposal) or not isinstance(
        answer, MemoryAnswer
    ):
        raise RuntimeError("Memory note checkpoint 内容无效")
    _validate_optional_decision_identity(
        req,
        record,
        proposal_id=proposal_id,
        answer_id=proposal.answer_id,
    )
    _require_writable_memory(proposal.memory_id)
    if _session_memory(record.session_id, proposal.memory_id) != proposal.memory_id:
        raise HTTPException(status_code=409, detail="session 与 memory 不匹配")
    if record.memory_id != proposal.memory_id:
        raise HTTPException(status_code=409, detail="proposal 与 memory 不匹配")
    if values.get("session_id") != record.session_id:
        raise HTTPException(status_code=409, detail="proposal 与 session 不匹配")
    if answer.answer_id != proposal.answer_id or answer.memory_id != proposal.memory_id:
        raise HTTPException(status_code=409, detail="proposal 与 answer 不匹配")
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "confirm",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "proposal_id": proposal_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status != "committed":
        _raise_for_workflow_status(status)
        raise RuntimeError("Memory note workflow 未完成")
    payload = _commit_response(state.get("result"))
    if (
        payload["memory_id"] != proposal.memory_id
        or payload["target_path"] != proposal.target_path
        or payload["home_path"] != proposal.home_path
        or payload["wikilink"] != proposal.wikilink
    ):
        raise RuntimeError("Memory note commit does not match the proposal")
    payload.update(
        {"session_id": record.session_id, **_workflow_response_identity(record)}
    )
    return payload


@app.delete("/api/memory-note-proposals/{proposal_id}")
async def cancel_memory_note(
    proposal_id: str,
    session_id: str | None = None,
    memory_id: str | None = None,
) -> dict[str, Any]:
    record, values = await _find_memory_workflow(
        "memory_note",
        value_key="proposal",
        identity_key="proposal_id",
        identity_value=proposal_id,
    )
    proposal = values["proposal"]
    if not isinstance(proposal, MemoryNoteProposal):
        raise RuntimeError("Memory note checkpoint proposal 无效")
    _validate_optional_decision_identity(
        MemoryOperationDecisionRequest(
            session_id=session_id,
            memory_id=memory_id,
            proposal_id=proposal_id,
        ),
        record,
        proposal_id=proposal_id,
        answer_id=proposal.answer_id,
    )
    state = await _resume_registered_memory_workflow(
        record,
        values,
        {
            "action": "cancel",
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "proposal_id": proposal_id,
        },
    )
    status = str(state.get("workflow_status") or "")
    if status != "cancelled":
        _raise_for_workflow_status(status)
        raise RuntimeError("Memory note workflow 未取消")
    return {
        "status": "cancelled",
        "proposal_id": proposal_id,
        "memory_id": record.memory_id,
        "session_id": record.session_id,
        **_workflow_response_identity(record),
    }


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
    runtime = get_research_runtime()
    registry = get_runtime_registry()
    records = registry.list(session_id=sid)
    handles = [
        _BACKGROUND_TASKS[record.task_id]
        for record in records
        if record.task_id in _BACKGROUND_TASKS
    ]
    for handle in handles:
        handle.cancel()
    if handles:
        await asyncio.gather(*handles, return_exceptions=True)

    records = registry.list(session_id=sid)
    lease_tokens: dict[str, str] = {}
    for record in records:
        token = registry.claim_lease(
            record.task_id, lease_seconds=runtime.lease_seconds
        )
        if token is None:
            for task_id, owned in lease_tokens.items():
                registry.release_lease(task_id, owned)
            raise HTTPException(
                status_code=409,
                detail="会话仍有工作流正在执行，请稍后重试删除",
            )
        lease_tokens[record.task_id] = token

    thread_ids: tuple[str, ...] = ()
    try:
        async with AsyncExitStack() as stack:
            guards: list[_LeaseGuard] = []
            for record in records:
                guards.append(
                    await stack.enter_async_context(
                        _workflow_lease(
                            record,
                            lease_tokens[record.task_id],
                            runtime.lease_seconds,
                        )
                    )
                )
            for record in records:
                snapshot = await runtime.get_workflow_snapshot(
                    record.workflow_type, record.thread_id
                )
                status = derive_workflow_status(record, snapshot)
                if status == "waiting_confirmation":
                    if record.workflow_type == "research":
                        await runtime.review(
                            record.thread_id,
                            "cancel",
                            session_id=record.session_id,
                            memory_id=record.memory_id,
                        )
                    else:
                        await runtime.resume_memory_operation(
                            record.workflow_type,
                            record.thread_id,
                            _interrupt_decision(record, snapshot, action="cancel"),
                        )
                elif status == "running":
                    await runtime.mark_workflow_cancelled(
                        record.workflow_type, record.thread_id
                    )
                latest = await runtime.get_workflow_snapshot(
                    record.workflow_type, record.thread_id
                )
                latest_status = derive_workflow_status(record, latest)
                if latest_status not in {
                    "completed", "failed", "cancelled", "expired"
                }:
                    raise RuntimeError(
                        f"工作流未能在删除前收口: {record.thread_id}"
                    )
                if latest_status == "cancelled":
                    registry.append_event(
                        record.thread_id,
                        "cancelled",
                        {"reason": "user_cancelled"},
                    )
            for guard in guards:
                await guard.verify()
            deleted, thread_ids = get_chat_store().delete_session_with_workflow_leases(
                sid, lease_tokens
            )
    except WorkflowLeaseLostError as exc:
        raise HTTPException(
            status_code=409, detail="删除期间工作流租约已被接管，请重试"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    for thread_id in thread_ids:
        await runtime.delete_workflow(thread_id)
    removed_workflows = len(thread_ids)
    for task_id, task in list(_TASKS.items()):
        if task.session_id == sid:
            _TASKS.pop(task_id, None)
    deleted_payload = {"chat": deleted}
    if removed_workflows:
        deleted_payload["workflows"] = removed_workflows
    return {"session_id": sid, "deleted": deleted_payload}

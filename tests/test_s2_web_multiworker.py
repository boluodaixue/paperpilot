from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import web.server as server
from src.research.models import MemoryDescriptor
from src.research.runtime_registry import OutboxEvent, WorkflowRecord


def _record(*, workflow_type: str = "research") -> WorkflowRecord:
    return WorkflowRecord(
        task_id="task-shared",
        thread_id="task-shared",
        session_id="session-shared",
        memory_id="M-shared",
        workflow_type=workflow_type,
        created_at=100.0,
        expires_at=1000.0,
        lease_owner=None,
        lease_until=None,
    )


def _snapshot(record: WorkflowRecord, *, status: str, waiting: bool = False):
    return SimpleNamespace(
        values={
            "thread_id": record.thread_id,
            "session_id": record.session_id,
            "memory_id": record.memory_id,
            "workflow_type": record.workflow_type,
            "workflow_status": status,
            "question": "cross-worker question",
            "decision": "confirm" if record.workflow_type != "research" else None,
        },
        next=() if waiting or status in {"committed", "completed"} else ("run",),
        interrupts=(object(),) if waiting else (),
    )


@pytest.mark.asyncio
async def test_get_task_rebuilds_disposable_cache_from_registry_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    snapshot = _snapshot(record, status="waiting_confirmation", waiting=True)

    class Registry:
        def get(self, task_id):
            return record if task_id == record.task_id else None

        def append_event(self, *_args, **_kwargs):
            raise AssertionError("waiting checkpoint must not emit terminal events")

    class Runtime:
        async def get_workflow_snapshot(self, workflow_type, thread_id):
            assert (workflow_type, thread_id) == ("research", record.thread_id)
            return snapshot

    monkeypatch.setattr(server, "get_runtime_registry", lambda: Registry())
    monkeypatch.setattr(server, "get_research_runtime", lambda: Runtime())
    server._TASKS.clear()

    task = await server._get_task(record.task_id)

    assert task.task_id == record.task_id
    assert task.query == "cross-worker question"
    assert task.status == "waiting_confirmation"
    assert server._TASKS[record.task_id] is task


@pytest.mark.asyncio
async def test_generic_terminal_sse_uses_registry_checkpoint_and_durable_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record(workflow_type="memory_note")
    snapshot = _snapshot(record, status="committed")

    class Registry:
        def __init__(self) -> None:
            self.events: list[OutboxEvent] = []

        def get(self, task_id):
            return record if task_id == record.task_id else None

        def append_event(self, thread_id, event_type, payload=None, **_kwargs):
            for event in self.events:
                if event.event_type == event_type:
                    return event
            event = OutboxEvent(
                event_id=f"event-{event_type}",
                thread_id=thread_id,
                sequence=len(self.events) + 1,
                event_type=event_type,
                payload_json="{}",
            )
            self.events.append(event)
            return event

        def list_events(self, thread_id, *, after_sequence=0):
            assert thread_id == record.thread_id
            return tuple(
                event for event in self.events if event.sequence > after_sequence
            )

    class Runtime:
        async def get_workflow_snapshot(self, workflow_type, thread_id):
            assert (workflow_type, thread_id) == (
                record.workflow_type,
                record.thread_id,
            )
            return snapshot

    registry = Registry()
    monkeypatch.setattr(server, "get_runtime_registry", lambda: registry)
    monkeypatch.setattr(server, "get_research_runtime", lambda: Runtime())
    server._TASKS.clear()

    response = await server.task_events(record.task_id, last_event_id=None)
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert '"status": "completed"' in body
    assert '"type": "confirmed"' in body
    assert '"type": "completed"' in body
    assert body.index('"type": "snapshot"') < body.index('"type": "completed"')


@pytest.mark.asyncio
async def test_create_memory_runs_sync_writer_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    entered = threading.Event()
    caller_thread = threading.get_ident()
    writer_threads: list[int] = []

    class Runtime:
        memory_store = SimpleNamespace(root=tmp_path)

        def create_memory(self, title):
            writer_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(timeout=2)
            return MemoryDescriptor(
                memory_id="M-threaded",
                title=title,
                relative_path="Memories/M-threaded/",
                created_at="2026-08-28T00:00:00+08:00",
                updated_at="2026-08-28T00:00:00+08:00",
            )

    monkeypatch.setattr(server, "get_research_runtime", lambda: Runtime())
    monkeypatch.setattr(server, "_configured_vault_name", lambda: None)

    pending = asyncio.create_task(
        server.create_memory(server.MemoryCreateRequest(title="Threaded"))
    )
    assert await asyncio.to_thread(entered.wait, 1)
    ticked = False
    await asyncio.sleep(0)
    ticked = True
    release.set()
    result = await pending

    assert ticked is True
    assert writer_threads and writer_threads[0] != caller_thread
    assert result["memory_id"] == "M-threaded"


@pytest.mark.asyncio
async def test_sweeper_rebuilds_research_adapter_before_claiming_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record()
    snapshot = _snapshot(record, status="running")
    stop = asyncio.Event()
    calls: list[str] = []

    class Registry:
        def list(self):
            return (record,)

        def claim_lease(self, task_id, **_kwargs):
            assert task_id in server._TASKS
            calls.append(task_id)
            stop.set()
            return "lease-token"

    class Runtime:
        sweep_interval_seconds = 0.001
        lease_seconds = 60
        terminal_retention_seconds = 3600

        async def get_workflow_snapshot(self, workflow_type, thread_id):
            return snapshot

    def capture_spawn(task_id, coroutine):
        calls.append(f"spawn:{task_id}")
        coroutine.close()

    server._TASKS.clear()
    monkeypatch.setattr(server, "get_runtime_registry", lambda: Registry())
    monkeypatch.setattr(server, "get_research_runtime", lambda: Runtime())
    monkeypatch.setattr(server, "_spawn_background", capture_spawn)

    await asyncio.wait_for(server._workflow_sweeper(stop), timeout=1)

    assert calls == [record.task_id, f"spawn:{record.task_id}"]
    assert server._TASKS[record.task_id].status == "running"

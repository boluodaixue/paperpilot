"""S1 CLI and evaluation checkpointer lifecycle acceptance tests."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scripts import run_benchmark, run_eval, run_repl, run_single
from scripts._workflow_cli import (
    UserCancelled,
    run_legacy_migration_workflow,
    run_memory_import_workflow,
    run_memory_question,
    run_reviewed_workflow,
)
from src.memory.chat_store import ChatStore
from src.research.memory import MarkdownMemoryStore
from src.research.models import (
    MemoryManifest,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchWorkflowResult,
)
from src.research.runtime import open_research_runtime
from src.research.runtime_registry import RuntimeRegistry
from tests._checkpoint_web_runtime import CheckpointWebPolicy


def _result(memory_id: str | None = "M-cli") -> ResearchWorkflowResult:
    brief = ResearchBrief(
        question="question",
        objective="objective",
        scope=("scope",),
        directions=("direction",),
        constraints=("constraint",),
        expected_output="report",
        memory_id=memory_id,
    )
    research = ResearchResult(
        task_id="task",
        status=ResearchStatus.PARTIAL,
        summary="summary",
        unresolved=("No source-locatable evidence was collected.",),
    )
    prefix = f"Memories/{memory_id}/" if memory_id is not None else ""
    return ResearchWorkflowResult(
        brief=brief,
        research_result=research,
        report_markdown="# Report",
        memory_manifest=MemoryManifest(report_path=f"{prefix}reports/report.md"),
        memory_id=memory_id,
    )


class _SnapshotRuntime:
    def __init__(self, snapshot: Any, result: ResearchWorkflowResult) -> None:
        self.snapshot = snapshot
        self.result = result
        self.calls: list[tuple[str, str | None]] = []

    async def get_snapshot(self, thread_id: str) -> Any:
        self.calls.append(("snapshot", thread_id))
        return self.snapshot

    async def start(self, *args, **kwargs):
        raise AssertionError("an existing thread must not be started again")

    async def continue_research(self, thread_id: str) -> dict[str, Any]:
        self.calls.append(("continue", thread_id))
        return {"workflow_result": self.result, "workflow_status": "completed"}

    async def review(
        self,
        thread_id: str,
        action: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((action, feedback))
        return {"workflow_result": self.result, "workflow_status": "completed"}


@pytest.mark.asyncio
async def test_existing_brief_checkpoint_is_displayed_without_restarting() -> None:
    result = _result()
    runtime = _SnapshotRuntime(
        SimpleNamespace(
            values={
                "question": "question",
                "memory_id": "M-cli",
                "brief": result.brief,
                "workflow_status": "waiting_confirmation",
                "workflow_result": None,
            },
            next=("review_brief",),
        ),
        result,
    )
    output: list[str] = []

    resumed = await run_reviewed_workflow(
        runtime,  # type: ignore[arg-type]
        "question",
        thread_id="existing-thread",
        memory_id="M-cli",
        auto_confirm=True,
        output_fn=output.append,
    )

    assert resumed is result
    assert runtime.calls == [("snapshot", "existing-thread"), ("confirm", None)]
    assert any("Research Brief" in item for item in output)


@pytest.mark.asyncio
async def test_existing_non_interrupt_checkpoint_uses_plain_continue() -> None:
    result = _result()
    runtime = _SnapshotRuntime(
        SimpleNamespace(
            values={
                "question": "question",
                "memory_id": "M-cli",
                "brief": result.brief,
                "confirmed": True,
                "workflow_status": "running",
                "workflow_result": None,
            },
            next=("research_agent",),
        ),
        result,
    )

    resumed = await run_reviewed_workflow(
        runtime,  # type: ignore[arg-type]
        "question",
        thread_id="running-thread",
        memory_id="M-cli",
        auto_confirm=True,
    )

    assert resumed is result
    assert runtime.calls == [
        ("snapshot", "running-thread"),
        ("continue", "running-thread"),
    ]


@pytest.mark.asyncio
async def test_reusing_thread_for_another_question_or_memory_is_rejected() -> None:
    result = _result()
    snapshot = SimpleNamespace(
        values={
            "question": "question",
            "memory_id": "M-cli",
            "brief": result.brief,
            "workflow_status": "waiting_confirmation",
        },
        next=("review_brief",),
    )

    with pytest.raises(ValueError, match="different research question"):
        await run_reviewed_workflow(
            _SnapshotRuntime(snapshot, result),  # type: ignore[arg-type]
            "different",
            thread_id="claimed-thread",
            memory_id="M-cli",
            auto_confirm=True,
        )
    with pytest.raises(ValueError, match="different Memory"):
        await run_reviewed_workflow(
            _SnapshotRuntime(snapshot, result),  # type: ignore[arg-type]
            "question",
            thread_id="claimed-thread",
            memory_id="M-other",
            auto_confirm=True,
        )


class _OfflinePolicy:
    def __init__(self) -> None:
        self.alignment_calls = 0

    def __call__(self, messages, *, tools=None):
        if "before research begins" in messages[0]["content"]:
            self.alignment_calls += 1
            return {
                "content": json.dumps(
                    {
                        "objective": "objective",
                        "scope": ["scope"],
                        "directions": ["direction"],
                        "constraints": ["constraint"],
                        "expected_output": "report",
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": json.dumps(
                {
                    "status": "partial",
                    "summary": "offline summary",
                    "findings": [],
                    "unresolved": ["offline"],
                }
            ),
            "tool_calls": [],
        }


@pytest.mark.asyncio
async def test_sqlite_cli_reopens_same_brief_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    descriptor = store.create_memory("CLI Memory", memory_id="M-cli")
    policy = _OfflinePolicy()
    config = {"research": {"limits": {"max_iterations": 3}}}
    thread_id = "cli-sqlite-restart"

    async with open_research_runtime(
        database,
        config=config,
        policy=policy,
        tools=[],
        memory_store=store,
    ) as runtime:
        paused = await runtime.start(
            "question",
            thread_id=thread_id,
            memory_id=descriptor.memory_id,
        )
        assert paused["workflow_status"] == "waiting_confirmation"

    async with open_research_runtime(
        database,
        config=config,
        policy=policy,
        tools=[],
        memory_store=store,
    ) as runtime:
        result = await run_reviewed_workflow(
            runtime,
            "question",
            thread_id=thread_id,
            memory_id=descriptor.memory_id,
            auto_confirm=True,
        )

    assert result.memory_id == descriptor.memory_id
    assert policy.alignment_calls == 1


@pytest.mark.asyncio
async def test_cli_cancel_persists_cancelled_research_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("CLI Memory", memory_id="M-cli")
    ChatStore(str(database)).set_meta("cli-cancel", title="cli-cancel")
    registry = RuntimeRegistry(database)
    config = {"research": {"limits": {"max_iterations": 3}}}

    async with open_research_runtime(
        database,
        config=config,
        policy=_OfflinePolicy(),
        tools=[],
        memory_store=store,
    ) as runtime:
        with pytest.raises(UserCancelled):
            await run_reviewed_workflow(
                runtime,
                "question",
                thread_id="cli-cancel-thread",
                memory_id="M-cli",
                session_id="cli-cancel",
                registry=registry,
                input_fn=lambda _prompt: "q",
            )
        snapshot = await runtime.get_snapshot("cli-cancel-thread")
        assert snapshot.values["workflow_status"] == "cancelled"
        assert snapshot.next == ()

    async with open_research_runtime(
        database,
        config=config,
        policy=_OfflinePolicy(),
        tools=[],
        memory_store=store,
    ) as runtime:
        restored = await runtime.get_snapshot("cli-cancel-thread")
        assert restored.values["workflow_status"] == "cancelled"
        assert restored.next == ()
    events = registry.list_events("cli-cancel-thread")
    assert [event.event_type for event in events] == ["cancelled"]


@pytest.mark.asyncio
async def test_cli_registry_locates_all_paused_workflows_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chat.db"
    root = tmp_path / "vault"
    store = MarkdownMemoryStore(root)
    store.create_memory("CLI Memory", memory_id="M-cli")
    legacy = root / "reports" / "Report-old.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Old\n\nKnown legacy evidence.\n", encoding="utf-8")
    ChatStore(str(database)).set_meta("cli-restart", title="cli-restart")
    registry = RuntimeRegistry(database)
    policy = CheckpointWebPolicy()
    config = {
        "runtime": {
            "proposal_ttl_seconds": 3600,
            "terminal_retention_seconds": 3600,
            "lease_seconds": 60,
            "sweep_interval_seconds": 5,
        }
    }

    def stop_at_confirmation(_prompt: str) -> str:
        raise RuntimeError("simulated CLI process stop")

    async with open_research_runtime(
        database,
        config=config,
        policy=policy,
        tools=[],
        memory_store=store,
    ) as runtime:
        operations = (
            run_memory_question(
                runtime,
                "M-cli",
                "unknown question",
                session_id="cli-restart",
                registry=registry,
                input_fn=stop_at_confirmation,
            ),
            run_memory_import_workflow(
                runtime,
                "M-cli",
                {"kind": "text", "title": "Inline", "text": "body"},
                session_id="cli-restart",
                registry=registry,
                input_fn=stop_at_confirmation,
            ),
            run_legacy_migration_workflow(
                runtime,
                "M-copy",
                "Copy",
                session_id="cli-restart",
                registry=registry,
                input_fn=stop_at_confirmation,
            ),
            run_reviewed_workflow(
                runtime,
                "research question",
                thread_id="cli-restart-research",
                memory_id="M-cli",
                session_id="cli-restart",
                registry=registry,
                input_fn=stop_at_confirmation,
            ),
        )
        for operation in operations:
            with pytest.raises(RuntimeError, match="simulated CLI process stop"):
                await operation

    async with open_research_runtime(
        database,
        config=config,
        policy=policy,
        tools=[],
        memory_store=store,
    ) as runtime:
        records = registry.list(session_id="cli-restart")
        assert {record.workflow_type for record in records} == {
            "memory_note",
            "memory_import",
            "legacy_migration",
            "research",
        }
        for record in records:
            snapshot = await runtime.get_workflow_snapshot(
                record.workflow_type, record.thread_id
            )
            assert snapshot.values["workflow_status"] in {
                "waiting_answer_decision",
                "waiting_confirmation",
            }
            assert snapshot.next
        await run_repl._print_workflows(runtime, registry, "cli-restart")
        for record in records:
            snapshot = await runtime.get_workflow_snapshot(
                record.workflow_type, record.thread_id
            )
            assert snapshot.values["workflow_status"] in {
                "waiting_answer_decision",
                "waiting_confirmation",
            }
            assert registry.list_events(record.thread_id) == ()


@pytest.mark.asyncio
async def test_cli_restart_backfills_terminal_outbox_idempotently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("CLI Memory", memory_id="M-cli")
    ChatStore(str(database)).set_meta("cli-outbox", title="cli-outbox")
    registry = RuntimeRegistry(database)
    expires_at = time.time() + 3600
    registry.register(
        task_id="cli-outbox-thread",
        thread_id="cli-outbox-thread",
        session_id="cli-outbox",
        memory_id="M-cli",
        workflow_type="research",
        expires_at=expires_at,
    )
    config = {"research": {"limits": {"max_iterations": 3}}}

    async with open_research_runtime(
        database,
        config=config,
        policy=_OfflinePolicy(),
        tools=[],
        memory_store=store,
    ) as runtime:
        await runtime.start(
            "question",
            thread_id="cli-outbox-thread",
            memory_id="M-cli",
            session_id="cli-outbox",
            expires_at=expires_at,
        )
        completed = await runtime.review("cli-outbox-thread", "confirm")
        assert completed["workflow_status"] == "completed"
        assert registry.list_events("cli-outbox-thread") == ()

    async with open_research_runtime(
        database,
        config=config,
        policy=_OfflinePolicy(),
        tools=[],
        memory_store=store,
    ) as runtime:
        await run_repl._print_workflows(runtime, registry, "cli-outbox")
        await run_repl._print_workflows(runtime, registry, "cli-outbox")

    assert [
        event.event_type for event in registry.list_events("cli-outbox-thread")
    ] == ["confirmed", "completed"]


@pytest.mark.asyncio
async def test_cli_refuses_a_workflow_owned_by_another_executor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("CLI Memory", memory_id="M-cli")
    ChatStore(str(database)).set_meta("cli-busy", title="cli-busy")
    registry = RuntimeRegistry(database)
    expires_at = time.time() + 3600
    record = registry.register(
        task_id="cli-busy-thread",
        thread_id="cli-busy-thread",
        session_id="cli-busy",
        memory_id="M-cli",
        workflow_type="research",
        expires_at=expires_at,
    )
    config = {"runtime": {"lease_seconds": 60}}

    async with open_research_runtime(
        database,
        config=config,
        policy=_OfflinePolicy(),
        tools=[],
        memory_store=store,
    ) as runtime:
        await runtime.start(
            "question",
            thread_id=record.thread_id,
            memory_id=record.memory_id,
            session_id=record.session_id,
            expires_at=expires_at,
        )
        other_owner = registry.claim_lease(
            record.task_id, lease_seconds=runtime.lease_seconds
        )
        assert other_owner is not None
        with pytest.raises(RuntimeError, match="already being executed"):
            await run_reviewed_workflow(
                runtime,
                "question",
                thread_id=record.thread_id,
                memory_id=record.memory_id,
                session_id=record.session_id,
                registry=registry,
                auto_confirm=True,
            )
        snapshot = await runtime.get_snapshot(record.thread_id)
        assert snapshot.values["workflow_status"] == "waiting_confirmation"
        assert registry.release_lease(record.task_id, other_owner)


@pytest.mark.asyncio
async def test_cli_renews_short_lease_while_waiting_for_blocking_input(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("CLI Memory", memory_id="M-cli")
    session_id = "cli-blocking-input"
    thread_id = "cli-blocking-input-thread"
    ChatStore(str(database)).set_meta(session_id, title=session_id)
    registry = RuntimeRegistry(database)
    entered = threading.Event()
    release_input = threading.Event()

    def blocking_input(_prompt: str) -> str:
        entered.set()
        assert release_input.wait(timeout=5)
        return "q"

    async with open_research_runtime(
        database,
        config={"runtime": {"lease_seconds": 0.15}},
        policy=_OfflinePolicy(),
        tools=[],
        memory_store=store,
    ) as runtime:
        task = asyncio.create_task(
            run_reviewed_workflow(
                runtime,
                "question",
                thread_id=thread_id,
                memory_id="M-cli",
                session_id=session_id,
                registry=registry,
                input_fn=blocking_input,
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.3)
        assert registry.claim_lease(thread_id, lease_seconds=0.15) is None
        release_input.set()
        with pytest.raises(UserCancelled):
            await task

    assert [event.event_type for event in registry.list_events(thread_id)] == [
        "cancelled"
    ]


@pytest.mark.asyncio
async def test_cli_losing_lease_during_input_cannot_review_checkpoint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("CLI Memory", memory_id="M-cli")
    session_id = "cli-stolen-lease"
    thread_id = "cli-stolen-lease-thread"
    ChatStore(str(database)).set_meta(session_id, title=session_id)
    registry = RuntimeRegistry(database)
    entered = threading.Event()
    release_input = threading.Event()
    review_actions: list[str] = []

    def blocking_input(_prompt: str) -> str:
        entered.set()
        assert release_input.wait(timeout=5)
        return "c"

    async with open_research_runtime(
        database,
        config={"runtime": {"lease_seconds": 60}},
        policy=_OfflinePolicy(),
        tools=[],
        memory_store=store,
    ) as runtime:
        original_review = runtime.review

        async def observed_review(thread_id: str, action: str, *args, **kwargs):
            review_actions.append(action)
            return await original_review(thread_id, action, *args, **kwargs)

        runtime.review = observed_review  # type: ignore[method-assign]
        task = asyncio.create_task(
            run_reviewed_workflow(
                runtime,
                "question",
                thread_id=thread_id,
                memory_id="M-cli",
                session_id=session_id,
                registry=registry,
                input_fn=blocking_input,
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        owned = registry.get(thread_id)
        assert owned is not None and owned.lease_owner is not None
        assert registry.release_lease(thread_id, owned.lease_owner)
        replacement = registry.claim_lease(thread_id, lease_seconds=60)
        assert replacement is not None
        release_input.set()
        with pytest.raises(RuntimeError, match="workflow lease lost"):
            await task
        assert registry.release_lease(thread_id, replacement)
        snapshot = await runtime.get_snapshot(thread_id)
        assert snapshot.values["workflow_status"] == "waiting_confirmation"

    assert review_actions == []
    assert registry.list_events(thread_id) == ()


@pytest.mark.asyncio
async def test_run_single_reconciles_completed_checkpoint_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("CLI Memory", memory_id="M-cli")
    thread_id = "single-completed"
    session_id = f"cli-single-{thread_id}"
    ChatStore(str(database)).set_meta(session_id, title=session_id)
    registry = RuntimeRegistry(database)
    expires_at = time.time() + 3600
    registry.register(
        task_id=thread_id,
        thread_id=thread_id,
        session_id=session_id,
        memory_id="M-cli",
        workflow_type="research",
        expires_at=expires_at,
    )
    config = {
        "chat": {"db_path": str(database)},
        "research": {"limits": {"max_iterations": 3}},
    }
    policy = _OfflinePolicy()

    async with open_research_runtime(
        database,
        config=config,
        policy=policy,
        tools=[],
        memory_store=store,
    ) as runtime:
        await runtime.start(
            "question",
            thread_id=thread_id,
            memory_id="M-cli",
            session_id=session_id,
            expires_at=expires_at,
        )
        completed = await runtime.review(thread_id, "confirm")
        assert completed["workflow_status"] == "completed"
    assert registry.list_events(thread_id) == ()

    real_open = open_research_runtime

    @asynccontextmanager
    async def reopened(path, config=None, **_kwargs):
        async with real_open(
            path,
            config=config,
            policy=policy,
            tools=[],
            memory_store=store,
        ) as runtime:
            yield runtime

    monkeypatch.setattr(run_single, "load_config", lambda _: config)
    monkeypatch.setattr(run_single, "open_research_runtime", reopened)
    output = await run_single._run(
        SimpleNamespace(
            config=None,
            query="question",
            thread_id=thread_id,
            memory_id="M-cli",
            yes=True,
        )
    )

    assert "Report:" in output
    assert [
        event.event_type for event in registry.list_events(thread_id)
    ] == ["confirmed", "completed"]


@pytest.mark.asyncio
async def test_run_single_expires_waiting_checkpoint_instead_of_confirming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "chat.db"
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("CLI Memory", memory_id="M-cli")
    thread_id = "single-expired"
    session_id = f"cli-single-{thread_id}"
    ChatStore(str(database)).set_meta(session_id, title=session_id)
    registry = RuntimeRegistry(database)
    expires_at = time.time() + 1.0
    registry.register(
        task_id=thread_id,
        thread_id=thread_id,
        session_id=session_id,
        memory_id="M-cli",
        workflow_type="research",
        expires_at=expires_at,
    )
    config = {
        "chat": {"db_path": str(database)},
        "runtime": {"proposal_ttl_seconds": 1.0, "lease_seconds": 60},
    }
    policy = _OfflinePolicy()
    async with open_research_runtime(
        database,
        config=config,
        policy=policy,
        tools=[],
        memory_store=store,
    ) as runtime:
        await runtime.start(
            "question",
            thread_id=thread_id,
            memory_id="M-cli",
            session_id=session_id,
            expires_at=expires_at,
        )
    await asyncio.sleep(1.05)

    real_open = open_research_runtime

    @asynccontextmanager
    async def reopened(path, config=None, **_kwargs):
        async with real_open(
            path,
            config=config,
            policy=policy,
            tools=[],
            memory_store=store,
        ) as runtime:
            yield runtime

    monkeypatch.setattr(run_single, "load_config", lambda _: config)
    monkeypatch.setattr(run_single, "open_research_runtime", reopened)
    with pytest.raises(RuntimeError, match="already expired"):
        await run_single._run(
            SimpleNamespace(
                config=None,
                query="question",
                thread_id=thread_id,
                memory_id="M-cli",
                yes=True,
            )
        )

    async with open_research_runtime(
        database,
        config=config,
        policy=policy,
        tools=[],
        memory_store=store,
    ) as runtime:
        snapshot = await runtime.get_snapshot(thread_id)
        assert snapshot.values["workflow_status"] == "expired"
    assert [event.event_type for event in registry.list_events(thread_id)] == [
        "expired"
    ]


@pytest.mark.asyncio
async def test_run_single_uses_configured_chat_database_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result()
    runtime = SimpleNamespace(
        memory_store=SimpleNamespace(root=tmp_path / "vault"),
        new_thread_id=lambda: "new-thread",
        get_memory_option=lambda memory_id: {"read_only": False},
    )
    opened: list[tuple[Path, dict[str, Any]]] = []
    exited: list[bool] = []

    @asynccontextmanager
    async def fake_open(path, config=None, **kwargs):
        opened.append((Path(path), config))
        try:
            yield runtime
        finally:
            exited.append(True)

    async def reviewed(*args, **kwargs):
        return result

    config = {"chat": {"db_path": str(tmp_path / "configured-chat.db")}}
    monkeypatch.setattr(run_single, "load_config", lambda _: config)
    monkeypatch.setattr(run_single, "open_research_runtime", fake_open)
    monkeypatch.setattr(run_single, "run_reviewed_workflow", reviewed)

    output = await run_single._run(
        SimpleNamespace(
            config=None,
            query="question",
            thread_id="existing-thread",
            memory_id="M-cli",
            yes=True,
        )
    )

    assert opened == [(tmp_path / "configured-chat.db", config)]
    assert exited == [True]
    assert "Report:" in output


@pytest.mark.asyncio
async def test_eval_and_benchmark_explicitly_inject_inmemory_savers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected: list[Any] = []

    class Runtime:
        def new_thread_id(self) -> str:
            return "eval-thread"

        async def run_auto_confirmed(self, *args, **kwargs):
            return _result(memory_id=None)

        async def close(self) -> None:
            return None

    def build(**kwargs):
        injected.append(kwargs.get("checkpointer"))
        return Runtime()

    class EmptyBench:
        def get_questions(self, **kwargs):
            return []

    monkeypatch.setattr(run_eval, "build_research_runtime", build)
    monkeypatch.setattr(run_eval, "ResearchBench", EmptyBench)
    await run_eval._evaluate_research_bench(0, None, {})

    monkeypatch.setattr("src.research.runtime.build_research_runtime", build)
    await run_benchmark.run_agent("question", {})

    assert len(injected) == 2
    assert all(isinstance(value, InMemorySaver) for value in injected)

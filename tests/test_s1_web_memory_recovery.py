"""S1 Web acceptance tests for checkpoint-owned Memory confirmations."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

import web.server as server
from src.research.memory import MarkdownMemoryStore
from src.research.runtime import build_research_runtime, open_research_runtime


class _MemoryPolicy:
    def __call__(self, messages, *, tools=None):
        del tools
        system = str(messages[0]["content"])
        user = str(messages[-1]["content"])
        if "before research begins" in system:
            return {
                "content": json.dumps(
                    {
                        "objective": "Recover the research brief",
                        "scope": ["checkpoint recovery"],
                        "directions": ["keep the same thread"],
                        "constraints": ["offline"],
                        "expected_output": "report",
                    }
                ),
                "tool_calls": [],
            }
        if "Answer only from" in system:
            context = json.loads(user.split("MEMORY_CONTEXT_JSON:\n", 1)[1])
            return {
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The checkpointed claim is grounded.",
                                "source_paths": [context["hits"][0]["path"]],
                            }
                        ],
                        "insufficient_evidence": [],
                    }
                )
            }
        if "FIXED_NOTE_CONTRACT_JSON" in user:
            contract = json.loads(
                user.split("FIXED_NOTE_CONTRACT_JSON:\n", 1)[1].split(
                    "\n\nMEMORY_ANSWER:", 1
                )[0]
            )
            fixed = contract["frontmatter"]
            sources = "\n".join(
                f"- [[{path[:-3]}]]" for path in contract["allowed_source_paths"]
            ) or "- None"
            markdown = (
                "---\n"
                f'id: {json.dumps(fixed["id"])}\n'
                f'type: {json.dumps(fixed["type"])}\n'
                f'memory_id: {json.dumps(fixed["memory_id"])}\n'
                'title: "Saved answer"\n'
                f'created_at: {json.dumps(fixed["created_at"])}\n'
                f'updated_at: {json.dumps(fixed["updated_at"])}\n'
                f'origin: {json.dumps(fixed["origin"])}\n'
                f'status: {json.dumps(fixed["status"])}\n'
                "tags:\n  - paperpilot\n"
                "---\n\n# Saved answer\n\nGrounded.\n\n"
                f"## Sources\n\n{sources}\n"
            )
            return {"content": json.dumps({"markdown": markdown})}
        context = json.loads(user.split("IMPORT_CONTEXT_JSON:\n", 1)[1])
        locator = context["excerpts"][0]["locator"]
        return {
            "content": json.dumps(
                {
                    "title": "Imported source",
                    "summary": "A checkpointed import.",
                    "support": [
                        {
                            "text": "The source supports one point.",
                            "locators": [locator],
                            "memory_paths": [],
                        }
                    ],
                    "conflicts": [],
                    "gaps": [],
                }
            )
        }


def _write_source(root: Path) -> None:
    path = root / "Memories/M-web/notes/N-source.md"
    path.write_text(
        (
            "---\n"
            'id: "N-source"\n'
            'type: "note"\n'
            'memory_id: "M-web"\n'
            'title: "Source"\n'
            'created_at: "2026-08-28T00:00:00+08:00"\n'
            'updated_at: "2026-08-28T00:00:00+08:00"\n'
            'origin: "user"\n'
            'status: "confirmed"\n'
            "tags:\n  - paperpilot\n"
            "---\n\n# Source\n\nThe checkpointed claim is grounded.\n"
        ),
        encoding="utf-8",
    )


def _write_legacy(root: Path) -> None:
    values = {
        "sources/Source-fixed.md": "---\nid: Source-fixed\ntype: source\n---\n\n# Source\n",
        "evidence/Evidence-fixed.md": (
            "---\nid: Evidence-fixed\ntype: evidence\n---\n\n"
            "# Evidence\n\n[[sources/Source-fixed]]\n"
        ),
        "reports/Report-fixed.md": (
            "---\nid: Report-fixed\ntype: report\n---\n\n"
            "# Report\n\n[[evidence/Evidence-fixed]]\n"
        ),
    }
    for relative, markdown in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")


@pytest.fixture()
def recovery_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault = tmp_path / "Vault"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Web", "M-web")
    _write_source(vault)
    _write_legacy(vault)
    saver = InMemorySaver()
    policy = _MemoryPolicy()
    config = {
        "research": {"limits": {"max_iterations": 3}},
        "runtime": {
            "proposal_ttl_seconds": 3600,
            "terminal_retention_seconds": 3600,
            "lease_seconds": 60,
            "sweep_interval_seconds": 5,
        },
    }

    def rebuild():
        runtime = build_research_runtime(
            config,
            policy=policy,
            tools=[],
            memory_store=store,
            checkpointer=saver,
        )
        server.get_research_runtime._runtime = runtime
        return runtime

    monkeypatch.setattr(server, "CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(server, "_config", {"research": {}})
    server.get_chat_store._store = None
    server.get_runtime_registry._registry = None
    initial_runtime = rebuild()
    with TestClient(server.app) as client:
        yield client, store, rebuild
    server.get_research_runtime._runtime = None
    server.get_chat_store._store = None
    server.get_runtime_registry._registry = None
    del initial_runtime


def test_note_answer_and_proposal_recover_without_process_caches(recovery_client) -> None:
    client, store, rebuild = recovery_client
    answer_response = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-note"},
    )
    assert answer_response.status_code == 200, answer_response.text
    answer = answer_response.json()
    assert answer["workflow_id"] == answer["task_id"] == answer["thread_id"]
    assert answer["expires_at"] > time.time()

    rebuild()
    proposal_response = client.post(
        "/api/memories/M-web/note-proposals",
        json={"answer_id": answer["answer_id"], "session_id": "session-note"},
    )
    assert proposal_response.status_code == 200, proposal_response.text
    proposal = proposal_response.json()
    assert proposal["workflow_id"] == answer["workflow_id"]
    assert not (store.root / proposal["target_path"]).exists()

    rebuild()
    confirmed = client.post(
        f'/api/memory-note-proposals/{proposal["proposal_id"]}/confirm',
        json={
            "session_id": "session-note",
            "memory_id": "M-web",
            "answer_id": answer["answer_id"],
            "proposal_id": proposal["proposal_id"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["workflow_id"] == answer["workflow_id"]
    assert (store.root / proposal["target_path"]).is_file()
    events = server.get_runtime_registry().list_events(answer["thread_id"])
    assert [event.event_type for event in events] == ["confirmed", "completed"]
    assert client.post(
        f'/api/memory-note-proposals/{proposal["proposal_id"]}/confirm'
    ).status_code == 409


def test_import_cancel_recovers_and_writes_only_cancelled_outbox(recovery_client) -> None:
    client, store, rebuild = recovery_client
    home_before = store.read_text("Memories/M-web/Home.md")
    response = client.post(
        "/api/memories/M-web/import-proposals",
        json={
            "kind": "text",
            "title": "Inline",
            "text": "checkpointed body",
            "session_id": "session-import",
        },
    )
    assert response.status_code == 200, response.text
    proposal = response.json()
    rebuild()
    cancelled = client.delete(
        f'/api/memories/M-web/import-proposals/{proposal["proposal_id"]}',
        params={"session_id": "session-import"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert store.read_text("Memories/M-web/Home.md") == home_before
    events = server.get_runtime_registry().list_events(proposal["thread_id"])
    assert [event.event_type for event in events] == ["cancelled"]
    assert events[0].payload == {"reason": "user_cancelled"}
    assert client.delete(
        f'/api/memories/M-web/import-proposals/{proposal["proposal_id"]}'
    ).status_code == 409


def test_legacy_confirmation_recovers_from_registry_and_checkpoint(recovery_client) -> None:
    client, store, rebuild = recovery_client
    response = client.post(
        "/api/legacy-memory/migration-proposals",
        json={
            "title": "Migrated",
            "target_memory_id": "M-migrated",
            "session_id": "session-legacy",
        },
    )
    assert response.status_code == 200, response.text
    proposal = response.json()
    rebuild()
    confirmed = client.post(
        f'/api/legacy-memory/migration-proposals/{proposal["proposal_id"]}/confirm',
        json={
            "session_id": "session-legacy",
            "memory_id": "M-legacy",
            "proposal_id": proposal["proposal_id"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["memory_id"] == "M-migrated"
    assert store.get_memory("M-migrated").title == "Migrated"
    events = server.get_runtime_registry().list_events(proposal["thread_id"])
    assert [event.event_type for event in events] == ["confirmed", "completed"]


def test_session_delete_removes_pending_checkpoint_registry_and_outbox(
    recovery_client,
) -> None:
    client, _store, _rebuild = recovery_client
    response = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-delete"},
    )
    assert response.status_code == 200, response.text
    workflow_id = response.json()["workflow_id"]
    assert server.get_runtime_registry().get(workflow_id) is not None

    deleted = client.delete("/api/sessions/session-delete")

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"]["workflows"] == 1
    assert server.get_runtime_registry().get(workflow_id) is None
    assert server.get_runtime_registry().list_events(workflow_id) == ()


def test_startup_quarantines_bad_locator_without_deleting_checkpoint(
    recovery_client,
) -> None:
    client, _store, _rebuild = recovery_client
    response = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-owner"},
    )
    assert response.status_code == 200, response.text
    workflow_id = response.json()["workflow_id"]
    server.get_chat_store().bind_memory("session-wrong", "M-web")
    connection = sqlite3.connect(server.CHAT_DB_PATH)
    try:
        connection.execute(
            "UPDATE runtime_workflows SET session_id = ? WHERE task_id = ?",
            ("session-wrong", workflow_id),
        )
        connection.commit()
    finally:
        connection.close()

    asyncio.run(server._restore_registered_workflows())

    assert server.get_runtime_registry().get(workflow_id) is None
    snapshot = asyncio.run(
        server.get_research_runtime().get_workflow_snapshot(
            "memory_note", workflow_id
        )
    )
    assert snapshot.values["session_id"] == "session-owner"


def test_startup_rebuilds_missing_outbox_from_terminal_checkpoint(
    recovery_client,
) -> None:
    client, _store, _rebuild = recovery_client
    answer = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-outbox"},
    ).json()
    proposal = client.post(
        "/api/memories/M-web/note-proposals",
        json={"answer_id": answer["answer_id"], "session_id": "session-outbox"},
    ).json()
    confirmed = client.post(
        f'/api/memory-note-proposals/{proposal["proposal_id"]}/confirm',
        json={
            "session_id": "session-outbox",
            "memory_id": "M-web",
            "answer_id": answer["answer_id"],
            "proposal_id": proposal["proposal_id"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text

    connection = sqlite3.connect(server.CHAT_DB_PATH)
    try:
        connection.execute(
            "DELETE FROM runtime_outbox WHERE thread_id = ?",
            (proposal["thread_id"],),
        )
        connection.commit()
    finally:
        connection.close()
    assert server.get_runtime_registry().list_events(proposal["thread_id"]) == ()

    asyncio.run(server._restore_registered_workflows())

    assert [
        event.event_type
        for event in server.get_runtime_registry().list_events(proposal["thread_id"])
    ] == ["confirmed", "completed"]


def test_startup_keeps_locator_when_checkpoint_read_temporarily_fails(
    recovery_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, _rebuild = recovery_client
    answer = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-transient"},
    ).json()
    registry = server.get_runtime_registry()
    runtime = server.get_research_runtime()

    async def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is temporarily busy")

    monkeypatch.setattr(runtime, "get_workflow_snapshot", unavailable)
    asyncio.run(server._restore_registered_workflows())

    assert registry.get(answer["workflow_id"]) is not None


def test_failure_outbox_reconciliation_uses_checkpoint_failure_code(
    recovery_client,
) -> None:
    client, _store, _rebuild = recovery_client
    aligned = client.post(
        "/api/alignment",
        json={
            "session_id": "session-failed-outbox",
            "memory_id": "M-web",
            "message": "Persist one stable failure code",
        },
    ).json()
    runtime = server.get_research_runtime()
    record = server.get_runtime_registry().get(aligned["task_id"])
    assert record is not None
    asyncio.run(runtime.mark_workflow_failed("research", record.thread_id, "runtimeerror"))
    snapshot = asyncio.run(runtime.get_workflow_snapshot("research", record.thread_id))
    server._reconcile_workflow_outbox(record, snapshot)

    asyncio.run(server._restore_registered_workflows())
    events = server.get_runtime_registry().list_events(record.thread_id)
    assert [(event.event_type, event.payload) for event in events] == [
        ("failed", {"code": "runtimeerror"})
    ]
    response = client.get(f"/api/tasks/{record.task_id}/events")
    assert response.status_code == 200
    assert '"status": "failed"' in response.text
    assert '"code": "runtimeerror"' in response.text


def test_outbox_write_failure_never_overwrites_committed_memory_state(
    recovery_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, _rebuild = recovery_client
    answer = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-outbox-fail"},
    ).json()
    proposal = client.post(
        "/api/memories/M-web/note-proposals",
        json={"answer_id": answer["answer_id"], "session_id": "session-outbox-fail"},
    ).json()
    registry = server.get_runtime_registry()
    original_append = registry.append_event

    def fail_completed(thread_id, event_type, payload=None, **kwargs):
        if event_type == "completed":
            raise sqlite3.OperationalError("simulated outbox failure")
        return original_append(thread_id, event_type, payload, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(registry, "append_event", fail_completed)
        with pytest.raises(sqlite3.OperationalError, match="outbox failure"):
            client.post(
                f'/api/memory-note-proposals/{proposal["proposal_id"]}/confirm',
                json={
                    "session_id": "session-outbox-fail",
                    "memory_id": "M-web",
                    "answer_id": answer["answer_id"],
                    "proposal_id": proposal["proposal_id"],
                },
            )

    record = registry.get(proposal["workflow_id"])
    assert record is not None
    snapshot = asyncio.run(
        server.get_research_runtime().get_workflow_snapshot(
            "memory_note", proposal["workflow_id"]
        )
    )
    assert snapshot.values["workflow_status"] == "committed"
    asyncio.run(server._restore_registered_workflows())
    assert [event.event_type for event in registry.list_events(record.thread_id)] == [
        "confirmed",
        "completed",
    ]


def test_concurrent_note_confirmation_has_one_commit_and_one_conflict(
    recovery_client,
) -> None:
    client, store, _rebuild = recovery_client
    answer = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-race"},
    ).json()
    proposal = client.post(
        "/api/memories/M-web/note-proposals",
        json={"answer_id": answer["answer_id"], "session_id": "session-race"},
    ).json()

    def confirm() -> int:
        return client.post(
            f'/api/memory-note-proposals/{proposal["proposal_id"]}/confirm',
            json={
                "session_id": "session-race",
                "memory_id": "M-web",
                "answer_id": answer["answer_id"],
                "proposal_id": proposal["proposal_id"],
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _value: confirm(), range(2)))
    assert statuses == [200, 409]
    assert (store.root / proposal["target_path"]).is_file()


def test_lease_guard_renews_until_executor_finishes(recovery_client) -> None:
    client, _store, _rebuild = recovery_client
    answer = client.post(
        "/api/memories/M-web/answers",
        json={"question": "What is grounded?", "session_id": "session-lease"},
    ).json()
    registry = server.get_runtime_registry()
    record = registry.get(answer["workflow_id"])
    assert record is not None
    token = registry.claim_lease(record.task_id, lease_seconds=0.15)
    assert token is not None

    async def hold() -> None:
        async with server._workflow_lease(record, token, 0.15):
            await asyncio.sleep(0.4)
            assert registry.claim_lease(record.task_id, lease_seconds=0.15) is None

    asyncio.run(hold())
    assert registry.claim_lease(record.task_id, lease_seconds=0.15) is not None


def test_research_brief_is_rebuilt_from_checkpoint_after_runtime_restart(
    recovery_client,
) -> None:
    client, _store, rebuild = recovery_client
    aligned = client.post(
        "/api/alignment",
        json={
            "session_id": "session-research",
            "memory_id": "M-web",
            "message": "Research checkpoint recovery",
        },
    )
    assert aligned.status_code == 200, aligned.text
    task_id = aligned.json()["task_id"]

    server._TASKS.clear()
    rebuild()
    asyncio.run(server._restore_registered_workflows())

    restored = client.get("/api/sessions/session-research/active-task")
    assert restored.status_code == 200, restored.text
    payload = restored.json()
    assert payload["task_id"] == task_id
    assert payload["status"] == "waiting_confirmation"
    assert payload["brief"]["objective"] == "Recover the research brief"


def test_research_http_confirmation_persists_before_background_slot(
    recovery_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, _rebuild = recovery_client
    aligned = client.post(
        "/api/alignment",
        json={
            "session_id": "session-confirm-step",
            "memory_id": "M-web",
            "message": "Persist confirmation before execution",
        },
    ).json()
    monkeypatch.setattr(server, "_RUN_SEMAPHORE", asyncio.Semaphore(0))

    started = client.post(
        "/api/research",
        json={
            "task_id": aligned["task_id"],
            "session_id": "session-confirm-step",
        },
    )

    assert started.status_code == 200, started.text
    record = server.get_runtime_registry().get(aligned["task_id"])
    assert record is not None
    snapshot = asyncio.run(
        server.get_research_runtime().get_workflow_snapshot(
            "research", aligned["thread_id"]
        )
    )
    assert snapshot.values["confirmed"] is True
    assert server.derive_workflow_status(record, snapshot) == "running"
    assert [
        event.event_type
        for event in server.get_runtime_registry().list_events(aligned["thread_id"])
    ] == ["confirmed"]


def test_session_delete_terminally_cancels_running_checkpoint_before_db_delete(
    recovery_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, _rebuild = recovery_client
    aligned = client.post(
        "/api/alignment",
        json={
            "session_id": "session-delete-running",
            "memory_id": "M-web",
            "message": "Delete a running workflow safely",
        },
    ).json()
    monkeypatch.setattr(server, "_RUN_SEMAPHORE", asyncio.Semaphore(0))
    started = client.post(
        "/api/research",
        json={
            "task_id": aligned["task_id"],
            "session_id": "session-delete-running",
        },
    )
    assert started.status_code == 200, started.text

    store = server.get_chat_store()

    def fail_delete(*_args, **_kwargs):
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(store, "delete_session_with_workflow_leases", fail_delete)
    deleted = client.delete("/api/sessions/session-delete-running")

    assert deleted.status_code == 409
    record = server.get_runtime_registry().get(aligned["task_id"])
    assert record is not None
    snapshot = asyncio.run(
        server.get_research_runtime().get_workflow_snapshot(
            "research", aligned["thread_id"]
        )
    )
    assert server.derive_workflow_status(record, snapshot) == "cancelled"
    assert [
        event.event_type
        for event in server.get_runtime_registry().list_events(aligned["thread_id"])
    ] == ["confirmed", "cancelled"]


def test_fastapi_lifespan_reopens_sqlite_and_confirms_same_note_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "paperpilot.db"
    store = MarkdownMemoryStore(tmp_path / "Vault")
    store.create_memory("Web", "M-web")
    _write_source(store.root)
    policy = _MemoryPolicy()
    config = {
        "research": {"limits": {"max_iterations": 3}},
        "runtime": {
            "proposal_ttl_seconds": 3600,
            "terminal_retention_seconds": 3600,
            "lease_seconds": 0.3,
            "sweep_interval_seconds": 0.05,
        },
    }

    @asynccontextmanager
    async def persistent_runtime(path, *, config):
        async with open_research_runtime(
            path,
            config=config,
            policy=policy,
            tools=[],
            memory_store=store,
        ) as runtime:
            yield runtime

    monkeypatch.setattr(server, "CHAT_DB_PATH", str(database))
    monkeypatch.setattr(server, "_config", config)
    monkeypatch.setattr(server, "open_research_runtime", persistent_runtime)
    server.get_chat_store._store = None
    server.get_runtime_registry._registry = None
    server.get_research_runtime._runtime = None
    server._TASKS.clear()

    with TestClient(server.app) as first:
        answer = first.post(
            "/api/memories/M-web/answers",
            json={"question": "What is grounded?", "session_id": "sqlite-web"},
        ).json()
        proposal_response = first.post(
            "/api/memories/M-web/note-proposals",
            json={"answer_id": answer["answer_id"], "session_id": "sqlite-web"},
        )
        assert proposal_response.status_code == 200, proposal_response.text
        proposal = proposal_response.json()

    assert not hasattr(server.get_research_runtime, "_runtime")
    with TestClient(server.app) as second:
        workflows = second.get("/api/sessions/sqlite-web/workflows").json()
        assert workflows[0]["workflow_id"] == proposal["workflow_id"]
        confirmed = second.post(
            f'/api/memory-note-proposals/{proposal["proposal_id"]}/confirm',
            json={
                "session_id": "sqlite-web",
                "memory_id": "M-web",
                "answer_id": answer["answer_id"],
                "proposal_id": proposal["proposal_id"],
            },
        )
        assert confirmed.status_code == 200, confirmed.text

    assert (store.root / proposal["target_path"]).is_file()
    server.get_chat_store._store = None
    server.get_runtime_registry._registry = None
    server._TASKS.clear()

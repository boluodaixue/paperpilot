"""S1 tests for the thin SQLite Runtime Registry and minimal outbox."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from src.memory.chat_store import ChatStore
from src.research.runtime_registry import RuntimeRegistry


_WORKFLOW_COLUMNS = {
    "task_id",
    "thread_id",
    "session_id",
    "memory_id",
    "workflow_type",
    "created_at",
    "expires_at",
    "lease_owner",
    "lease_until",
}
_OUTBOX_COLUMNS = {
    "event_id",
    "thread_id",
    "sequence",
    "event_type",
    "payload_json",
}


def _database(tmp_path: Path) -> tuple[Path, RuntimeRegistry]:
    path = tmp_path / "runtime.db"
    chat = ChatStore(str(path))
    chat.bind_memory("session-a", "M-A")
    chat.bind_memory("session-b", "M-B")
    return path, RuntimeRegistry(path)


def _register(
    registry: RuntimeRegistry,
    *,
    suffix: str = "a",
    session_id: str = "session-a",
    memory_id: str = "M-A",
    workflow_type: str = "research",
):
    return registry.register(
        task_id=f"task-{suffix}",
        thread_id=f"thread-{suffix}",
        session_id=session_id,
        memory_id=memory_id,
        workflow_type=workflow_type,
        created_at=100.0,
        expires_at=200.0,
    )


def test_schema_contains_only_thin_registry_and_minimal_outbox_columns(
    tmp_path: Path,
) -> None:
    path, registry = _database(tmp_path)
    _register(registry)
    connection = sqlite3.connect(path)
    try:
        workflow_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runtime_workflows)")
        }
        outbox_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runtime_outbox)")
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert workflow_columns == _WORKFLOW_COLUMNS
    assert outbox_columns == _OUTBOX_COLUMNS
    assert journal_mode.lower() == "wal"
    forbidden = {
        "status", "brief", "proposal", "answer", "result", "error",
        "markdown", "content", "attachment_bytes", "payload",
    }
    assert not workflow_columns.intersection(forbidden)
    assert not outbox_columns.intersection(forbidden)


def test_register_is_idempotent_but_identity_and_session_switches_are_rejected(
    tmp_path: Path,
) -> None:
    _path, registry = _database(tmp_path)
    first = _register(registry)
    repeated = _register(registry)
    assert repeated == first
    assert registry.get("task-a") == first
    assert registry.get_by_thread("thread-a") == first
    assert registry.list(session_id="session-a") == (first,)

    with pytest.raises(ValueError, match="identity collision"):
        registry.register(
            task_id="task-a",
            thread_id="thread-other",
            session_id="session-a",
            memory_id="M-A",
            workflow_type="research",
        )
    with pytest.raises(ValueError, match="session binding"):
        registry.register(
            task_id="task-wrong-memory",
            thread_id="thread-wrong-memory",
            session_id="session-a",
            memory_id="M-B",
            workflow_type="research",
        )
    with pytest.raises(ValueError, match="session does not exist"):
        registry.register(
            task_id="task-orphan",
            thread_id="thread-orphan",
            session_id="missing",
            memory_id="M-A",
            workflow_type="research",
        )


def test_concurrent_lease_claim_has_one_winner_and_token_controls_renew_release(
    tmp_path: Path,
) -> None:
    path, first_registry = _database(tmp_path)
    _register(first_registry)
    second_registry = RuntimeRegistry(path)
    barrier = threading.Barrier(2)
    results: list[str | None] = []

    def claim(registry: RuntimeRegistry) -> None:
        barrier.wait()
        results.append(registry.claim_lease("task-a", now=100.0, lease_seconds=10.0))

    first = threading.Thread(target=claim, args=(first_registry,))
    second = threading.Thread(target=claim, args=(second_registry,))
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    tokens = [item for item in results if item is not None]
    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
    assert len(tokens) == 1
    token = tokens[0]
    assert token.startswith("lease-")
    assert not first_registry.renew_lease(
        "task-a", "lease-wrong", now=101.0, lease_seconds=10.0
    )
    assert first_registry.renew_lease(
        "task-a", token, now=101.0, lease_seconds=10.0
    )
    assert not second_registry.claim_lease(
        "task-a", now=110.0, lease_seconds=10.0
    )
    assert first_registry.release_lease("task-a", token)
    assert not first_registry.release_lease("task-a", token)
    replacement = second_registry.claim_lease(
        "task-a", now=110.0, lease_seconds=10.0
    )
    assert replacement is not None and replacement != token


def test_expired_lease_can_be_claimed_but_cannot_be_renewed_by_old_token(
    tmp_path: Path,
) -> None:
    path, registry = _database(tmp_path)
    _register(registry)
    old = registry.claim_lease("task-a", now=10.0, lease_seconds=5.0)
    assert old is not None
    other = RuntimeRegistry(path)
    replacement = other.claim_lease("task-a", now=15.0, lease_seconds=5.0)
    assert replacement is not None and replacement != old
    assert not registry.renew_lease(
        "task-a", old, now=15.0, lease_seconds=5.0
    )
    assert not registry.release_lease("task-a", old)


def test_outbox_is_minimal_ordered_idempotent_and_cascades_with_workflow(
    tmp_path: Path,
) -> None:
    _path, registry = _database(tmp_path)
    _register(registry)
    confirmed = registry.append_event("thread-a", "confirmed")
    repeated = registry.append_event("thread-a", "confirmed")
    failed = registry.append_event(
        "thread-a",
        "failed",
        {"code": "policy_error"},
        event_id="failure-event",
    )

    assert repeated == confirmed
    assert confirmed.sequence == 1
    assert confirmed.payload == {}
    assert failed.sequence == 2
    assert failed.payload == {"code": "policy_error"}
    assert registry.list_events("thread-a") == (confirmed, failed)
    assert registry.list_events("thread-a", after_sequence=1) == (failed,)
    with pytest.raises(ValueError, match="non-minimal"):
        registry.append_event(
            "thread-a", "completed", {"markdown": "must not persist"}
        )
    with pytest.raises(ValueError, match="collision"):
        registry.append_event(
            "thread-a",
            "failed",
            {"code": "different_error"},
            event_id="failure-event",
        )

    assert registry.delete("task-a")
    assert registry.get("task-a") is None
    assert registry.list_events("thread-a") == ()
    assert not registry.delete("task-a")


def test_session_delete_cascades_registry_and_outbox_when_foreign_keys_are_on(
    tmp_path: Path,
) -> None:
    path, registry = _database(tmp_path)
    _register(registry)
    registry.append_event("thread-a", "confirmed")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM session_meta WHERE session_id = ?", ("session-a",))
        connection.commit()
    finally:
        connection.close()

    assert registry.get("task-a") is None
    assert registry.list_events("thread-a") == ()


def test_coordinated_session_delete_requires_exact_live_workflow_leases(
    tmp_path: Path,
) -> None:
    path, registry = _database(tmp_path)
    _register(registry)
    chat = ChatStore(str(path))
    token = registry.claim_lease("task-a", now=100.0, lease_seconds=10.0)
    assert token is not None

    with pytest.raises(RuntimeError, match="exact|changed"):
        chat.delete_session_with_workflow_leases("session-a", {}, now=101.0)
    with pytest.raises(RuntimeError, match="lease"):
        chat.delete_session_with_workflow_leases(
            "session-a", {"task-a": "lease-wrong"}, now=101.0
        )

    deleted, threads = chat.delete_session_with_workflow_leases(
        "session-a", {"task-a": token}, now=101.0
    )
    assert deleted == 0
    assert threads == ("thread-a",)
    assert registry.get("task-a") is None
    assert registry.list_events("thread-a") == ()

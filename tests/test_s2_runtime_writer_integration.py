from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import src.research.runtime as runtime_module
from src.research.memory import MarkdownMemoryStore
from src.research.memory_write_plans import build_create_memory_plan
from src.research.models import MemoryDescriptor
from src.research.runtime import build_research_runtime, open_research_runtime


class _Policy:
    async def ainvoke(self, *_args, **_kwargs):
        return {"content": "unused", "tool_calls": []}


def _config(vault: Path, database: Path) -> dict:
    return {
        "research": {
            "vault_root": str(vault),
            "report_review": {"enabled": False},
            "limits": {},
        },
        "chat": {"db_path": str(database)},
        "runtime": {
            "lease_seconds": 5,
            "writer_coordination_interval_seconds": 0.01,
        },
        "tools": {"enabled": []},
    }


def _runtime(vault: Path, database: Path):
    return build_research_runtime(
        _config(vault, database),
        policy=_Policy(),
        tools=[],
        memory_store=MarkdownMemoryStore(vault),
        checkpointer=InMemorySaver(),
        write_db_path=database,
    )


def test_build_runtime_uses_persistent_queue_and_stable_vault_scope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    first = _runtime(tmp_path / "vault-a", database)
    same = _runtime(tmp_path / "vault-a", database)
    other = _runtime(tmp_path / "vault-b", database)

    assert first.vault_write_service.queue.db_path == str(database)
    assert first.vault_write_service.queue.vault_scope == same.vault_write_service.queue.vault_scope
    assert first.vault_write_service.queue.vault_scope != other.vault_write_service.queue.vault_scope
    assert first.vault_write_service.writer.root == first.memory_store.root

    first.create_memory("Vault A", "M-vault-a")
    assert first.vault_write_service.queue.list(status="succeeded")
    assert same.vault_write_service.queue.list(status="succeeded")
    assert other.vault_write_service.queue.list() == ()


@pytest.mark.asyncio
async def test_open_runtime_recovers_queued_write_from_same_checkpoint_database(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    database = tmp_path / "paperpilot.sqlite3"
    initial = _runtime(vault, database)
    plan = build_create_memory_plan(
        memory_id="M-restart",
        title="Restarted",
        created_at="2026-08-28T12:00:00+08:00",
        origin_thread_id="thread-restart",
    )
    queued = initial.vault_write_service.queue.enqueue(**plan.enqueue_kwargs())
    assert queued.status == "queued"
    assert not (vault / "Memories" / "M-restart").exists()

    async with open_research_runtime(
        database,
        _config(vault, database),
        policy=_Policy(),
        tools=[],
        memory_store=MarkdownMemoryStore(vault),
    ) as restarted:
        recovered = restarted.vault_write_service.queue.get(queued.job_id)
        assert recovered is not None and recovered.status == "succeeded"
        descriptor = restarted.get_memory("M-restart")
        assert descriptor.title == "Restarted"
        assert (vault / descriptor.relative_path / "Home.md").is_file()


def test_runtime_injects_one_service_into_all_workflow_builders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def builder(name: str):
        def capture(*_args, **kwargs):
            captured[name] = kwargs.get("vault_write_service")
            return SimpleNamespace(name=name)

        return capture

    monkeypatch.setattr(runtime_module, "build_research_workflow", builder("research"))
    monkeypatch.setattr(runtime_module, "build_memory_note_workflow", builder("note"))
    monkeypatch.setattr(runtime_module, "build_memory_import_workflow", builder("import"))
    monkeypatch.setattr(runtime_module, "build_legacy_migration_workflow", builder("legacy"))
    runtime = _runtime(tmp_path / "vault", tmp_path / "runtime.sqlite3")
    service = runtime.vault_write_service
    assert captured == {
        "research": service,
        "note": service,
        "import": service,
        "legacy": service,
    }


def test_runtime_direct_managed_mutations_route_to_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "vault", tmp_path / "runtime.sqlite3")
    service = runtime.vault_write_service
    calls: list[str] = []
    descriptor = MemoryDescriptor(
        memory_id="M-routed",
        title="Routed",
        relative_path="Memories/M-routed/",
        created_at="2026-08-28T12:00:00+08:00",
        updated_at="2026-08-28T12:00:00+08:00",
    )
    monkeypatch.setattr(
        service,
        "create_memory",
        lambda *_args, **_kwargs: (calls.append("create") or descriptor),
    )
    monkeypatch.setattr(
        service,
        "commit_memory_note",
        lambda _proposal: (
            calls.append("note")
            or {
                "memory_id": "M-routed",
                "target_path": "Memories/M-routed/notes/Note-one.md",
                "home_path": "Memories/M-routed/Home.md",
                "wikilink": "[[Memories/M-routed/notes/Note-one]]",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "commit_memory_import",
        lambda _proposal: (calls.append("import") or {"status": "committed"}),
    )
    monkeypatch.setattr(
        service,
        "commit_legacy_memory_migration",
        lambda _proposal: (calls.append("legacy") or descriptor),
    )

    assert runtime.create_memory("Routed", "M-routed") == descriptor
    note = SimpleNamespace(memory_id="M-routed")
    assert runtime.commit_memory_note(note)["memory_id"] == "M-routed"
    imported = SimpleNamespace(memory_id="M-routed")
    assert runtime.commit_memory_import(imported)["status"] == "committed"
    assert runtime.commit_legacy_memory_migration({"target_memory_id": "M-routed"}) == descriptor
    assert calls == ["create", "note", "import", "legacy"]
    assert asdict(descriptor)["memory_id"] == "M-routed"

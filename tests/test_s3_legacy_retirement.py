"""S3 acceptance tests for safe legacy retirement and historical pointers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.memory.chat_store import KIND_REPORT, ChatStore
from src.research.memory import MarkdownMemoryStore
from src.research.runtime import build_research_runtime
from src.research.vault import LEGACY_MEMORY_ID, scan_legacy_memory_markdown
from src.research.vault_writer import VaultWriter


def _runtime(tmp_path: Path, *, archive: bool = True):
    vault = tmp_path / "Vault"
    archive_root = tmp_path / "Archive"
    if archive:
        archive_root.mkdir()
    database = tmp_path / "chat.db"
    chat = ChatStore(str(database))
    store = MarkdownMemoryStore(vault)
    config = {
        "research": {
            "limits": {"max_iterations": 2},
            **({"legacy_archive_root": str(archive_root)} if archive else {}),
        },
        "chat": {"db_path": str(database)},
    }
    runtime = build_research_runtime(
        config,
        policy=lambda _messages, **_kwargs: {"content": "{}", "tool_calls": []},
        tools=[],
        memory_store=store,
        checkpointer=InMemorySaver(),
        write_db_path=database,
    )
    return runtime, chat, archive_root


def _legacy_report(store: MarkdownMemoryStore) -> Path:
    report = store.root / "reports" / "Report-old.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Old report\n\nLegacy fact.\n", encoding="utf-8")
    return report


def test_s3_retires_active_legacy_rebinds_sessions_and_resolves_old_manifest(
    tmp_path: Path,
) -> None:
    runtime, chat, _ = _runtime(tmp_path)
    report = _legacy_report(runtime.memory_store)
    for session_id in ("initiator", "other"):
        chat.bind_memory(session_id, LEGACY_MEMORY_ID)
    pointer = json.dumps(
        {
            "memory_id": None,
            "manifest": {
                "report_path": "reports/Report-old.md",
                "evidence_paths": [],
                "source_paths": [],
            },
        },
        sort_keys=True,
    )
    chat.add("other", "assistant", KIND_REPORT, pointer)
    raw_before = chat.get_messages("other")

    proposal = runtime.prepare_legacy_memory_migration("Migrated", "M-retired")
    retirement = proposal["retirement"]
    assert retirement["affected_sessions"] == ["initiator", "other"]
    assert retirement["path_mapping"] == {
        "reports/Report-old.md": "Memories/M-retired/reports/Report-old.md"
    }
    archive_target = Path(retirement["archive_target"])
    assert not archive_target.exists()

    descriptor = runtime.commit_legacy_memory_migration(proposal)

    assert descriptor.memory_id == "M-retired"
    assert not report.exists()
    assert scan_legacy_memory_markdown(runtime.memory_store.root) == ()
    assert archive_target.joinpath("reports", "Report-old.md").is_file()
    assert chat.get_memory_binding("initiator") == "M-retired"
    assert chat.get_memory_binding("other") == "M-retired"
    assert chat.get_messages("other") == raw_before
    assert "Legacy fact." in runtime.read_memory("reports/Report-old.md")
    assert all(
        item["memory_id"] != LEGACY_MEMORY_ID
        for item in runtime.list_memory_options()
    )
    cleanup = runtime.prepare_legacy_archive_cleanup(str(proposal["proposal_id"]))
    assert "reports/Report-old.md" in cleanup["delete_paths"]
    with pytest.raises(Exception):
        runtime.delete_legacy_archive(
            str(proposal["proposal_id"]), confirmation_token="stale"
        )
    assert archive_target.is_dir()
    deleted = runtime.delete_legacy_archive(
        str(proposal["proposal_id"]),
        confirmation_token=str(cleanup["confirmation_token"]),
    )
    assert deleted["status"] == "deleted"
    assert not archive_target.exists()


def test_s3_missing_explicit_archive_root_refuses_before_writes(tmp_path: Path) -> None:
    runtime, _chat, _ = _runtime(tmp_path, archive=False)
    report = _legacy_report(runtime.memory_store)

    proposal = runtime.prepare_legacy_memory_migration("Migrated", "M-no-archive")
    assert "legacy_archive_root" in proposal["retirement"]["blocked_reason"]
    with pytest.raises(ValueError, match="legacy_archive_root"):
        runtime.commit_legacy_memory_migration(proposal)

    assert report.is_file()
    assert not (runtime.memory_store.root / "Memories" / "M-no-archive").exists()


def test_s3_archive_collision_and_source_change_leave_legacy_active(tmp_path: Path) -> None:
    runtime, chat, _ = _runtime(tmp_path)
    report = _legacy_report(runtime.memory_store)
    chat.bind_memory("session", LEGACY_MEMORY_ID)
    proposal = runtime.prepare_legacy_memory_migration("Migrated", "M-conflict")
    report.write_text("# Old report\n\nExternally changed.\n", encoding="utf-8")

    with pytest.raises(Exception):
        runtime.commit_legacy_memory_migration(proposal)

    assert report.is_file()
    assert chat.get_memory_binding("session") == LEGACY_MEMORY_ID
    assert not (runtime.memory_store.root / "Memories" / "M-conflict").exists()


def test_s3_archive_target_collision_never_moves_legacy(tmp_path: Path) -> None:
    runtime, chat, _ = _runtime(tmp_path)
    report = _legacy_report(runtime.memory_store)
    chat.bind_memory("session", LEGACY_MEMORY_ID)
    proposal = runtime.prepare_legacy_memory_migration("Migrated", "M-archive-collision")
    archive_target = Path(proposal["retirement"]["archive_target"])
    archive_target.mkdir(parents=True)
    (archive_target / "foreign.txt").write_text("foreign", encoding="utf-8")

    with pytest.raises(Exception):
        runtime.commit_legacy_memory_migration(proposal)

    assert report.is_file()
    assert chat.get_memory_binding("session") == LEGACY_MEMORY_ID
    assert (archive_target / "foreign.txt").read_text(encoding="utf-8") == "foreign"


def test_s3_recovers_after_switch_before_writer_completion(tmp_path: Path) -> None:
    runtime, chat, archive_root = _runtime(tmp_path)
    report = _legacy_report(runtime.memory_store)
    chat.bind_memory("session", LEGACY_MEMORY_ID)
    proposal = runtime.prepare_legacy_memory_migration("Migrated", "M-recovered")
    service = runtime.vault_write_service
    fired = False

    def failpoint(name: str) -> None:
        nonlocal fired
        if name == "after_linearized" and not fired:
            fired = True
            raise RuntimeError("forced stop after switch")

    service.writer = VaultWriter(
        runtime.memory_store.root,
        service.queue,
        failpoint=failpoint,
        legacy_archive_root=archive_root,
    )
    with pytest.raises(RuntimeError, match="durable command was retained"):
        runtime.commit_legacy_memory_migration(proposal)

    assert not report.exists()
    assert chat.get_memory_binding("session") == "M-recovered"
    service.writer = VaultWriter(
        runtime.memory_store.root,
        service.queue,
        legacy_archive_root=archive_root,
    )
    descriptor = runtime.commit_legacy_memory_migration(proposal)
    assert descriptor.memory_id == "M-recovered"
    assert runtime.read_memory("reports/Report-old.md").endswith("Legacy fact.\n")

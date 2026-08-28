"""W6 copy-on-publish migration tests for the legacy root Memory."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import web.server as server
from src.memory.chat_store import KIND_REPORT, ChatStore
from src.research.memory import MarkdownMemoryStore, MemoryWriteConflictError
from src.research.vault import validate_frontmatter


def _write_legacy(vault: Path) -> dict[str, bytes]:
    contents = {
        "sources/Source-fixed.md": (
            "---\nid: \"Source-fixed\"\ntype: \"source\"\n---\n\n"
            "# Primary source\n\n- Reference: https://example.test/source\n"
        ),
        "evidence/raw_under.score.md": (
            "---\nid: \"raw_under.score\"\ntype: \"evidence\"\n---\n\n"
            "# Evidence: fixed finding\n\n"
            "[[sources/Source-fixed|Primary source]]\n"
        ),
        "reports/Report-fixed.md": (
            "---\nid: \"Report-fixed\"\ntype: \"report\"\n"
            "root_thread_id: \"legacy-root\"\n---\n\n"
            "# Legacy report\n\n"
            "Supported by [[evidence/raw_under.score|Evidence]].\n"
        ),
    }
    for relative_path, markdown in contents.items():
        path = vault / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8", newline="\n")
    return {
        relative_path: (vault / relative_path).read_bytes()
        for relative_path in contents
    }


def _frontmatter(markdown: str) -> dict[str, object]:
    lines = markdown.splitlines()
    closing = lines.index("---", 1)
    import yaml

    value = yaml.safe_load("\n".join(lines[1:closing]))
    assert isinstance(value, dict)
    return validate_frontmatter(value)


def _assert_source_unchanged(vault: Path, original: dict[str, bytes]) -> None:
    assert {
        relative_path: (vault / relative_path).read_bytes()
        for relative_path in original
    } == original


def test_preview_is_zero_write_and_contains_complete_safe_conversion(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    original = _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    before = sorted(path.relative_to(vault) for path in vault.rglob("*"))

    proposal = store.prepare_legacy_memory_migration(
        "Migrated legacy research",
        "M-migrated",
    )

    assert sorted(path.relative_to(vault) for path in vault.rglob("*")) == before
    _assert_source_unchanged(vault, original)
    assert proposal["source_memory_id"] == "M-legacy"
    assert proposal["target_memory_id"] == "M-migrated"
    assert proposal["target_relative_path"] == "Memories/M-migrated/"
    assert len(proposal["source_content_hash"]) == 64
    files = {item["source_path"]: item for item in proposal["files"]}
    assert set(files) == set(original)
    evidence = files["evidence/raw_under.score.md"]
    assert evidence["target_path"].startswith("Memories/M-migrated/evidence/Evidence-")
    assert "[[Memories/M-migrated/sources/Source-fixed|Primary source]]" in evidence["markdown"]
    report = files["reports/Report-fixed.md"]
    assert f"[[{evidence['target_path'][:-3]}|Evidence]]" in report["markdown"]
    assert report["wikilink"] in proposal["home_markdown"]
    for item in files.values():
        frontmatter = _frontmatter(item["markdown"])
        assert frontmatter["memory_id"] == "M-migrated"
        assert frontmatter["origin"] == "research"
        assert frontmatter["status"] == "confirmed"


def test_confirm_publishes_one_complete_managed_copy_and_keeps_legacy(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    original = _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    proposal = store.prepare_legacy_memory_migration(
        "Migrated legacy research",
        "M-migrated",
    )

    descriptor = store.commit_legacy_memory_migration(proposal)

    assert descriptor.memory_id == "M-migrated"
    assert descriptor.title == "Migrated legacy research"
    assert store.get_memory("M-migrated") == descriptor
    memory_root = vault / "Memories" / "M-migrated"
    assert sorted(path.name for path in memory_root.iterdir()) == [
        "Home.md",
        "attachments",
        "evidence",
        "imports",
        "notes",
        "reports",
        "sources",
    ]
    for item in proposal["files"]:
        assert (vault / item["target_path"]).read_text(encoding="utf-8") == item["markdown"]
    assert not any(path.name.startswith(".M-migrated.migration.") for path in (vault / "Memories").iterdir())
    _assert_source_unchanged(vault, original)


def test_source_change_after_preview_conflicts_without_visible_target(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    original = _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    proposal = store.prepare_legacy_memory_migration("Migration", "M-conflict")
    report = vault / "reports" / "Report-fixed.md"
    report.write_text(report.read_text(encoding="utf-8") + "external edit\n", encoding="utf-8")

    with pytest.raises(MemoryWriteConflictError, match="changed"):
        store.commit_legacy_memory_migration(proposal)

    assert not (vault / "Memories" / "M-conflict").exists()
    assert not (vault / "Memories").exists()
    assert (vault / "evidence" / "raw_under.score.md").read_bytes() == original[
        "evidence/raw_under.score.md"
    ]


def test_staging_write_failure_rolls_back_without_touching_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    original = _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    proposal = store.prepare_legacy_memory_migration("Migration", "M-failure")
    original_write_text = Path.write_text

    def failing_write_text(path: Path, data: str, *args, **kwargs):
        if ".migration." in path.as_posix() and path.parent.name == "evidence":
            raise OSError("injected staging failure")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    with pytest.raises(OSError, match="injected"):
        store.commit_legacy_memory_migration(proposal)

    assert not (vault / "Memories" / "M-failure").exists()
    assert not (vault / "Memories").exists()
    _assert_source_unchanged(vault, original)


def test_modified_or_reserved_proposal_and_existing_target_are_rejected(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    with pytest.raises(ValueError, match="reserved"):
        store.prepare_legacy_memory_migration("Migration", "M-legacy")

    proposal = store.prepare_legacy_memory_migration("Migration", "M-target")
    forged = {**proposal, "title": "Forged"}
    with pytest.raises(ValueError, match="modified|match"):
        store.commit_legacy_memory_migration(forged)

    store.create_memory("Occupied", "M-occupied")
    with pytest.raises(FileExistsError, match="already exists"):
        store.prepare_legacy_memory_migration("Migration", "M-occupied")
    assert not (vault / "Memories" / "M-target").exists()


def test_unresolved_legacy_wikilink_blocks_preview_without_writes(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_legacy(vault)
    report = vault / "reports" / "Report-fixed.md"
    report.write_text(
        report.read_text(encoding="utf-8") + "[[evidence/Missing|Missing]]\n",
        encoding="utf-8",
    )
    before = sorted(path.relative_to(vault) for path in vault.rglob("*"))

    with pytest.raises(ValueError, match="missing or ambiguous"):
        MarkdownMemoryStore(vault).prepare_legacy_memory_migration(
            "Migration", "M-broken"
        )

    assert sorted(path.relative_to(vault) for path in vault.rglob("*")) == before
    assert not (vault / "Memories").exists()


def test_publish_point_exposes_absent_or_complete_memory_never_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    proposal = store.prepare_legacy_memory_migration("Migration", "M-atomic")
    target = vault / "Memories" / "M-atomic"
    reached_publish = threading.Event()
    allow_publish = threading.Event()
    original_rename = Path.rename

    def gated_rename(source: Path, destination: Path):
        if ".migration." in source.name and destination == target:
            assert not destination.exists()
            assert (source / "Home.md").is_file()
            assert {
                path.relative_to(source).as_posix()
                for path in source.rglob("*")
                if path.is_file()
            } == {
                "Home.md",
                *(
                    Path(item["target_path"]).relative_to(
                        "Memories", "M-atomic"
                    ).as_posix()
                    for item in proposal["files"]
                ),
            }
            reached_publish.set()
            assert allow_publish.wait(timeout=5)
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", gated_rename)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.commit_legacy_memory_migration, proposal)
        assert reached_publish.wait(timeout=5)
        # A direct filesystem reader cannot observe the private staging path as
        # the canonical Memory before the single directory rename.
        assert not target.exists()
        allow_publish.set()
        descriptor = future.result(timeout=5)

    assert descriptor.memory_id == "M-atomic"
    assert store.get_memory("M-atomic") == descriptor
    assert all((vault / item["target_path"]).is_file() for item in proposal["files"])
    assert not any(
        path.name.startswith(".M-atomic.migration.")
        for path in (vault / "Memories").iterdir()
    )


def test_publish_rename_failure_removes_staging_and_keeps_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    original = _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    proposal = store.prepare_legacy_memory_migration("Migration", "M-rename-fail")
    original_rename = Path.rename

    def failing_rename(source: Path, destination: Path):
        if ".migration." in source.name:
            raise OSError("injected publish failure")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError, match="publish failure"):
        store.commit_legacy_memory_migration(proposal)

    assert not (vault / "Memories" / "M-rename-fail").exists()
    assert not (vault / "Memories").exists()
    _assert_source_unchanged(vault, original)


def test_external_edit_during_staging_is_preserved_and_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    proposal = store.prepare_legacy_memory_migration("Migration", "M-mid-edit")
    report = vault / "reports" / "Report-fixed.md"
    original_write_text = Path.write_text
    changed = False

    def editing_write_text(path: Path, data: str, *args, **kwargs):
        nonlocal changed
        result = original_write_text(path, data, *args, **kwargs)
        if (
            not changed
            and ".migration." in path.as_posix()
            and path.parent.name == "sources"
        ):
            original_write_text(
                report,
                report.read_text(encoding="utf-8") + "external during commit\n",
                encoding="utf-8",
            )
            changed = True
        return result

    monkeypatch.setattr(Path, "write_text", editing_write_text)
    with pytest.raises(MemoryWriteConflictError, match="during migration"):
        store.commit_legacy_memory_migration(proposal)

    assert changed is True
    assert report.read_text(encoding="utf-8").endswith("external during commit\n")
    assert not (vault / "Memories" / "M-mid-edit").exists()
    assert not (vault / "Memories").exists()


def test_legacy_chat_manifest_remains_byte_identical_and_expandable_after_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    original = _write_legacy(vault)
    memory_store = MarkdownMemoryStore(vault)
    chat_store = ChatStore(str(tmp_path / "chat.db"))
    pointer = json.dumps(
        {
            "task_id": "legacy-task",
            "thread_id": "legacy-thread",
            "memory_id": None,
            "manifest": {
                "report_path": "reports/Report-fixed.md",
                "evidence_paths": ["evidence/raw_under.score.md"],
                "source_paths": ["sources/Source-fixed.md"],
            },
        },
        ensure_ascii=False,
    )
    chat_store.add("legacy-session", "assistant", KIND_REPORT, pointer)
    raw_before = chat_store.get_messages("legacy-session")
    monkeypatch.setattr(server.get_chat_store, "_store", chat_store, raising=False)
    monkeypatch.setattr(
        server.get_research_runtime,
        "_runtime",
        SimpleNamespace(read_memory=memory_store.read_text),
        raising=False,
    )

    expanded_before = server._expanded_messages("legacy-session")
    proposal = memory_store.prepare_legacy_memory_migration(
        "Migration", "M-chat-copy"
    )
    memory_store.commit_legacy_memory_migration(proposal)
    expanded_after = server._expanded_messages("legacy-session")
    raw_after = chat_store.get_messages("legacy-session")

    assert raw_after == raw_before
    assert raw_after[0]["content"] == pointer
    assert expanded_before[0]["content"] == original[
        "reports/Report-fixed.md"
    ].decode("utf-8")
    assert expanded_after == expanded_before
    _assert_source_unchanged(vault, original)

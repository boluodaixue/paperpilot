"""S4 acceptance tests for the rebuildable SQLite FTS5 derivative index."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from src.research import MarkdownMemoryIndex, MarkdownMemoryStore


def _write(root: Path, relative: str, markdown: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(markdown.encode("utf-8"))
    return target


def test_s4_persists_required_document_and_chunk_fields_outside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    database = tmp_path / "app-data" / "retrieval.db"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Persistent", "M-persistent")
    note = _write(
        vault / "Memories" / "M-persistent",
        "notes/N-one.md",
        "---\ntitle: Indexed title\ntags: [paperpilot]\n---\n\n# Heading\n\nPersistentneedle. [[Memories/M-persistent/Home]]",
    )

    hits = MarkdownMemoryIndex(store, database).search("M-persistent", "persistentneedle")
    hit = next(item for item in hits if item.relative_path.endswith("N-one.md"))

    assert database.is_file()
    assert not database.is_relative_to(vault)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    document = connection.execute(
        "SELECT * FROM memory_index_documents WHERE relative_path LIKE '%N-one.md'"
    ).fetchone()
    chunk = connection.execute(
        "SELECT memory_id, relative_path, chunk_id, content_hash, title, frontmatter, body, wikilinks "
        "FROM memory_chunks_fts WHERE relative_path LIKE '%N-one.md'"
    ).fetchone()
    connection.close()
    assert document["memory_id"] == "M-persistent"
    assert document["content_hash"] == hashlib.sha256(note.read_bytes()).hexdigest()
    assert document["modified_ns"] == note.stat().st_mtime_ns
    assert document["title"] == "Indexed title"
    assert "paperpilot" in document["frontmatter_text"]
    assert chunk["chunk_id"].endswith("#0")
    assert chunk["relative_path"] == hit.relative_path
    assert "Persistentneedle" in chunk["body"]
    assert "Memories/M-persistent/Home" in chunk["wikilinks"]


def test_s4_index_delete_rebuild_has_equivalent_results(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    database = tmp_path / "retrieval.db"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Rebuild", "M-rebuild")
    memory = vault / "Memories" / "M-rebuild"
    _write(memory, "notes/N-a.md", "# Alpha\n\nRebuildneedle and evidence.")
    _write(memory, "notes/N-b.md", "# Beta\n\n[[Memories/M-rebuild/notes/N-a]]")
    first = MarkdownMemoryIndex(store, database).search("M-rebuild", "rebuildneedle", limit=10)

    database.unlink()
    rebuilt = MarkdownMemoryIndex(store, database).search("M-rebuild", "rebuildneedle", limit=10)

    assert [(hit.relative_path, hit.score) for hit in rebuilt] == [
        (hit.relative_path, hit.score) for hit in first
    ]


def test_s4_incremental_create_modify_delete_and_rename_converge(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    database = tmp_path / "retrieval.db"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Changes", "M-changes")
    memory = vault / "Memories" / "M-changes"
    original = _write(memory, "notes/N-old.md", "# Old\n\nOldneedle.")
    index = MarkdownMemoryIndex(store, database)
    assert index.search("M-changes", "oldneedle")

    created = _write(memory, "notes/N-created.md", "# Created\n\nCreatedneedle.")
    assert index.search("M-changes", "createdneedle")[0].relative_path.endswith("N-created.md")
    created.write_text("# Changed\n\nChangedneedle.", encoding="utf-8")
    changed_ns = created.stat().st_mtime_ns + 10_000_000
    os.utime(created, ns=(changed_ns, changed_ns))
    assert index.search("M-changes", "changedneedle")[0].title == "Changed"
    assert index.search("M-changes", "createdneedle") == ()

    renamed = memory / "notes" / "N-renamed.md"
    original.rename(renamed)
    assert index.search("M-changes", "oldneedle")[0].relative_path.endswith("N-renamed.md")
    created.unlink()
    assert index.search("M-changes", "changedneedle") == ()


def test_s4_strict_memory_scope_and_hash_revalidation(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    database = tmp_path / "retrieval.db"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Alpha", "M-alpha")
    store.create_memory("Beta", "M-beta")
    alpha = _write(
        vault / "Memories" / "M-alpha",
        "notes/N-alpha.md",
        "# Alpha\n\nScopedneedle.",
    )
    _write(
        vault / "Memories" / "M-beta",
        "notes/N-secret.md",
        "# Secret\n\nScopedneedle beta secret.",
    )
    index = MarkdownMemoryIndex(store, database, reconciliation_seconds=3600)
    assert [hit.relative_path for hit in index.search("M-alpha", "scopedneedle")] == [
        "Memories/M-alpha/notes/N-alpha.md"
    ]

    before = alpha.stat()
    replacement = "# Alpha\n\nDifferentterm"
    assert len(replacement.encode("utf-8")) == before.st_size
    alpha.write_bytes(replacement.encode("utf-8"))
    os.utime(alpha, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert index.search("M-alpha", "scopedneedle") == ()
    assert index.search("M-alpha", "differentterm")[0].relative_path.endswith("N-alpha.md")


def test_s4_fixed_chinese_wikilink_and_large_vault_set(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    database = tmp_path / "retrieval.db"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Large", "M-large")
    memory = vault / "Memories" / "M-large"
    for index in range(250):
        token = "量子计算" if index == 173 else f"普通条目{index}"
        link = (
            " [[Memories/M-large/notes/N-neighbor|关联笔记]]"
            if index == 173
            else ""
        )
        _write(memory, f"notes/N-{index:03d}.md", f"# 条目 {index}\n\n{token}{link}")
    _write(memory, "notes/N-neighbor.md", "# 邻居\n\n仅由 WikiLink 召回。")

    hits = MarkdownMemoryIndex(store, database).search("M-large", "量子 计算", limit=10)

    paths = {hit.relative_path for hit in hits}
    assert "Memories/M-large/notes/N-173.md" in paths
    assert "Memories/M-large/notes/N-neighbor.md" in paths
    connection = sqlite3.connect(database)
    count = connection.execute(
        "SELECT COUNT(*) FROM memory_index_documents WHERE memory_id = 'M-large'"
    ).fetchone()[0]
    connection.close()
    assert count == 252  # 250 notes, one neighbor, and Home.md

"""S5 acceptance tests for optional, failure-safe hybrid retrieval."""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

from src.research import MarkdownMemoryIndex, MarkdownMemoryStore


def _write(root: Path, relative: str, markdown: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return target


class FixedMultilingualEmbeddings:
    def __init__(self, model_id: str = "fixed-multilingual-v1", *, fail: bool = False) -> None:
        self.model_id = model_id
        self.fail = fail
        self.seen: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("offline model unavailable")
        self.seen.extend(texts)
        vectors: list[list[float]] = []
        for text in texts:
            folded = text.casefold()
            vehicle = any(term in folded for term in ("automobile", "car", "汽车", "车辆"))
            learning = any(
                term in folded for term in ("machine learning", "ml system", "机器学习")
            )
            exact = "exactneedle" in folded
            vectors.append(
                [
                    10.0 if vehicle else 0.01,
                    10.0 if learning else 0.01,
                    10.0 if exact else 0.01,
                    0.1,
                ]
            )
        return vectors


def test_s5_multilingual_synonyms_and_exact_keyword_do_not_regress(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Hybrid", "M-hybrid")
    memory = vault / "Memories" / "M-hybrid"
    _write(memory, "notes/N-car.md", "# Transport\n\nA car reduces travel time.")
    _write(memory, "notes/N-ml.md", "# AI\n\nMachine learning improves ranking.")
    _write(memory, "notes/N-exact.md", "# Exact\n\nExactneedle is authoritative.")

    baseline = MarkdownMemoryIndex(store, tmp_path / "baseline.db")
    assert baseline.search("M-hybrid", "automobile") == ()
    assert baseline.search("M-hybrid", "机器学习") == ()

    hybrid = MarkdownMemoryIndex(
        store,
        tmp_path / "hybrid.db",
        embedding_provider=FixedMultilingualEmbeddings(),
    )
    assert hybrid.search("M-hybrid", "automobile")[0].relative_path.endswith("N-car.md")
    assert hybrid.search("M-hybrid", "机器学习")[0].relative_path.endswith("N-ml.md")
    exact = hybrid.search("M-hybrid", "exactneedle")
    assert exact[0].relative_path.endswith("N-exact.md")


def test_s5_wikilink_neighbor_is_fused_with_semantic_seed(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Links", "M-links")
    memory = vault / "Memories" / "M-links"
    _write(
        memory,
        "notes/N-car.md",
        "# Transport\n\nA car overview. [[Memories/M-links/notes/N-policy]]",
    )
    _write(memory, "notes/N-policy.md", "# Policy\n\nLinked context only.")
    index = MarkdownMemoryIndex(
        store,
        tmp_path / "retrieval.db",
        embedding_provider=FixedMultilingualEmbeddings(),
    )

    paths = {hit.relative_path for hit in index.search("M-links", "automobile", limit=10)}

    assert "Memories/M-links/notes/N-car.md" in paths
    assert "Memories/M-links/notes/N-policy.md" in paths


def test_s5_embedding_failure_has_identical_s4_fallback(
    tmp_path: Path,
    caplog,
) -> None:
    vault = tmp_path / "Vault"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Fallback", "M-fallback")
    _write(
        vault / "Memories" / "M-fallback",
        "notes/N-note.md",
        "# Stable\n\nFallbackneedle remains searchable.",
    )
    expected = MarkdownMemoryIndex(store, tmp_path / "fts.db").search(
        "M-fallback", "fallbackneedle"
    )
    with caplog.at_level(logging.WARNING, logger="src.research.retrieval"):
        actual = MarkdownMemoryIndex(
            store,
            tmp_path / "hybrid.db",
            embedding_provider=FixedMultilingualEmbeddings(fail=True),
        ).search("M-fallback", "fallbackneedle")

    assert actual == expected
    assert "using SQLite FTS5" in caplog.text
    assert "RuntimeError" in caplog.text


def test_s5_model_version_replaces_cache_and_stays_rebuildable(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    database = tmp_path / "retrieval.db"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Versions", "M-versions")
    _write(vault / "Memories" / "M-versions", "notes/N-car.md", "# Car\n\nA car.")

    MarkdownMemoryIndex(
        store, database, embedding_provider=FixedMultilingualEmbeddings("model-v1")
    ).search("M-versions", "automobile")
    cached_provider = FixedMultilingualEmbeddings("model-v1")
    MarkdownMemoryIndex(store, database, embedding_provider=cached_provider).search(
        "M-versions", "automobile"
    )
    assert cached_provider.seen == ["automobile"]
    MarkdownMemoryIndex(
        store, database, embedding_provider=FixedMultilingualEmbeddings("model-v2")
    ).search("M-versions", "automobile")

    connection = sqlite3.connect(database)
    model_ids = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT model_id FROM memory_embedding_chunks WHERE memory_id = ?",
            ("M-versions",),
        )
    }
    connection.close()
    assert model_ids == {"model-v2"}

    rebuilt = MarkdownMemoryIndex(
        store, database, embedding_provider=FixedMultilingualEmbeddings("model-v2")
    )
    rebuilt.rebuild("M-versions")
    assert rebuilt.search("M-versions", "automobile")[0].relative_path.endswith("N-car.md")


def test_s5_selected_memory_only_and_final_hash_is_current(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Selected", "M-selected")
    store.create_memory("Secret", "M-secret")
    selected = _write(
        vault / "Memories" / "M-selected",
        "notes/N-car.md",
        "# Selected\n\nA car for the selected scope.",
    )
    _write(
        vault / "Memories" / "M-secret",
        "notes/N-secret.md",
        "# Secret\n\nDO-NOT-EMBED-THIS car.",
    )
    provider = FixedMultilingualEmbeddings()
    hits = MarkdownMemoryIndex(
        store, tmp_path / "retrieval.db", embedding_provider=provider
    ).search("M-selected", "automobile", limit=10)

    assert hits
    assert all(hit.relative_path.startswith("Memories/M-selected/") for hit in hits)
    assert all("DO-NOT-EMBED-THIS" not in text for text in provider.seen)
    hit = next(item for item in hits if item.relative_path.endswith("N-car.md"))
    assert hit.content_hash == hashlib.sha256(selected.read_bytes()).hexdigest()


def test_s5_invalid_vectors_fall_back_without_fake_semantic_results(tmp_path: Path) -> None:
    class InvalidEmbeddings(FixedMultilingualEmbeddings):
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[float("nan")] for _text in texts]

    vault = tmp_path / "Vault"
    store = MarkdownMemoryStore(vault)
    store.create_memory("Invalid", "M-invalid")
    _write(
        vault / "Memories" / "M-invalid",
        "notes/N-note.md",
        "# Stable\n\nExactneedle survives invalid vectors.",
    )
    hits = MarkdownMemoryIndex(
        store, tmp_path / "retrieval.db", embedding_provider=InvalidEmbeddings()
    ).search("M-invalid", "exactneedle")

    assert hits[0].relative_path.endswith("N-note.md")
    assert hits[0].score == 2

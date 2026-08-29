"""W3 acceptance tests for deterministic, rebuildable Memory retrieval."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from src.research import MarkdownMemoryIndex, MarkdownMemoryStore


def _write(memory_root: Path, relative: str, markdown: str) -> Path:
    target = memory_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return target


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError) as symlink_error:
        if os.name != "nt":
            pytest.skip(f"symbolic links are unavailable: {symlink_error}")

    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if junction.returncode != 0:
        pytest.skip("symbolic links and directory junctions are unavailable")


def test_rebuild_reads_standard_and_user_markdown_without_persisting_index(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Retrieval", "M-retrieval")
    memory = tmp_path / "Memories" / "M-retrieval"
    standard = _write(
        memory,
        "notes/N-standard.md",
        "---\ntitle: Frontmatter title\ncustom:\n  nested: value\n---\n\n# Ignored H1\n\nBody.",
    )
    _write(memory, "notes/User note.md", "# User H1\n\nPlain user Markdown.")

    rebuilt = MarkdownMemoryIndex(store).rebuild("M-retrieval")

    by_path = {hit.relative_path: hit for hit in rebuilt}
    standard_hit = by_path["Memories/M-retrieval/notes/N-standard.md"]
    assert standard_hit.title == "Frontmatter title"
    assert standard_hit.content_hash == hashlib.sha256(standard.read_bytes()).hexdigest()
    assert standard_hit.modified_ns == standard.stat().st_mtime_ns
    assert by_path["Memories/M-retrieval/notes/User note.md"].title == "User H1"
    assert not list(tmp_path.rglob("*index*"))


def test_search_ranks_relevant_notes_and_returns_empty_for_unrelated_query(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Topic", "M-topic")
    memory = tmp_path / "Memories" / "M-topic"
    _write(memory, "notes/Attention overview.md", "# Architecture\n\nMinor detail.")
    _write(memory, "notes/N-body.md", "# Other\n\nAttention appears in the body.")
    _write(memory, "notes/N-unrelated.md", "# Cooking\n\nBread and butter.")
    index = MarkdownMemoryIndex(store)

    hits = index.search("M-topic", "attention")

    assert [hit.relative_path for hit in hits[:2]] == [
        "Memories/M-topic/notes/Attention overview.md",
        "Memories/M-topic/notes/N-body.md",
    ]
    assert hits[0].score > hits[1].score > 0
    assert all("N-unrelated.md" not in hit.relative_path for hit in hits)
    assert index.search("M-topic", "volcanology") == ()


def test_natural_question_stopwords_do_not_seed_unrelated_notes(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Questions", "M-questions")
    memory = tmp_path / "Memories" / "M-questions"
    _write(memory, "notes/N-attention.md", "# Attention\n\nAttention has a history.")
    _write(
        memory,
        "notes/N-common.md",
        "# Common words\n\nWhat is the meaning of this and that?",
    )

    hits = MarkdownMemoryIndex(store).search(
        "M-questions",
        "what is the history of attention",
    )

    assert [hit.relative_path for hit in hits] == [
        "Memories/M-questions/notes/N-attention.md"
    ]


def test_search_isolates_selected_memory_and_ignores_cross_memory_link(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Alpha", "M-alpha")
    store.create_memory("Beta", "M-beta")
    alpha = tmp_path / "Memories" / "M-alpha"
    beta = tmp_path / "Memories" / "M-beta"
    _write(
        alpha,
        "notes/N-alpha.md",
        "# Alpha\n\nIsolationtoken. [[Memories/M-beta/notes/N-secret|Secret]]",
    )
    _write(beta, "notes/N-secret.md", "# Secret\n\nIsolationtoken beta secret.")

    hits = MarkdownMemoryIndex(store).search("M-alpha", "isolationtoken", limit=10)

    assert [hit.relative_path for hit in hits] == [
        "Memories/M-alpha/notes/N-alpha.md"
    ]


def test_wikilinks_add_forward_notes_and_backlinks_one_hop(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Links", "M-links")
    memory = tmp_path / "Memories" / "M-links"
    _write(
        memory,
        "notes/N-source.md",
        "# Source\n\nCatalystterm. [[Memories/M-links/notes/N-target|Target]]",
    )
    _write(memory, "notes/N-target.md", "# Target\n\nOrchidterm only here.")
    index = MarkdownMemoryIndex(store)

    forward = index.search("M-links", "catalystterm", limit=10)
    backward = index.search("M-links", "orchidterm", limit=10)

    assert {hit.relative_path for hit in forward} == {
        "Memories/M-links/notes/N-source.md",
        "Memories/M-links/notes/N-target.md",
    }
    assert {hit.relative_path for hit in backward} == {
        "Memories/M-links/notes/N-source.md",
        "Memories/M-links/notes/N-target.md",
    }
    source_hit = next(hit for hit in forward if hit.relative_path.endswith("N-source.md"))
    assert source_hit.wikilinks == ("Memories/M-links/notes/N-target",)


def test_chinese_terms_support_spaces_substrings_and_bigrams(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("中文检索", "M-chinese")
    memory = tmp_path / "Memories" / "M-chinese"
    _write(memory, "notes/N-quantum.md", "# 量子计算综述\n\n讨论量子算法和复杂度。")
    _write(memory, "notes/N-other.md", "# 园艺\n\n讨论花卉种植。")

    hits = MarkdownMemoryIndex(store).search("M-chinese", "量子 计算")

    assert hits
    assert hits[0].relative_path.endswith("N-quantum.md")
    assert all(not hit.relative_path.endswith("N-other.md") for hit in hits)


def test_search_rebuild_observes_external_edit_hash_mtime_and_title(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Edits", "M-edits")
    note = _write(
        tmp_path / "Memories" / "M-edits",
        "notes/N-edit.md",
        "# Old title\n\nOldterm.",
    )
    index = MarkdownMemoryIndex(store)
    before = index.search("M-edits", "oldterm")[0]
    new_markdown = "# New title\n\nNewterm from an external editor."
    note.write_text(new_markdown, encoding="utf-8")
    changed_ns = before.modified_ns + 10_000_000
    os.utime(note, ns=(changed_ns, changed_ns))

    after = index.search("M-edits", "newterm")[0]

    assert after.title == "New title"
    assert after.content_hash == hashlib.sha256(note.read_bytes()).hexdigest()
    assert after.content_hash != before.content_hash
    assert after.modified_ns == changed_ns
    assert index.search("M-edits", "oldterm") == ()


def test_limit_summary_bound_and_stable_tie_order(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Bounds", "M-bounds")
    memory = tmp_path / "Memories" / "M-bounds"
    long_body = "Boundterm " + ("x" * 1000)
    for name in ("N-c.md", "N-a.md", "N-b.md"):
        _write(memory, f"notes/{name}", f"# Same\n\n{long_body}")
    index = MarkdownMemoryIndex(store)

    hits = index.search("M-bounds", "boundterm", limit=2)

    assert [Path(hit.relative_path).name for hit in hits] == ["N-a.md", "N-b.md"]
    assert all(len(hit.summary) <= 320 for hit in hits)
    assert all(hit.summary.endswith("…") for hit in hits)
    with pytest.raises(ValueError, match="between 1 and 10"):
        index.search("M-bounds", "boundterm", limit=0)
    with pytest.raises(ValueError, match="between 1 and 10"):
        index.search("M-bounds", "boundterm", limit=11)


def test_direct_hit_summary_is_centered_on_a_late_body_match(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Snippets", "M-snippets")
    memory = tmp_path / "Memories" / "M-snippets"
    _write(
        memory,
        "notes/N-late.md",
        "# Long note\n\n" + ("preface " * 100) + "Needleterm evidence is here.",
    )

    hit = MarkdownMemoryIndex(store).search("M-snippets", "needleterm")[0]

    assert "Needleterm evidence" in hit.summary
    assert len(hit.summary) <= 320
    assert hit.summary.startswith("…")


def test_search_rebuilds_even_for_blank_query(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Blank", "M-blank")
    index = MarkdownMemoryIndex(store)

    assert index.search("M-blank", "   ") == ()
    assert "M-blank" in index._notes


def test_symlink_or_junction_escape_is_rejected_without_reading_outside(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("Safe", "M-safe")
    memory = tmp_path / "vault" / "Memories" / "M-safe"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "N-secret.md"
    secret.write_text("# Secret\n\nEscapeterm.", encoding="utf-8")
    _make_directory_link(memory / "linked", outside)

    with pytest.raises(ValueError, match="escapes"):
        MarkdownMemoryIndex(store).search("M-safe", "escapeterm")


def test_invalid_utf8_outside_selected_memory_is_never_read(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Selected", "M-selected")
    store.create_memory("Other", "M-other")
    _write(
        tmp_path / "Memories" / "M-selected",
        "notes/N-selected.md",
        "# Selected\n\nNeedleterm.",
    )
    invalid = tmp_path / "Memories" / "M-other" / "notes" / "N-invalid.md"
    invalid.write_bytes(b"\xff\xfe\x00")

    hits = MarkdownMemoryIndex(store).search("M-selected", "needleterm")

    assert len(hits) == 1
    assert hits[0].relative_path.endswith("N-selected.md")

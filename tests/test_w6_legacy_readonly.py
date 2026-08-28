"""W6 contract tests for the virtual read-only legacy Memory."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from src.research.memory import MarkdownMemoryStore
from src.research.memory_dialogue import answer_memory, propose_memory_note
from src.research.models import MemoryAnswer
from src.research.vault import (
    LEGACY_MEMORY_ID,
    build_legacy_wikilink,
    resolve_legacy_memory_markdown_path,
    scan_legacy_memory_markdown,
    validate_legacy_memory_markdown_path,
)


def _snapshot(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    entries: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        entries.append(
            (relative, "directory", None)
            if path.is_dir()
            else (relative, "file", path.read_bytes())
        )
    return tuple(entries)


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError) as symlink_error:
        if os.name != "nt":
            pytest.skip(f"symbolic links are unavailable: {symlink_error}")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("symbolic links and junctions are unavailable")


def test_safe_legacy_scan_is_deterministic_recursive_and_read_only(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    contents = {
        "reports/Report-z.md": "# Z\n",
        "reports/archive/Report-a.md": "# A\n",
        "evidence/Evidence-one.md": "# Evidence\n",
        "sources/Source-one.md": "# Source\n",
        "sources/original.pdf": b"not part of the Markdown view",
    }
    for relative_path, content in contents.items():
        path = vault / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    before = _snapshot(vault)

    scanned = scan_legacy_memory_markdown(vault)

    assert tuple(path for path, _ in scanned) == (
        "evidence/Evidence-one.md",
        "reports/Report-z.md",
        "reports/archive/Report-a.md",
        "sources/Source-one.md",
    )
    assert all(path.is_file() for _, path in scanned)
    assert scan_legacy_memory_markdown(vault) == scanned
    assert _snapshot(vault) == before


@pytest.mark.parametrize(
    "relative_path",
    (
        "../reports/Report.md",
        "notes/Note.md",
        "reports/not-markdown.txt",
        "reports/CON.md",
        "reports/N-note.md:stream",
    ),
)
def test_legacy_path_contract_rejects_unsafe_or_out_of_scope_paths(
    relative_path: str,
) -> None:
    with pytest.raises(ValueError):
        validate_legacy_memory_markdown_path(relative_path)


def test_legacy_wikilink_keeps_root_path_and_validates_alias(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    report = vault / "reports" / "Report-old.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Old\n", encoding="utf-8")

    assert build_legacy_wikilink("reports/Report-old.md") == (
        "[[reports/Report-old]]"
    )
    assert build_legacy_wikilink("reports/Report-old.md", "Old report") == (
        "[[reports/Report-old|Old report]]"
    )
    assert resolve_legacy_memory_markdown_path(
        vault, "reports/Report-old.md"
    ) == report.resolve()
    with pytest.raises(ValueError):
        build_legacy_wikilink("reports/Report-old.md", "bad|alias")


def test_legacy_scan_rejects_linked_entries_instead_of_partially_reading(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    reports = vault / "reports"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Report-outside.md").write_text("outside", encoding="utf-8")
    _make_directory_link(reports / "linked", outside)

    with pytest.raises(ValueError, match="symlink or junction"):
        scan_legacy_memory_markdown(vault)


def test_reserved_legacy_id_cannot_be_created_or_receive_note_proposals(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path / "vault")
    with pytest.raises(ValueError, match="read-only"):
        store.create_memory("Not writable", memory_id=LEGACY_MEMORY_ID)

    answer = MemoryAnswer(
        answer_id="Answer-legacy",
        memory_id=LEGACY_MEMORY_ID,
        question="What is known?",
        markdown="Read-only answer",
        citations=(),
        insufficient_evidence=(),
    )
    with pytest.raises(ValueError, match="read-only"):
        import asyncio

        asyncio.run(propose_memory_note(store, object(), answer))
    assert not (store.root / "Memories").exists()


@pytest.mark.asyncio
async def test_legacy_memory_can_be_answered_read_only_with_root_citation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    report = vault / "reports" / "Report-old.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "# Legacy finding\n\nCatalysttoken establishes the old baseline.\n",
        encoding="utf-8",
    )
    before = _snapshot(vault)

    def policy(messages, *, tools=None):
        context = json.loads(
            messages[-1]["content"].split("MEMORY_CONTEXT_JSON:\n", 1)[1]
        )
        return {
            "content": json.dumps(
                {
                    "claims": [
                        {
                            "text": "The old report establishes the baseline.",
                            "source_paths": [context["hits"][0]["path"]],
                        }
                    ],
                    "insufficient_evidence": [],
                }
            )
        }

    answer = await answer_memory(
        MarkdownMemoryStore(vault),
        policy,
        LEGACY_MEMORY_ID,
        "What does catalysttoken establish?",
    )

    assert answer.memory_id == LEGACY_MEMORY_ID
    assert [citation.relative_path for citation in answer.citations] == [
        "reports/Report-old.md"
    ]
    assert "[[reports/Report-old]]" in answer.markdown
    assert _snapshot(vault) == before

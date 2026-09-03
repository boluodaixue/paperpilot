"""W1 acceptance tests for the multi-Memory Markdown store slice."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from src.research.memory import MarkdownMemoryStore
from src.research.models import (
    EvidenceItem,
    ExecutionIdentity,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
)
from src.research.report_review import validate_revised_report
from src.research.vault import validate_frontmatter


def _identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(
        thread_id=thread_id,
        parent_thread_id=None,
        root_thread_id=thread_id,
        depth=0,
    )


def _brief(question: str = "How does attention work?") -> ResearchBrief:
    return ResearchBrief(
        question=question,
        objective="Explain the evidence",
        scope=("architecture",),
        directions=("primary sources",),
        constraints=("cite locations",),
        expected_output="report",
    )


def _result(
    evidence_id: str = "E-attention",
    *,
    title: str = "Attention source",
) -> ResearchResult:
    evidence = EvidenceItem(
        evidence_id=evidence_id,
        finding="Attention replaces recurrence.",
        source_type="web",
        title=title,
        source_ref=f"https://example.com/{evidence_id}",
        locator="section 1",
        excerpt="Attention is sufficient.",
        excerpt_type="quote",
    )
    return ResearchResult(
        task_id="task",
        status=ResearchStatus.COMPLETED,
        summary="Attention is central.",
        findings=(evidence.finding,),
        evidence=(evidence,),
    )


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    _, yaml_text, _ = text.split("---", 2)
    loaded = yaml.safe_load(yaml_text)
    assert isinstance(loaded, dict)
    return validate_frontmatter(loaded)


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


def test_create_memory_atomically_builds_home_and_required_directories(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)

    descriptor = store.create_memory("Transformer Research", "M-transformers")

    assert descriptor.memory_id == "M-transformers"
    assert descriptor.title == "Transformer Research"
    assert descriptor.relative_path == "Memories/M-transformers/"
    memory_root = tmp_path / descriptor.relative_path
    assert sorted(path.name for path in memory_root.iterdir()) == [
        "Home.md",
        "attachments",
        "evidence",
        "imports",
        "notes",
        "reports",
        "sources",
    ]
    home = (memory_root / "Home.md").read_text(encoding="utf-8")
    for heading in (
        "# Transformer Research",
        "## Objective",
        "## Reports",
        "## Notes",
        "## Imports",
        "## Known findings",
        "## Open questions",
        "## Last updated",
    ):
        assert heading in home
    assert "Not specified" in home
    assert _frontmatter(memory_root / "Home.md")["memory_id"] == "M-transformers"
    assert not any(path.name.startswith(".") for path in (tmp_path / "Memories").iterdir())


def test_create_memory_generates_valid_unique_ids_and_rejects_duplicate(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    first = store.create_memory("First")
    second = store.create_memory("Second")

    assert first.memory_id.startswith("M-")
    assert second.memory_id.startswith("M-")
    assert first.memory_id != second.memory_id
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    with pytest.raises(FileExistsError, match="already exists"):
        store.create_memory("Duplicate", first.memory_id)
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_get_and_list_read_current_home_title_without_an_index(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Original", "M-beta")
    store.create_memory("Alpha", "M-alpha")
    home = tmp_path / "Memories" / "M-beta" / "Home.md"
    home.write_text(
        home.read_text(encoding="utf-8").replace(
            'title: "Original"', 'title: "Edited in Obsidian"'
        ),
        encoding="utf-8",
    )

    assert store.get_memory("M-beta").title == "Edited in Obsidian"
    assert [(item.memory_id, item.title) for item in store.list_memories()] == [
        ("M-alpha", "Alpha"),
        ("M-beta", "Edited in Obsidian"),
    ]
    with pytest.raises(FileNotFoundError, match="does not exist"):
        store.get_memory("M-missing")


def test_managed_research_isolated_with_full_frontmatter_and_full_wikilinks(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Alpha", "M-alpha")
    store.create_memory("Beta", "M-beta")

    _, alpha_first = store.persist_research(
        _brief("Alpha one"),
        _result("raw_under.score", title="unsafe | [source]"),
        _identity("thread-alpha-one"),
        memory_id="M-alpha",
    )
    _, alpha_second = store.persist_research(
        _brief("Alpha two"),
        _result("E-second"),
        _identity("thread-alpha-two"),
        memory_id="M-alpha",
    )
    _, beta = store.persist_research(
        _brief("Beta one"),
        _result("raw_under.score"),
        _identity("thread-beta-one"),
        memory_id="M-beta",
    )

    assert alpha_first.report_path.startswith("Memories/M-alpha/reports/")
    assert alpha_second.report_path.startswith("Memories/M-alpha/reports/")
    assert alpha_first.report_path != alpha_second.report_path
    assert beta.report_path.startswith("Memories/M-beta/reports/")
    assert all(
        path.startswith("Memories/M-beta/")
        for path in (*beta.evidence_paths, *beta.source_paths)
    )
    assert len(list((tmp_path / "Memories" / "M-alpha" / "reports").glob("*.md"))) == 2
    assert len(list((tmp_path / "Memories" / "M-beta" / "reports").glob("*.md"))) == 1

    managed_paths = [
        *alpha_first.evidence_paths,
        *alpha_first.source_paths,
        alpha_first.report_path,
    ]
    for relative in managed_paths:
        frontmatter = _frontmatter(tmp_path / relative)
        assert frontmatter["memory_id"] == "M-alpha"
        assert frontmatter["tags"] == ["paperpilot"]
    evidence_path = tmp_path / alpha_first.evidence_paths[0]
    evidence = evidence_path.read_text(encoding="utf-8")
    assert evidence_path.stem.startswith("Evidence-")
    assert "[[Memories/M-alpha/sources/Source-" in evidence
    assert "unsafe | [source]" not in evidence.split("## Source", 1)[1].splitlines()[2]
    report = (tmp_path / alpha_first.report_path).read_text(encoding="utf-8")
    assert f"[[Memories/M-alpha/evidence/{evidence_path.stem}|Evidence]]" in report
    alpha_home = (tmp_path / "Memories" / "M-alpha" / "Home.md").read_text(
        encoding="utf-8"
    )
    assert f"[[{alpha_first.report_path[:-3]}]]" in alpha_home
    assert f"[[{alpha_second.report_path[:-3]}]]" in alpha_home
    assert "## Reports\n\n- None yet." not in alpha_home
    home_updated_at = _frontmatter(
        tmp_path / "Memories" / "M-alpha" / "Home.md"
    )["updated_at"]
    assert f"## Last updated\n\n{home_updated_at}" in alpha_home
    validate_revised_report(report, report, alpha_first)
    assert not (tmp_path / "reports").exists()
    assert not (tmp_path / "evidence").exists()
    assert not (tmp_path / "sources").exists()


def test_later_research_preserves_existing_managed_evidence_and_source_edits(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Editable", "M-editable")
    _, first = store.persist_research(
        _brief("First"),
        _result(),
        _identity("editable-first"),
        memory_id="M-editable",
    )
    evidence_path = tmp_path / first.evidence_paths[0]
    source_path = tmp_path / first.source_paths[0]
    edited_evidence = evidence_path.read_text(encoding="utf-8") + "\nObsidian evidence note.\n"
    edited_source = source_path.read_text(encoding="utf-8") + "\nObsidian source note.\n"
    evidence_path.write_text(edited_evidence, encoding="utf-8")
    source_path.write_text(edited_source, encoding="utf-8")

    _, second = store.persist_research(
        _brief("Second"),
        _result(),
        _identity("editable-second"),
        memory_id="M-editable",
    )

    assert second.evidence_paths == first.evidence_paths
    assert second.source_paths == first.source_paths
    assert evidence_path.read_text(encoding="utf-8") == edited_evidence
    assert source_path.read_text(encoding="utf-8") == edited_source
    home = store.read_text("Memories/M-editable/Home.md")
    assert f"[[{first.report_path[:-3]}]]" in home
    assert f"[[{second.report_path[:-3]}]]" in home


def test_legacy_persistence_remains_on_root_with_legacy_markdown(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    report, manifest = store.persist_research(
        _brief(), _result("raw_under.score"), _identity("legacy-thread")
    )

    assert manifest.report_path.startswith("reports/")
    assert manifest.evidence_paths == ("evidence/raw_under.score.md",)
    assert "memory_id:" not in report
    assert "[[evidence/raw_under.score|Evidence]]" in report
    evidence = (tmp_path / manifest.evidence_paths[0]).read_text(encoding="utf-8")
    assert "[[sources/Source-" in evidence
    assert "[[Memories/" not in evidence


def test_managed_persistence_requires_an_existing_memory(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        store.persist_research(
            _brief(), _result(), _identity("missing-memory"), memory_id="M-missing"
        )
    assert not (tmp_path / "Memories").exists()


def test_replace_report_accepts_legacy_and_managed_paths_only(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Managed", "M-managed")
    managed = tmp_path / "Memories" / "M-managed" / "reports" / "Report-one.md"
    legacy = tmp_path / "reports" / "Report-old.md"
    for path in (managed, legacy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf-8")

    store.replace_report("Memories/M-managed/reports/Report-one.md", "managed revision")
    store.replace_report("reports/Report-old.md", "legacy revision")
    assert managed.read_text(encoding="utf-8") == "managed revision"
    assert legacy.read_text(encoding="utf-8") == "legacy revision"
    with pytest.raises(ValueError):
        store.replace_report("Memories/M-managed/evidence/E-one.md", "invalid")


def test_concurrent_create_exposes_one_complete_memory_and_no_staging(
    tmp_path: Path,
) -> None:
    def create(title: str) -> str:
        try:
            MarkdownMemoryStore(tmp_path).create_memory(title, "M-race")
            return "created"
        except FileExistsError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("One", "Two")))

    assert sorted(results) == ["created", "duplicate"]
    memory_root = tmp_path / "Memories" / "M-race"
    assert (memory_root / "Home.md").is_file()
    assert all(
        (memory_root / name).is_dir()
        for name in ("reports", "evidence", "sources", "notes", "imports", "attachments")
    )
    assert not any(path.name.startswith(".") for path in (tmp_path / "Memories").iterdir())


def test_concurrent_managed_writes_leave_only_complete_markdown_files(
    tmp_path: Path,
) -> None:
    MarkdownMemoryStore(tmp_path).create_memory("Race", "M-race")

    def persist(number: int) -> str:
        _, manifest = MarkdownMemoryStore(tmp_path).persist_research(
            _brief(f"Question {number}"),
            _result(f"E-{number}"),
            _identity(f"thread-{number}"),
            memory_id="M-race",
        )
        return manifest.report_path

    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(persist, range(4)))

    assert len(set(reports)) == 4
    memory_root = tmp_path / "Memories" / "M-race"
    assert not list(memory_root.rglob("*.tmp"))
    for path in memory_root.rglob("*.md"):
        assert path.read_text(encoding="utf-8").endswith("\n")


def test_memory_operations_reject_memories_symlink_escape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_directory_link(vault / "Memories", outside)
    store = MarkdownMemoryStore(vault)

    with pytest.raises(ValueError, match="escapes"):
        store.create_memory("Escaped", "M-escaped")
    with pytest.raises(ValueError, match="escapes"):
        store.get_memory("M-escaped")
    with pytest.raises(ValueError, match="escapes"):
        store.list_memories()
    assert not list(outside.iterdir())


def test_memory_operations_reject_target_memory_symlink_escape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    memories = vault / "Memories"
    memories.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_directory_link(memories / "M-escaped", outside)
    store = MarkdownMemoryStore(vault)

    with pytest.raises(ValueError, match="escapes"):
        store.create_memory("Escaped", "M-escaped")
    with pytest.raises(ValueError, match="escapes"):
        store.get_memory("M-escaped")
    with pytest.raises(ValueError, match="escapes"):
        store.list_memories()
    assert not list(outside.iterdir())

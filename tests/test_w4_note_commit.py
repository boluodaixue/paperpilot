"""W4 acceptance tests for controlled Memory note commits."""
from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import src.research.memory as memory_module
from src.research.memory import (
    MarkdownMemoryStore,
    MemoryWriteConflictError,
    update_memory_home_with_note,
)
from src.research.models import MemoryNoteProposal
from src.research.vault import build_wikilink


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
    }


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


def _proposal(
    store: MarkdownMemoryStore,
    *,
    memory_id: str = "M-notes",
    note_id: str = "Note-one",
    title: str = "Saved answer",
    source_paths: tuple[str, ...] = (),
) -> MemoryNoteProposal:
    descriptor = store.get_memory(memory_id)
    timestamp = descriptor.updated_at
    target_path = f"Memories/{memory_id}/notes/{note_id}.md"
    wikilink = build_wikilink(target_path)
    links = "\n".join(
        f"- {build_wikilink(source_path, 'Source')}" for source_path in source_paths
    )
    markdown = (
        "---\n"
        f'id: "{note_id}"\n'
        'type: "note"\n'
        f'memory_id: "{memory_id}"\n'
        f'title: "{title}"\n'
        f'created_at: "{timestamp}"\n'
        f'updated_at: "{timestamp}"\n'
        'origin: "conversation"\n'
        'status: "confirmed"\n'
        "tags:\n"
        "  - paperpilot\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{links or 'Saved without citations.'}\n"
    )
    home_path, current_home, home_hash = store.memory_home_snapshot(memory_id)
    return MemoryNoteProposal(
        proposal_id=f"Proposal-{note_id}",
        answer_id=f"Answer-{note_id}",
        memory_id=memory_id,
        note_id=note_id,
        title=title,
        target_path=target_path,
        markdown=markdown,
        wikilink=wikilink,
        source_paths=source_paths,
        home_path=home_path,
        home_content_hash=home_hash,
        target_content_hash=None,
        home_markdown=update_memory_home_with_note(
            current_home,
            wikilink,
            timestamp,
        ),
    )


def test_home_update_changes_only_timestamp_and_unique_notes_list(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    descriptor = store.create_memory("Notes", "M-notes")
    _, home, _ = store.memory_home_snapshot("M-notes")
    home = home.replace(
        "## Known findings\n\n- None yet.",
        "## Known findings\n\nUser-authored finding that must remain byte-for-byte.",
    )
    link = "[[Memories/M-notes/notes/Note-one]]"

    updated = update_memory_home_with_note(home, link, descriptor.updated_at)

    assert "User-authored finding that must remain byte-for-byte." in updated
    assert "## Notes\n\n- [[Memories/M-notes/notes/Note-one]]\n" in updated
    assert updated.count("## Notes") == 1
    assert updated.count(link) == 1
    with pytest.raises(ValueError, match="already contains"):
        update_memory_home_with_note(updated, link, descriptor.updated_at)
    with pytest.raises(ValueError, match="exactly one ## Notes"):
        update_memory_home_with_note(
            home.replace("## Notes", "## Missing"),
            link,
            descriptor.updated_at,
        )
    with pytest.raises(ValueError, match="exactly one ## Notes"):
        update_memory_home_with_note(
            f"{home}\n## Notes\n\n- Extra\n",
            link,
            descriptor.updated_at,
        )


def test_snapshot_and_validation_are_read_only(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    before = _tree(tmp_path)

    home_path, markdown, content_hash = store.memory_home_snapshot("M-notes")
    store.validate_memory_note_proposal(proposal)

    assert (home_path, markdown, content_hash) == (
        proposal.home_path,
        (tmp_path / proposal.home_path).read_bytes().decode("utf-8"),
        proposal.home_content_hash,
    )
    assert _tree(tmp_path) == before


def test_success_creates_only_note_and_replaces_home(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    source_path = "Memories/M-notes/reports/Report-source.md"
    source = tmp_path / source_path
    source.write_text("# Existing source\n", encoding="utf-8")
    proposal = _proposal(store, source_paths=(source_path,))
    before_source = source.read_bytes()

    result = store.commit_memory_note(proposal)

    assert result == {
        "memory_id": "M-notes",
        "target_path": proposal.target_path,
        "home_path": proposal.home_path,
        "wikilink": proposal.wikilink,
    }
    assert (tmp_path / proposal.target_path).read_text(encoding="utf-8") == proposal.markdown
    assert (tmp_path / proposal.home_path).read_text(encoding="utf-8") == proposal.home_markdown
    assert source.read_bytes() == before_source
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.tmp"))


def test_external_home_edit_conflicts_before_note_creation(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    home = tmp_path / proposal.home_path
    home.write_text(home.read_text(encoding="utf-8") + "\nExternal edit.\n", encoding="utf-8")
    external_home = home.read_bytes()

    with pytest.raises(MemoryWriteConflictError, match="changed"):
        store.commit_memory_note(proposal)

    assert home.read_bytes() == external_home
    assert not (tmp_path / proposal.target_path).exists()


def test_external_target_and_repeated_confirmation_conflict_without_overwrite(
    tmp_path: Path,
) -> None:
    first_store = MarkdownMemoryStore(tmp_path / "first")
    first_store.create_memory("Notes", "M-notes")
    external = _proposal(first_store)
    target = first_store.root / external.target_path
    target.write_text("external note", encoding="utf-8")
    with pytest.raises(MemoryWriteConflictError, match="already exists"):
        first_store.commit_memory_note(external)
    assert target.read_text(encoding="utf-8") == "external note"

    second_store = MarkdownMemoryStore(tmp_path / "second")
    second_store.create_memory("Notes", "M-notes")
    repeated = _proposal(second_store)
    second_store.commit_memory_note(repeated)
    after_first = _tree(second_store.root)
    with pytest.raises(MemoryWriteConflictError):
        second_store.commit_memory_note(repeated)
    assert _tree(second_store.root) == after_first


@pytest.mark.parametrize(
    "mutation",
    [
        lambda proposal: replace(proposal, target_path="Memories/M-notes/notes/../bad.md"),
        lambda proposal: replace(proposal, home_path="Memories/M-notes/notes/Home.md"),
        lambda proposal: replace(proposal, target_content_hash="0" * 64),
        lambda proposal: replace(proposal, wikilink="[[notes/Note-one]]"),
        lambda proposal: replace(proposal, title="Different title"),
        lambda proposal: replace(
            proposal,
            markdown=proposal.markdown.replace('type: "note"', 'type: "report"'),
        ),
        lambda proposal: replace(
            proposal,
            markdown=proposal.markdown.replace(
                "Saved without citations.",
                "[[Memories/M-other/notes/Note-other]]",
            ),
        ),
        lambda proposal: replace(
            proposal,
            markdown=proposal.markdown.replace(
                "Saved without citations.",
                "Malformed [[Memories/M-notes/notes/Note-broken",
            ),
        ),
        lambda proposal: replace(
            proposal,
            markdown=proposal.markdown.replace(
                'tags:\n  - paperpilot',
                'tags:\n  - unexpected',
            ),
        ),
        lambda proposal: replace(
            proposal,
            markdown=proposal.markdown.replace(
                'status: "confirmed"',
                'status: "confirmed"\nextra: "not fixed"',
            ),
        ),
        lambda proposal: replace(
            proposal,
            markdown=re.sub(
                r"^updated_at: .+$",
                'updated_at: "2099-01-01T00:00:00+08:00"',
                proposal.markdown,
                count=1,
                flags=re.MULTILINE,
            ),
        ),
    ],
)
def test_invalid_path_frontmatter_or_links_never_write(
    tmp_path: Path,
    mutation,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = mutation(_proposal(store))
    before = _tree(tmp_path)

    with pytest.raises(ValueError):
        store.validate_memory_note_proposal(proposal)

    assert _tree(tmp_path) == before


def test_home_replace_failure_rolls_back_note_and_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    home_before = (tmp_path / proposal.home_path).read_bytes()

    def fail_replace(target, replacement) -> Path:
        raise OSError("simulated Home replace failure")

    monkeypatch.setattr(memory_module, "_atomic_replace_preserving_old", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        store.commit_memory_note(proposal)

    assert (tmp_path / proposal.home_path).read_bytes() == home_before
    assert not (tmp_path / proposal.target_path).exists()
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.tmp"))


def test_home_change_after_final_hash_before_atomic_replace_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    home = tmp_path / proposal.home_path
    external_home = home.read_bytes() + b"\nExternal edit in final race window.\n"
    real_atomic_replace = memory_module._atomic_replace_preserving_old

    def edit_then_replace(target: Path, replacement: Path) -> Path:
        target.write_bytes(external_home)
        return real_atomic_replace(target, replacement)

    monkeypatch.setattr(
        memory_module,
        "_atomic_replace_preserving_old",
        edit_then_replace,
    )
    with pytest.raises(MemoryWriteConflictError, match="atomic replacement point"):
        store.commit_memory_note(proposal)

    assert home.read_bytes() == external_home
    assert not (tmp_path / proposal.target_path).exists()
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.tmp"))
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.bak"))


def test_second_home_edit_during_conflict_restore_is_not_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    home = tmp_path / proposal.home_path
    first_external = home.read_bytes() + b"\nFirst external edit.\n"
    second_external = first_external + b"Second external edit.\n"
    real_atomic_replace = memory_module._atomic_replace_preserving_old
    atomic_calls = 0

    def edit_around_replacements(target: Path, replacement: Path) -> Path:
        nonlocal atomic_calls
        atomic_calls += 1
        if atomic_calls == 1:
            target.write_bytes(first_external)
        elif atomic_calls == 2:
            target.write_bytes(second_external)
        return real_atomic_replace(target, replacement)

    monkeypatch.setattr(
        memory_module,
        "_atomic_replace_preserving_old",
        edit_around_replacements,
    )
    with pytest.raises(MemoryWriteConflictError, match="atomic replacement point"):
        store.commit_memory_note(proposal)

    assert atomic_calls == 3
    assert home.read_bytes() == second_external
    assert not (tmp_path / proposal.target_path).exists()
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.tmp"))
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.bak"))


def test_restore_cleanup_retry_never_repeats_a_completed_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    home = tmp_path / proposal.home_path
    external_home = home.read_bytes() + b"\nExternal edit.\n"
    real_atomic_replace = memory_module._atomic_replace_preserving_old
    real_unlink = Path.unlink
    atomic_calls = 0
    cleanup_failed_once = False

    def edit_then_replace(target: Path, replacement: Path) -> Path:
        nonlocal atomic_calls
        atomic_calls += 1
        if atomic_calls == 1:
            target.write_bytes(external_home)
        return real_atomic_replace(target, replacement)

    def fail_first_post_exchange_cleanup(path: Path, *args, **kwargs) -> None:
        nonlocal cleanup_failed_once
        if atomic_calls >= 2 and not cleanup_failed_once:
            cleanup_failed_once = True
            raise OSError("simulated transient cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        memory_module,
        "_atomic_replace_preserving_old",
        edit_then_replace,
    )
    monkeypatch.setattr(Path, "unlink", fail_first_post_exchange_cleanup)
    with pytest.raises(MemoryWriteConflictError, match="atomic replacement point"):
        store.commit_memory_note(proposal)

    assert cleanup_failed_once is True
    assert atomic_calls == 2
    assert home.read_bytes() == external_home
    assert not (tmp_path / proposal.target_path).exists()
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.tmp"))
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.bak"))


def test_continuous_home_edits_fail_safe_with_latest_recovery_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    home = tmp_path / proposal.home_path
    first_external = home.read_bytes() + b"\nFirst external edit.\n"
    second_external = first_external + b"Second external edit.\n"
    real_atomic_replace = memory_module._atomic_replace_preserving_old
    atomic_calls = 0

    def edit_before_every_exchange(target: Path, replacement: Path) -> Path:
        nonlocal atomic_calls
        atomic_calls += 1
        target.write_bytes(first_external if atomic_calls == 1 else second_external)
        return real_atomic_replace(target, replacement)

    monkeypatch.setattr(memory_module, "_MAX_HOME_RESTORE_EXCHANGES", 1)
    monkeypatch.setattr(
        memory_module,
        "_atomic_replace_preserving_old",
        edit_before_every_exchange,
    )
    with pytest.raises(MemoryWriteConflictError, match="kept changing") as caught:
        store.commit_memory_note(proposal)

    recovery_relative = str(caught.value).split("preserved at ", 1)[1]
    recovery = tmp_path / recovery_relative
    assert atomic_calls == 2
    assert home.read_bytes() == first_external
    assert recovery.read_bytes() == second_external
    assert not (tmp_path / proposal.target_path).exists()


def test_home_change_after_note_create_is_rechecked_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    home = tmp_path / proposal.home_path
    real_link = os.link

    def link_then_edit_home(source, destination) -> None:
        real_link(source, destination)
        home.write_bytes(home.read_bytes() + b"\nExternal mid-commit edit.\n")

    monkeypatch.setattr("src.research.memory.os.link", link_then_edit_home)
    with pytest.raises(MemoryWriteConflictError, match="during note commit"):
        store.commit_memory_note(proposal)

    assert home.read_bytes().endswith(b"External mid-commit edit.\n")
    assert not (tmp_path / proposal.target_path).exists()
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.tmp"))


def test_two_store_instances_from_same_snapshot_allow_exactly_one_commit(
    tmp_path: Path,
) -> None:
    first_store = MarkdownMemoryStore(tmp_path)
    second_store = MarkdownMemoryStore(tmp_path)
    first_store.create_memory("Notes", "M-notes")
    first = _proposal(first_store, note_id="Note-first")
    second = _proposal(second_store, note_id="Note-second")

    def commit(store: MarkdownMemoryStore, proposal: MemoryNoteProposal) -> str:
        try:
            store.commit_memory_note(proposal)
            return "committed"
        except MemoryWriteConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(commit, first_store, first),
            pool.submit(commit, second_store, second),
        )
        results = [future.result() for future in futures]

    assert sorted(results) == ["committed", "conflict"]
    existing = [
        proposal
        for proposal in (first, second)
        if (tmp_path / proposal.target_path).exists()
    ]
    assert len(existing) == 1
    home = (tmp_path / first.home_path).read_text(encoding="utf-8")
    assert existing[0].wikilink in home
    failed = second if existing[0] == first else first
    assert not (tmp_path / failed.target_path).exists()
    assert not list((tmp_path / "Memories" / "M-notes").rglob("*.tmp"))


def test_notes_symlink_escape_is_rejected_before_outside_write(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("Notes", "M-notes")
    proposal = _proposal(store)
    notes = store.root / "Memories" / "M-notes" / "notes"
    notes.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_directory_link(notes, outside)

    with pytest.raises(ValueError, match="escapes"):
        store.commit_memory_note(proposal)

    assert not list(outside.iterdir())

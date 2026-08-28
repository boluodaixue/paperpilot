from __future__ import annotations

import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import src.research.memory as memory_module
from src.research.memory import (
    MarkdownMemoryStore,
    MemoryWriteConflictError,
    update_memory_home_with_import,
)
from src.research.models import MemoryImportProposal
from src.research.obsidian import build_obsidian_open_uri
from src.research.vault import (
    build_attachment_wikilink,
    resolve_memory_attachment_path,
    validate_memory_attachment_path,
)


STAMP = "2026-08-28T12:00:00+08:00"


def _frontmatter(
    *,
    note_id: str,
    note_type: str,
    memory_id: str,
    title: str,
    origin: str,
    extras: tuple[tuple[str, str | int], ...] = (),
) -> str:
    lines = [
        "---",
        f'id: "{note_id}"',
        f'type: "{note_type}"',
        f'memory_id: "{memory_id}"',
        f'title: "{title}"',
        f'created_at: "{STAMP}"',
        f'updated_at: "{STAMP}"',
        f'origin: "{origin}"',
        'status: "confirmed"',
    ]
    for key, value in extras:
        rendered = str(value) if isinstance(value, int) else f'"{value}"'
        lines.append(f"{key}: {rendered}")
    lines.extend(("tags:", "  - paperpilot", "---"))
    return "\n".join(lines)


def _proposal(
    store: MarkdownMemoryStore,
    *,
    memory_id: str = "M-w5",
    raw: bytes = b"bounded import text",
    source_ref: str = "sample.txt",
    locator: str = "document",
    suffix: str = "a",
) -> MemoryImportProposal:
    digest = hashlib.sha256(raw).hexdigest()
    fingerprint_payload = json.dumps(
        {
            "source_ref": source_ref,
            "locator": locator,
            "content_hash": digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()[:32]
    import_id = f"Import-{fingerprint}"
    note_id = f"Note-import-{fingerprint}"
    prefix = f"Memories/{memory_id}"
    attachment_path = f"{prefix}/attachments/Asset-{digest}.txt"
    import_path = f"{prefix}/imports/{import_id}.md"
    note_path = f"{prefix}/notes/{note_id}.md"
    import_link = f"[[{import_path[:-3]}]]"
    note_link = f"[[{note_path[:-3]}]]"
    attachment_link = build_attachment_wikilink(
        attachment_path,
        "Original source",
    )
    import_markdown = (
        _frontmatter(
            note_id=import_id,
            note_type="import",
            memory_id=memory_id,
            title=f"Import {suffix}",
            origin="import",
            extras=(
                ("source_kind", "file"),
                ("source_ref", source_ref),
                ("locator", locator),
                ("media_type", "text/plain"),
                ("byte_size", len(raw)),
                ("content_hash", digest),
                ("attachment_path", attachment_path),
            ),
        )
        + f"\n\n# Import {suffix}\n\n- Original: {attachment_link}\n"
    )
    note_markdown = (
        _frontmatter(
            note_id=note_id,
            note_type="note",
            memory_id=memory_id,
            title=f"Import synthesis {suffix}",
            origin="import",
        )
        + f"\n\n# Import synthesis {suffix}\n\n## Sources\n\n- {import_link}\n"
    )
    home_path, home, home_hash = store.memory_home_snapshot(memory_id)
    home_markdown = update_memory_home_with_import(
        home,
        import_link,
        note_link,
        STAMP,
    )
    return MemoryImportProposal(
        proposal_id=f"ImportProposal-{suffix}",
        import_id=import_id,
        note_id=note_id,
        memory_id=memory_id,
        source_kind="file",
        source_ref=source_ref,
        locator=locator,
        media_type="text/plain",
        byte_size=len(raw),
        content_hash=digest,
        attachment_path=attachment_path,
        attachment_bytes=raw,
        import_path=import_path,
        import_markdown=import_markdown,
        import_wikilink=import_link,
        note_path=note_path,
        note_markdown=note_markdown,
        note_wikilink=note_link,
        note_source_paths=(import_path,),
        home_path=home_path,
        home_content_hash=home_hash,
        home_markdown=home_markdown,
    )


@pytest.fixture
def store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> MarkdownMemoryStore:
    monkeypatch.setattr(
        MarkdownMemoryStore,
        "_timestamp",
        staticmethod(lambda: STAMP),
    )
    value = MarkdownMemoryStore(tmp_path / "Vault")
    value.create_memory("W5 imports", "M-w5")
    return value


def _private_files(store: MarkdownMemoryStore) -> tuple[Path, ...]:
    return tuple(
        path
        for path in store.root.rglob("*")
        if path.is_file() and (path.name.endswith(".tmp") or ".bak" in path.name)
    )


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


def test_commit_creates_attachment_import_note_and_updates_home(
    store: MarkdownMemoryStore,
) -> None:
    proposal = _proposal(store)
    before = {path for path in store.root.rglob("*") if path.is_file()}

    store.validate_memory_import_proposal(proposal)
    assert {path for path in store.root.rglob("*") if path.is_file()} == before
    result = store.commit_memory_import(proposal)

    assert result["status"] == "committed"
    assert (store.root / proposal.attachment_path).read_bytes() == proposal.attachment_bytes
    assert (store.root / proposal.import_path).read_text(encoding="utf-8") == proposal.import_markdown
    assert (store.root / proposal.note_path).read_text(encoding="utf-8") == proposal.note_markdown
    home = (store.root / proposal.home_path).read_text(encoding="utf-8")
    assert proposal.import_wikilink in home
    assert proposal.note_wikilink in home
    assert not _private_files(store)


def test_exact_duplicate_is_a_zero_write_noop(store: MarkdownMemoryStore) -> None:
    proposal = _proposal(store)
    store.commit_memory_import(proposal)
    paths = tuple(path for path in store.root.rglob("*") if path.is_file())
    snapshots = {path: path.read_bytes() for path in paths}

    duplicate = store.find_memory_import(
        proposal.memory_id,
        proposal.source_ref,
        proposal.locator,
        proposal.content_hash,
    )
    result = store.commit_memory_import(proposal)

    assert duplicate is not None
    assert duplicate.note_path == proposal.note_path
    assert duplicate.wikilinks == (
        proposal.import_wikilink,
        build_attachment_wikilink(proposal.attachment_path),
        proposal.note_wikilink,
    )
    assert result["status"] == "duplicate"
    assert {path: path.read_bytes() for path in paths} == snapshots
    assert not _private_files(store)


def test_duplicate_survives_user_deleted_organization_note(
    store: MarkdownMemoryStore,
) -> None:
    proposal = _proposal(store)
    store.commit_memory_import(proposal)
    (store.root / proposal.note_path).unlink()

    duplicate = store.find_memory_import(
        proposal.memory_id,
        proposal.source_ref,
        proposal.locator,
        proposal.content_hash,
    )

    assert duplicate is not None
    assert duplicate.note_path is None


def test_import_and_attachment_without_home_marker_are_not_duplicate(
    store: MarkdownMemoryStore,
) -> None:
    proposal = _proposal(store, suffix="unlinearized")
    (store.root / proposal.attachment_path).write_bytes(proposal.attachment_bytes)
    (store.root / proposal.import_path).write_text(
        proposal.import_markdown,
        encoding="utf-8",
    )

    with pytest.raises(MemoryWriteConflictError, match="linearization point"):
        store.find_memory_import(
            proposal.memory_id,
            proposal.source_ref,
            proposal.locator,
            proposal.content_hash,
        )


def test_publish_window_never_reports_a_false_duplicate(
    store: MarkdownMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal(store, suffix="publish-window")
    observer = MarkdownMemoryStore(store.root)
    real_link = os.link
    link_calls = 0
    observed_conflict = False

    def inspect_after_import_publish(source: object, target: object) -> None:
        nonlocal link_calls, observed_conflict
        real_link(source, target)
        link_calls += 1
        if link_calls == 2:
            try:
                duplicate = observer.find_memory_import(
                    proposal.memory_id,
                    proposal.source_ref,
                    proposal.locator,
                    proposal.content_hash,
                )
            except MemoryWriteConflictError:
                observed_conflict = True
            else:
                pytest.fail(f"publish window returned false duplicate: {duplicate!r}")

    monkeypatch.setattr(memory_module.os, "link", inspect_after_import_publish)
    assert store.commit_memory_import(proposal)["status"] == "committed"
    assert observed_conflict
    assert observer.find_memory_import(
        proposal.memory_id,
        proposal.source_ref,
        proposal.locator,
        proposal.content_hash,
    ) is not None


def test_same_content_different_source_reuses_attachment(
    store: MarkdownMemoryStore,
) -> None:
    first = _proposal(store, suffix="first", source_ref="first.txt")
    store.commit_memory_import(first)
    attachment_stat = (store.root / first.attachment_path).stat()
    second = _proposal(store, suffix="second", source_ref="second.txt")

    result = store.commit_memory_import(second)

    assert result["status"] == "committed"
    assert (store.root / second.attachment_path).stat().st_ino == attachment_stat.st_ino
    assert (store.root / second.import_path).is_file()
    assert (store.root / second.note_path).is_file()


def test_attachment_creator_rollback_preserves_other_linearized_import(
    store: MarkdownMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = _proposal(store, source_ref="creator.txt", suffix="creator")
    reuser = _proposal(store, source_ref="reuser.txt", suffix="reuser")
    other = MarkdownMemoryStore(store.root)
    real_link = os.link
    injected = False
    reuser_result: dict[str, object] | None = None

    def commit_reuser_after_attachment(source: object, target: object) -> None:
        nonlocal injected, reuser_result
        real_link(source, target)
        if Path(target) == store.root / creator.attachment_path and not injected:
            injected = True
            reuser_result = other.commit_memory_import(reuser)

    monkeypatch.setattr(memory_module.os, "link", commit_reuser_after_attachment)
    with pytest.raises(MemoryWriteConflictError, match="Home.md changed"):
        store.commit_memory_import(creator)

    assert reuser_result is not None and reuser_result["status"] == "committed"
    assert (store.root / reuser.attachment_path).read_bytes() == reuser.attachment_bytes
    assert (store.root / reuser.import_path).is_file()
    assert (store.root / reuser.note_path).is_file()
    assert not (store.root / creator.import_path).exists()
    assert not (store.root / creator.note_path).exists()
    assert reuser.import_wikilink in (store.root / reuser.home_path).read_text(
        encoding="utf-8"
    )
    assert not _private_files(store)


def test_exact_interleave_preserves_attachment_and_returns_duplicate(
    store: MarkdownMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal(store, suffix="exact-interleave")
    other = MarkdownMemoryStore(store.root)
    real_link = os.link
    injected = False
    winner_result: dict[str, object] | None = None

    def commit_winner_after_attachment(source: object, target: object) -> None:
        nonlocal injected, winner_result
        real_link(source, target)
        if Path(target) == store.root / proposal.attachment_path and not injected:
            injected = True
            winner_result = other.commit_memory_import(proposal)

    monkeypatch.setattr(memory_module.os, "link", commit_winner_after_attachment)
    loser_result = store.commit_memory_import(proposal)

    assert winner_result is not None and winner_result["status"] == "committed"
    assert loser_result["status"] == "duplicate"
    assert (store.root / proposal.attachment_path).read_bytes() == proposal.attachment_bytes
    assert (store.root / proposal.import_path).is_file()
    assert (store.root / proposal.note_path).is_file()
    assert proposal.import_wikilink in (store.root / proposal.home_path).read_text(
        encoding="utf-8"
    )
    assert not _private_files(store)


def test_same_source_locator_with_changed_content_is_a_new_version(
    store: MarkdownMemoryStore,
) -> None:
    first = _proposal(store)
    store.commit_memory_import(first)
    changed = _proposal(store, raw=b"changed", suffix="changed")

    assert store.find_memory_import(
        "M-w5", "sample.txt", "document", changed.content_hash
    ) is None
    assert store.commit_memory_import(changed)["status"] == "committed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_hash", "0" * 64),
        ("byte_size", 999),
        ("attachment_path", "Memories/M-w5/attachments/Asset-bad.txt"),
        ("import_path", "Memories/M-w5/notes/Import-a.md"),
        ("note_path", "Memories/M-other/notes/Note-import-a.md"),
        ("note_source_paths", ()),
    ],
)
def test_invalid_proposal_is_rejected_without_writes(
    store: MarkdownMemoryStore,
    field: str,
    value: object,
) -> None:
    proposal = replace(_proposal(store), **{field: value})
    before = tuple(path for path in store.root.rglob("*") if path.is_file())

    with pytest.raises(ValueError):
        store.validate_memory_import_proposal(proposal)

    assert tuple(path for path in store.root.rglob("*") if path.is_file()) == before


def test_frontmatter_and_wikilink_tampering_is_rejected(
    store: MarkdownMemoryStore,
) -> None:
    proposal = _proposal(store)
    extra = replace(
        proposal,
        import_markdown=proposal.import_markdown.replace(
            "tags:\n",
            'unexpected: "field"\ntags:\n',
        ),
    )
    bad_note_link = replace(
        proposal,
        note_markdown=proposal.note_markdown.replace(
            proposal.import_wikilink,
            "[[Memories/M-other/imports/Import-a]]",
        ),
    )
    forged_identity = replace(
        proposal,
        import_id="Import-" + "0" * 32,
    )

    with pytest.raises(ValueError, match="fixed fields"):
        store.validate_memory_import_proposal(extra)
    with pytest.raises(ValueError):
        store.validate_memory_import_proposal(bad_note_link)
    with pytest.raises(ValueError, match="derived from"):
        store.validate_memory_import_proposal(forged_identity)


def test_home_conflict_and_replace_failure_leave_no_half_batch(
    store: MarkdownMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _proposal(store, suffix="stale")
    home = store.root / stale.home_path
    home.write_text(home.read_text(encoding="utf-8") + "\nexternal\n", encoding="utf-8")
    with pytest.raises(MemoryWriteConflictError):
        store.commit_memory_import(stale)
    assert not (store.root / stale.import_path).exists()

    current = _proposal(store, suffix="failure")

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated Home replace failure")

    monkeypatch.setattr(memory_module, "_atomic_replace_preserving_old", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        store.commit_memory_import(current)
    assert not (store.root / current.attachment_path).exists()
    assert not (store.root / current.import_path).exists()
    assert not (store.root / current.note_path).exists()
    assert not _private_files(store)


@pytest.mark.parametrize("failed_link", (1, 2, 3))
def test_each_creation_failure_rolls_back_the_batch(
    store: MarkdownMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    failed_link: int,
) -> None:
    proposal = _proposal(store, suffix=f"failure-{failed_link}")
    original_home = (store.root / proposal.home_path).read_bytes()
    real_link = os.link
    calls = 0

    def fail_selected_link(source: object, target: object) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_link:
            raise OSError("simulated creation failure")
        real_link(source, target)

    monkeypatch.setattr(memory_module.os, "link", fail_selected_link)
    with pytest.raises(OSError, match="simulated creation failure"):
        store.commit_memory_import(proposal)

    assert (store.root / proposal.home_path).read_bytes() == original_home
    assert not (store.root / proposal.attachment_path).exists()
    assert not (store.root / proposal.import_path).exists()
    assert not (store.root / proposal.note_path).exists()
    assert not _private_files(store)


@pytest.mark.parametrize("failed_temp", (1, 2, 3, 4))
def test_each_temp_preparation_failure_leaves_no_files(
    store: MarkdownMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    failed_temp: int,
) -> None:
    proposal = _proposal(store, suffix=f"temp-{failed_temp}")
    original = store._write_commit_temp
    calls = 0

    def fail_selected_temp(parent: Path, name: str, content: str | bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == failed_temp:
            raise OSError("simulated temp failure")
        return original(parent, name, content)

    monkeypatch.setattr(store, "_write_commit_temp", fail_selected_temp)
    with pytest.raises(OSError, match="simulated temp failure"):
        store.commit_memory_import(proposal)

    assert not (store.root / proposal.attachment_path).exists()
    assert not (store.root / proposal.import_path).exists()
    assert not (store.root / proposal.note_path).exists()
    assert not _private_files(store)


def test_partial_external_targets_conflict_without_overwrite(
    store: MarkdownMemoryStore,
) -> None:
    attachment_conflict = _proposal(store, suffix="attachment-conflict")
    attachment = store.root / attachment_conflict.attachment_path
    attachment.write_bytes(b"external-different-content")
    with pytest.raises(MemoryWriteConflictError, match="different content"):
        store.commit_memory_import(attachment_conflict)
    assert attachment.read_bytes() == b"external-different-content"
    assert not (store.root / attachment_conflict.import_path).exists()

    import_conflict = _proposal(
        store,
        suffix="import-conflict",
        raw=b"second external target",
        source_ref="second.txt",
    )
    import_target = store.root / import_conflict.import_path
    import_target.write_text("external import", encoding="utf-8")
    with pytest.raises(MemoryWriteConflictError, match="import target"):
        store.commit_memory_import(import_conflict)
    assert import_target.read_text(encoding="utf-8") == "external import"
    assert not (store.root / import_conflict.attachment_path).exists()
    assert not (store.root / import_conflict.note_path).exists()
    assert not _private_files(store)


def test_home_edit_at_atomic_point_is_restored_and_batch_is_rolled_back(
    store: MarkdownMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = _proposal(store, suffix="atomic-race")
    home = store.root / proposal.home_path
    external = home.read_text(encoding="utf-8") + "\nexternal-at-atomic-point\n"
    real_replace = memory_module._atomic_replace_preserving_old
    injected = False

    def inject_edit(target: Path, replacement: Path) -> Path:
        nonlocal injected
        if not injected:
            injected = True
            target.write_text(external, encoding="utf-8")
        return real_replace(target, replacement)

    monkeypatch.setattr(memory_module, "_atomic_replace_preserving_old", inject_edit)
    with pytest.raises(MemoryWriteConflictError, match="atomic replacement point"):
        store.commit_memory_import(proposal)

    assert home.read_text(encoding="utf-8") == external
    assert not (store.root / proposal.attachment_path).exists()
    assert not (store.root / proposal.import_path).exists()
    assert not (store.root / proposal.note_path).exists()
    assert not _private_files(store)


def test_two_store_instances_same_import_are_one_commit_and_one_duplicate(
    store: MarkdownMemoryStore,
) -> None:
    proposal = _proposal(store, suffix="same-race")
    other = MarkdownMemoryStore(store.root)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda value: value.commit_memory_import(proposal),
                (store, other),
            )
        )

    assert sorted(result["status"] for result in results) == [
        "committed",
        "duplicate",
    ]
    assert not _private_files(store)


def test_two_store_instances_different_imports_from_same_home_conflict(
    store: MarkdownMemoryStore,
) -> None:
    first = _proposal(store, suffix="race-one", source_ref="one.txt")
    second = _proposal(store, suffix="race-two", source_ref="two.txt")
    other = MarkdownMemoryStore(store.root)

    def commit(value: tuple[MarkdownMemoryStore, MemoryImportProposal]) -> str:
        target_store, proposal = value
        try:
            return str(target_store.commit_memory_import(proposal)["status"])
        except MemoryWriteConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = tuple(executor.map(commit, ((store, first), (other, second))))

    assert sorted(statuses) == ["committed", "conflict"]
    committed = first if (store.root / first.import_path).exists() else second
    rejected = second if committed is first else first
    assert not (store.root / rejected.import_path).exists()
    assert not (store.root / rejected.note_path).exists()
    assert not _private_files(store)


def test_attachment_contract_and_obsidian_uri(store: MarkdownMemoryStore) -> None:
    proposal = _proposal(store)
    store.commit_memory_import(proposal)

    assert validate_memory_attachment_path(
        proposal.attachment_path,
        memory_id="M-w5",
    ) == proposal.attachment_path
    uri = build_obsidian_open_uri(
        store.root,
        proposal.attachment_path,
        vault_name="Research Vault",
    )
    assert "vault=Research%20Vault" in uri
    assert "%2Fattachments%2F" in uri
    assert uri.endswith(".txt")
    for invalid in (
        "../Asset-" + "0" * 64 + ".txt",
        "Memories/M-w5/attachments/nested/Asset-" + "0" * 64 + ".txt",
        "Memories/M-w5/attachments/Asset-" + "0" * 64 + ".exe",
        "Memories/M-other/attachments/Asset-" + "0" * 64 + ".txt",
    ):
        with pytest.raises(ValueError):
            resolve_memory_attachment_path(store.root, invalid, memory_id="M-w5")


def test_attachment_junction_or_symlink_escape_is_rejected(
    store: MarkdownMemoryStore,
    tmp_path: Path,
) -> None:
    attachments = store.root / "Memories" / "M-w5" / "attachments"
    outside = tmp_path / "outside"
    outside.mkdir()
    attachments.rmdir()
    _make_directory_link(attachments, outside)
    path = f"Memories/M-w5/attachments/Asset-{'0' * 64}.txt"

    with pytest.raises(ValueError, match="symlink or junction"):
        resolve_memory_attachment_path(store.root, path, memory_id="M-w5")


def test_attachment_link_to_another_directory_in_same_memory_is_rejected(
    store: MarkdownMemoryStore,
) -> None:
    attachments = store.root / "Memories" / "M-w5" / "attachments"
    notes = store.root / "Memories" / "M-w5" / "notes"
    attachments.rmdir()
    _make_directory_link(attachments, notes)
    path = f"Memories/M-w5/attachments/Asset-{'0' * 64}.txt"

    with pytest.raises(ValueError, match="symlink or junction"):
        resolve_memory_attachment_path(store.root, path, memory_id="M-w5")

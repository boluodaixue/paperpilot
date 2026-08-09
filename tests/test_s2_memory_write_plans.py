from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from src.research.memory import (
    MarkdownMemoryStore,
    update_memory_home_with_import,
    update_memory_home_with_note,
)
from src.research.memory_write_plans import (
    MemoryWritePlan,
    build_create_memory_plan,
    build_legacy_copy_plan,
    build_memory_import_plan,
    build_memory_note_plan,
    build_report_review_plan,
    build_research_bundle_plan,
)
from src.research.models import (
    EvidenceItem,
    ExecutionIdentity,
    MemoryImportProposal,
    MemoryNoteProposal,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
)
from src.research.vault import build_attachment_wikilink, build_wikilink
from src.research.vault_write_queue import VaultWriteQueue
from src.research.vault_writer import canonical_command_hash


STAMP = "2026-08-29T12:00:00+08:00"


def _identity(thread_id: str = "root-plan") -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _brief(memory_id: str = "M-plan") -> ResearchBrief:
    return ResearchBrief(
        question="What is supported?",
        objective="Explain the evidence",
        scope=("primary source",),
        directions=("verify",),
        constraints=("cite",),
        expected_output="report",
        memory_id=memory_id,
    )


def _result() -> ResearchResult:
    evidence = EvidenceItem(
        evidence_id="E-plan",
        finding="The plan is supported.",
        source_type="web",
        title="Primary source",
        source_ref="https://example.test/source",
        locator="section 1",
        excerpt="Supported text.",
        excerpt_type="quote",
    )
    return ResearchResult(
        task_id="task-plan",
        status=ResearchStatus.COMPLETED,
        summary="Supported summary.",
        findings=(evidence.finding,),
        evidence=(evidence,),
    )


def _store(tmp_path: Path, memory_id: str = "M-plan") -> MarkdownMemoryStore:
    store = MarkdownMemoryStore(tmp_path / "Vault")
    store.create_memory("Planning", memory_id)
    return store


def _note_proposal(store: MarkdownMemoryStore) -> MemoryNoteProposal:
    memory_id = "M-plan"
    note_id = "Note-plan"
    target = f"Memories/{memory_id}/notes/{note_id}.md"
    link = build_wikilink(target)
    markdown = (
        "---\n"
        f'id: "{note_id}"\n'
        'type: "note"\n'
        f'memory_id: "{memory_id}"\n'
        'title: "Planned note"\n'
        f'created_at: "{STAMP}"\n'
        f'updated_at: "{STAMP}"\n'
        'origin: "conversation"\n'
        'status: "confirmed"\n'
        "tags:\n  - paperpilot\n"
        "---\n\n# Planned note\n"
    )
    home_path, current_home, home_hash = store.memory_home_snapshot(memory_id)
    return MemoryNoteProposal(
        proposal_id="Proposal-plan",
        answer_id="Answer-plan",
        memory_id=memory_id,
        note_id=note_id,
        title="Planned note",
        target_path=target,
        markdown=markdown,
        wikilink=link,
        source_paths=(),
        home_path=home_path,
        home_content_hash=home_hash,
        target_content_hash=None,
        home_markdown=update_memory_home_with_note(
            current_home, link, STAMP
        ),
    )


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
        lines.append(f"{key}: {value}" if isinstance(value, int) else f'{key}: "{value}"')
    lines.extend(("tags:", "  - paperpilot", "---"))
    return "\n".join(lines)


def _import_proposal(store: MarkdownMemoryStore) -> MemoryImportProposal:
    memory_id = "M-plan"
    raw = b"bounded import text"
    content_hash = hashlib.sha256(raw).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "source_ref": "sample.txt",
                "locator": "document",
                "content_hash": content_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]
    import_id = f"Import-{fingerprint}"
    note_id = f"Note-import-{fingerprint}"
    prefix = f"Memories/{memory_id}"
    attachment_path = f"{prefix}/attachments/Asset-{content_hash}.txt"
    import_path = f"{prefix}/imports/{import_id}.md"
    note_path = f"{prefix}/notes/{note_id}.md"
    import_link = build_wikilink(import_path)
    note_link = build_wikilink(note_path)
    attachment_link = build_attachment_wikilink(attachment_path, "Original source")
    import_markdown = _frontmatter(
        note_id=import_id,
        note_type="import",
        memory_id=memory_id,
        title="Import",
        origin="import",
        extras=(
            ("source_kind", "file"),
            ("source_ref", "sample.txt"),
            ("locator", "document"),
            ("media_type", "text/plain"),
            ("byte_size", len(raw)),
            ("content_hash", content_hash),
            ("attachment_path", attachment_path),
        ),
    ) + f"\n\n# Import\n\n- Original: {attachment_link}\n"
    note_markdown = _frontmatter(
        note_id=note_id,
        note_type="note",
        memory_id=memory_id,
        title="Import synthesis",
        origin="import",
    ) + f"\n\n# Import synthesis\n\n## Sources\n\n- {import_link}\n"
    home_path, home, home_hash = store.memory_home_snapshot(memory_id)
    return MemoryImportProposal(
        proposal_id="ImportProposal-plan",
        import_id=import_id,
        note_id=note_id,
        memory_id=memory_id,
        source_kind="file",
        source_ref="sample.txt",
        locator="document",
        media_type="text/plain",
        byte_size=len(raw),
        content_hash=content_hash,
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
        home_markdown=update_memory_home_with_import(
            home, import_link, note_link, STAMP
        ),
    )


def _write_legacy(vault: Path) -> dict[str, bytes]:
    contents = {
        "sources/Source-fixed.md": (
            '---\nid: "Source-fixed"\ntype: "source"\n---\n\n# Source\n'
        ),
        "evidence/E-fixed.md": (
            '---\nid: "E-fixed"\ntype: "evidence"\n---\n\n'
            "# Evidence\n\n[[sources/Source-fixed|Source]]\n"
        ),
        "reports/Report-fixed.md": (
            '---\nid: "Report-fixed"\ntype: "report"\n'
            'root_thread_id: "legacy-root"\n---\n\n'
            "# Report\n\n[[evidence/E-fixed|Evidence]]\n"
        ),
    }
    for relative, markdown in contents.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8", newline="\n")
    return {relative: (vault / relative).read_bytes() for relative in contents}


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _payload(plan: MemoryWritePlan) -> dict[str, object]:
    value = json.loads(plan.command_blob)
    assert isinstance(value, dict)
    return value


def _targets(plan: MemoryWritePlan) -> dict[str, dict[str, object]]:
    raw = _payload(plan)["targets"]
    assert isinstance(raw, list)
    return {str(item["path"]): item for item in raw}


def test_create_command_is_stable_complete_and_round_trips() -> None:
    first = build_create_memory_plan(
        memory_id="M-created", title="Created", created_at=STAMP
    )
    second = build_create_memory_plan(
        memory_id="M-created", title="Created", created_at=STAMP
    )

    assert first == second
    payload = _payload(first)
    assert first.operation_type == "create_memory"
    assert payload["publish"] == "directory_create"
    assert payload["anchor"] == "Memories/M-created"
    assert len(payload["directories"]) == 6
    assert "descriptor" not in payload["result"]
    assert payload["result"]["memory_id"] == "M-created"
    assert payload["result"]["request_hash"] == payload["input_hashes"]["request"]
    assert first.command_hash == hashlib.sha256(first.command_blob).hexdigest()
    assert canonical_command_hash(first.command_blob) == first.command_hash
    assert first.enqueue_kwargs()["command_blob"] == first.command_blob


def test_command_deserialization_rejects_content_hash_tampering() -> None:
    command = build_create_memory_plan(
        memory_id="M-created", title="Created", created_at=STAMP
    )
    value = _payload(command)
    value["targets"][0]["content_hash"] = "0" * 64

    with pytest.raises(ValueError, match="content hash does not match"):
        canonical_command_hash(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        )


def test_plan_metadata_enqueues_the_same_opaque_canonical_command(
    tmp_path: Path,
) -> None:
    plan = build_create_memory_plan(
        memory_id="M-created", title="Created", created_at=STAMP
    )
    queue = VaultWriteQueue(tmp_path / "runtime.db", vault_scope="vault-plans")

    job = queue.enqueue(**plan.enqueue_kwargs())

    assert job.job_id == plan.job_id
    assert job.idempotency_key == plan.idempotency_key
    assert job.operation_type == plan.operation_type
    assert job.memory_id == plan.memory_id
    assert job.command_blob == plan.command_blob
    assert job.command_hash == plan.command_hash


def test_research_plan_uses_report_anchor_and_never_updates_home(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before = _files(store.root)

    command = build_research_bundle_plan(
        store,
        _brief(),
        _result(),
        _identity(),
        memory_id="M-plan",
        created_at=STAMP,
    )

    assert _files(store.root) == before
    payload = _payload(command)
    assert command.operation_type == "research_bundle"
    assert payload["publish"] == "file_bundle"
    assert payload["anchor"].startswith("Memories/M-plan/reports/Report-")
    assert payload["anchor"].endswith(".md")
    targets = _targets(command)
    assert "Memories/M-plan/Home.md" not in targets
    assert payload["anchor"] in targets
    assert payload["result"]["request_hash"] == payload["input_hashes"]["request"]
    assert {PurePosixPath(path).parts[2] for path in targets} == {
        "sources",
        "evidence",
        "reports",
    }


def test_research_plan_captures_reuse_and_expected_old_hashes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = build_research_bundle_plan(
        store, _brief(), _result(), _identity(), memory_id="M-plan", created_at=STAMP
    )
    first_targets = _targets(first)
    first_paths = sorted(first_targets)
    reusable_path_value, changed_path_value = first_paths[0], first_paths[1]
    reusable_path = store.root / reusable_path_value
    reusable_path.write_bytes(base64.b64decode(first_targets[reusable_path_value]["content_b64"]))
    changed_path = store.root / changed_path_value
    changed_path.write_text("external edit", encoding="utf-8")

    second = build_research_bundle_plan(
        store, _brief(), _result(), _identity(), memory_id="M-plan", created_at=STAMP
    )
    targets = _targets(second)

    assert targets[reusable_path_value]["expected_mode"] == "reuse"
    assert targets[reusable_path_value]["expected_hash"] is None
    assert targets[changed_path_value]["expected_mode"] == "hash"
    assert targets[changed_path_value]["expected_hash"] == hashlib.sha256(
        b"external edit"
    ).hexdigest()


def test_report_review_plan_uses_explicit_original_hash_and_is_read_only(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    original, manifest = store.persist_research(
        _brief(), _result(), _identity(), memory_id="M-plan"
    )
    revised = original + "\nReview clarification.\n"
    before = _files(store.root)

    command = build_report_review_plan(
        store,
        memory_id="M-plan",
        original_markdown=original,
        revised_markdown=revised,
        manifest=manifest,
    )

    assert _files(store.root) == before
    expected = hashlib.sha256(original.encode()).hexdigest()
    target = _targets(command)[manifest.report_path]
    assert command.operation_type == "report_review"
    assert target["expected_mode"] == "hash"
    assert target["expected_hash"] == expected
    assert target["content_hash"] == hashlib.sha256(revised.encode()).hexdigest()

    (store.root / manifest.report_path).write_text(
        original + "\nObsidian edit.\n", encoding="utf-8"
    )
    replay = build_report_review_plan(
        store,
        memory_id="M-plan",
        original_markdown=original,
        revised_markdown=revised,
        manifest=manifest,
    )
    assert replay == command


def test_note_plan_has_absent_note_and_home_cas_anchor_without_writes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    proposal = _note_proposal(store)
    before = _files(store.root)

    command = build_memory_note_plan(store, proposal)

    assert _files(store.root) == before
    payload = _payload(command)
    targets = _targets(command)
    assert command.operation_type == "memory_note"
    assert payload["expected_home_hash"] == proposal.home_content_hash
    assert targets[proposal.target_path]["expected_mode"] == "absent"
    assert targets[proposal.home_path]["expected_mode"] == "hash"
    assert payload["anchor"] == proposal.home_path


def test_import_plan_is_content_addressed_and_home_anchored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = _import_proposal(store)
    before = _files(store.root)

    command = build_memory_import_plan(store, proposal)

    assert _files(store.root) == before
    payload = _payload(command)
    targets = _targets(command)
    assert command.operation_type == "memory_import"
    assert targets[proposal.attachment_path]["expected_mode"] == "reuse"
    assert targets[proposal.import_path]["expected_mode"] == "absent"
    assert targets[proposal.note_path]["expected_mode"] == "absent"
    assert targets[proposal.home_path]["expected_mode"] == "hash"
    assert targets[proposal.attachment_path]["content_hash"] == proposal.content_hash
    assert payload["anchor"] == proposal.home_path
    assert payload["expected_home_hash"] == proposal.home_content_hash


def test_import_plan_marks_an_exact_existing_attachment_reusable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    proposal = _import_proposal(store)
    attachment = store.root / proposal.attachment_path
    attachment.write_bytes(proposal.attachment_bytes)

    command = build_memory_import_plan(store, proposal)

    target = _targets(command)[proposal.attachment_path]
    assert target["expected_mode"] == "reuse"
    assert target["expected_hash"] is None


def test_legacy_plan_is_copy_only_directory_publish_and_does_not_touch_source(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "Vault"
    original = _write_legacy(vault)
    store = MarkdownMemoryStore(vault)
    proposal = store.prepare_legacy_memory_migration("Migrated", "M-migrated")
    before = _files(vault)

    command = build_legacy_copy_plan(store, proposal)

    assert _files(vault) == before
    assert {path: (vault / path).read_bytes() for path in original} == original
    payload = _payload(command)
    assert command.operation_type == "legacy_copy"
    assert command.memory_id == "M-migrated"
    assert payload["publish"] == "directory_create"
    assert payload["anchor"] == "Memories/M-migrated"
    assert len(payload["directories"]) == 6
    assert "descriptor" not in payload["result"]
    assert payload["result"]["home_path"] == "Memories/M-migrated/Home.md"
    assert all(target["expected_mode"] == "absent" for target in payload["targets"])
    assert not (vault / "Memories/M-migrated").exists()


def test_managed_plans_never_turn_m_legacy_into_a_writable_scope() -> None:
    with pytest.raises(ValueError, match="read-only|reserved"):
        build_create_memory_plan(
            memory_id="M-legacy", title="Legacy", created_at=STAMP
        )

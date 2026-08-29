from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.research.memory import MarkdownMemoryStore, MemoryWriteConflictError
from src.research.memory_import import prepare_memory_text_import
from src.research.memory_write_plans import build_create_memory_plan
from src.research.models import (
    EvidenceItem,
    ExecutionIdentity,
    MemoryImportProposal,
    MemoryNoteProposal,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
)
from src.research.vault_write_queue import VaultWriteQueue
from src.research.vault_write_service import VaultWriteService, _WriterHeartbeat
from src.research.vault_writer import VaultWriter


def _service(
    tmp_path: Path,
    *,
    lease_seconds: float = 60.0,
) -> tuple[MarkdownMemoryStore, VaultWriteQueue, VaultWriteService]:
    vault = tmp_path / "vault"
    store = MarkdownMemoryStore(vault)
    queue = VaultWriteQueue(
        tmp_path / "runtime.db",
        vault_scope=f"scope-{tmp_path.name}",
        poll_interval_seconds=0.005,
    )
    writer = VaultWriter(vault, queue, job_lease_seconds=lease_seconds)
    service = VaultWriteService(
        store,
        queue,
        writer,
        lease_seconds=lease_seconds,
        coordination_interval_seconds=0.01,
        wait_timeout_seconds=2,
        startup_timeout_seconds=2,
    )
    return store, queue, service


def _brief(memory_id: str) -> ResearchBrief:
    return ResearchBrief(
        question="How does attention work?",
        objective="Explain the evidence",
        scope=("architecture",),
        directions=("primary sources",),
        constraints=("cite locations",),
        expected_output="report",
        memory_id=memory_id,
    )


def _research_result() -> ResearchResult:
    evidence = EvidenceItem(
        evidence_id="E-attention",
        finding="Attention replaces recurrence.",
        source_type="web",
        title="Attention source",
        source_ref="https://example.test/attention",
        locator="section 1",
        excerpt="Attention is sufficient.",
        excerpt_type="quote",
    )
    return ResearchResult(
        task_id="root-task",
        status=ResearchStatus.COMPLETED,
        summary="Attention is central.",
        findings=(evidence.finding,),
        evidence=(evidence,),
    )


def _identity(thread_id: str = "research-thread") -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _note_proposal(store: MarkdownMemoryStore, memory_id: str) -> MemoryNoteProposal:
    home_path, home, home_hash = store.memory_home_snapshot(memory_id)
    timestamp = store._timestamp()
    note_id = "Note-service"
    target_path = f"Memories/{memory_id}/notes/{note_id}.md"
    wikilink = f"[[{target_path[:-3]}]]"
    markdown = (
        "---\n"
        f'id: "{note_id}"\n'
        'type: "note"\n'
        f'memory_id: "{memory_id}"\n'
        'title: "Service note"\n'
        f'created_at: "{timestamp}"\n'
        f'updated_at: "{timestamp}"\n'
        'origin: "conversation"\n'
        'status: "confirmed"\n'
        "tags:\n  - paperpilot\n"
        "---\n\n"
        "# Service note\n\nA durable note.\n"
    )
    return MemoryNoteProposal(
        proposal_id="Proposal-service-note",
        answer_id="Answer-service",
        memory_id=memory_id,
        note_id=note_id,
        title="Service note",
        target_path=target_path,
        markdown=markdown,
        wikilink=wikilink,
        source_paths=(),
        home_path=home_path,
        home_content_hash=home_hash,
        target_content_hash=None,
        home_markdown=store.update_memory_home_with_note(home, wikilink, timestamp),
    )


class _ImportPolicy:
    async def __call__(self, messages, tools):
        del tools
        context = json.loads(messages[-1]["content"].split("IMPORT_CONTEXT_JSON:\n", 1)[1])
        locator = context["excerpts"][0]["locator"]
        return {
            "content": json.dumps(
                {
                    "title": "Imported source",
                    "summary": "A bounded imported summary.",
                    "support": [
                        {
                            "text": "The source supports one point.",
                            "locators": [locator],
                            "memory_paths": [],
                        }
                    ],
                    "conflicts": [],
                    "gaps": [],
                }
            )
        }


def _write_legacy(vault: Path) -> None:
    values = {
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
            "# Legacy\n\n[[evidence/E-fixed|Evidence]]\n"
        ),
    }
    for relative, content in values.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_create_memory_is_queued_idempotent_and_reconstructs_descriptor(
    tmp_path: Path,
) -> None:
    store, queue, service = _service(tmp_path)

    first = service.create_memory("Service Memory", "M-service")
    repeated = service.create_memory("Service Memory", "M-service")

    assert repeated == first == store.get_memory("M-service")
    jobs = queue.list()
    assert len(jobs) == 1
    assert jobs[0].operation_type == "create_memory"
    assert jobs[0].status == "succeeded"
    assert jobs[0].command_blob is None
    assert jobs[0].result is not None
    assert "descriptor" not in jobs[0].result


def test_create_memory_rejects_same_key_with_different_title(tmp_path: Path) -> None:
    store, queue, service = _service(tmp_path)
    service.create_memory("Original", "M-create-collision")

    with pytest.raises(ValueError, match="idempotency key collision"):
        service.create_memory("Different", "M-create-collision")

    assert store.get_memory("M-create-collision").title == "Original"
    assert len(queue.list()) == 1


def test_create_memory_replay_reads_latest_descriptor_from_home(tmp_path: Path) -> None:
    store, queue, service = _service(tmp_path)
    service.create_memory("Original", "M-home-truth")
    home = store.root / "Memories/M-home-truth/Home.md"
    home.write_text(
        home.read_text(encoding="utf-8").replace(
            'title: "Original"', 'title: "Obsidian title"'
        ),
        encoding="utf-8",
    )

    replayed = service.create_memory("Original", "M-home-truth")

    assert replayed.title == "Obsidian title"
    assert queue.list()[0].result is not None
    assert "descriptor" not in queue.list()[0].result


def test_managed_research_and_report_review_use_writer_and_rebuild_manifest(
    tmp_path: Path,
) -> None:
    store, queue, service = _service(tmp_path)
    service.create_memory("Research", "M-research")

    report, manifest = service.persist_research(
        _brief("M-research"),
        _research_result(),
        _identity(),
        memory_id="M-research",
        created_at="2026-08-28T00:00:00+08:00",
    )
    replay_report, replay_manifest = service.persist_research(
        _brief("M-research"),
        _research_result(),
        _identity(),
        memory_id="M-research",
    )

    assert replay_report == report
    assert replay_manifest == manifest
    assert store.read_text(manifest.report_path) == report
    revised = report + "\nAdditional synthesis without changing citations.\n"
    service.replace_report(
        manifest.report_path,
        revised,
        original_markdown=report,
        manifest=manifest,
        origin_thread_id="research-thread",
    )
    assert store.read_text(manifest.report_path) == revised
    assert [job.operation_type for job in queue.list()] == [
        "create_memory",
        "research_bundle",
        "report_review",
    ]


def test_research_rejects_same_thread_key_with_different_inputs(
    tmp_path: Path,
) -> None:
    _store, queue, service = _service(tmp_path)
    service.create_memory("Research", "M-research-collision")
    brief = _brief("M-research-collision")
    service.persist_research(
        brief,
        _research_result(),
        _identity("same-root"),
        memory_id="M-research-collision",
    )

    changed = replace(_research_result(), summary="A different research result.")
    with pytest.raises(ValueError, match="idempotency key collision"):
        service.persist_research(
            brief,
            changed,
            _identity("same-root"),
            memory_id="M-research-collision",
        )

    assert [job.operation_type for job in queue.list()].count("research_bundle") == 1


def test_report_review_exact_replay_requires_receipt_and_preserves_external_edit(
    tmp_path: Path,
) -> None:
    store, queue, service = _service(tmp_path)
    service.create_memory("Review", "M-review-replay")
    original, manifest = service.persist_research(
        _brief("M-review-replay"),
        _research_result(),
        _identity("review-replay-root"),
        memory_id="M-review-replay",
        created_at="2026-08-28T00:00:00+08:00",
    )
    revised = original + "\nA deterministic reviewed sentence.\n"

    service.replace_report(
        manifest.report_path,
        revised,
        memory_id="M-review-replay",
        original_markdown=original,
        manifest=manifest,
        origin_thread_id="review-replay-root",
    )
    service.replace_report(
        manifest.report_path,
        revised,
        memory_id="M-review-replay",
        original_markdown=original,
        manifest=manifest,
        origin_thread_id="review-replay-root",
    )

    review_jobs = [job for job in queue.list() if job.operation_type == "report_review"]
    assert len(review_jobs) == 1
    assert review_jobs[0].status == "succeeded"
    assert review_jobs[0].result is not None
    assert review_jobs[0].result["revised_hash"]

    external = revised + "\nObsidian changed this after review.\n"
    (store.root / manifest.report_path).write_text(external, encoding="utf-8")
    with pytest.raises(MemoryWriteConflictError, match="after the report review"):
        service.replace_report(
            manifest.report_path,
            revised,
            memory_id="M-review-replay",
            original_markdown=original,
            manifest=manifest,
            origin_thread_id="review-replay-root",
        )
    assert store.read_text(manifest.report_path) == external


@pytest.mark.asyncio
async def test_note_import_and_legacy_copy_return_store_compatible_results(
    tmp_path: Path,
) -> None:
    store, queue, service = _service(tmp_path)
    service.create_memory("Notes", "M-notes")
    note = _note_proposal(store, "M-notes")

    note_result = service.commit_memory_note(note, origin_thread_id="note-thread")
    assert note_result == {
        "memory_id": "M-notes",
        "target_path": note.target_path,
        "home_path": note.home_path,
        "wikilink": note.wikilink,
    }

    service.create_memory("Imports", "M-imports")
    proposal = await prepare_memory_text_import(
        store,
        _ImportPolicy(),
        "M-imports",
        "Inline",
        "alpha\nbeta",
    )
    assert isinstance(proposal, MemoryImportProposal)
    import_result = service.commit_memory_import(
        proposal, origin_thread_id="import-thread"
    )
    assert import_result["status"] == "committed"
    assert isinstance(import_result["wikilinks"], tuple)
    assert (store.root / proposal.attachment_path).read_bytes() == b"alpha\nbeta"

    _write_legacy(store.root)
    legacy = store.prepare_legacy_memory_migration("Migrated", "M-migrated")
    descriptor = service.commit_legacy_memory_migration(
        legacy, origin_thread_id="legacy-thread"
    )
    assert descriptor == store.get_memory("M-migrated")
    migrated_home = store.root / "Memories/M-migrated/Home.md"
    migrated_home.write_text(
        migrated_home.read_text(encoding="utf-8").replace(
            'title: "Migrated"', 'title: "Obsidian migrated"'
        ),
        encoding="utf-8",
    )
    replayed = service.commit_legacy_memory_migration(
        legacy, origin_thread_id="legacy-thread"
    )
    assert replayed.title == "Obsidian migrated"
    legacy_job = next(job for job in queue.list() if job.operation_type == "legacy_copy")
    assert legacy_job.result is not None
    assert "descriptor" not in legacy_job.result
    assert {job.operation_type for job in queue.list()} >= {
        "memory_note",
        "memory_import",
        "legacy_copy",
    }


def test_legacy_root_research_compatibility_does_not_make_m_legacy_writable(
    tmp_path: Path,
) -> None:
    store, queue, service = _service(tmp_path)
    report, manifest = service.persist_research(
        _brief("M-unused"), _research_result(), _identity("legacy-thread")
    )

    assert report == store.read_text(manifest.report_path)
    assert manifest.report_path.startswith("reports/")
    assert queue.list() == ()
    with pytest.raises(ValueError, match="read-only"):
        service.persist_research(
            _brief("M-legacy"),
            _research_result(),
            _identity("forbidden-thread"),
            memory_id="M-legacy",
        )


def test_waiter_takes_over_after_another_writer_lease_expires(tmp_path: Path) -> None:
    store, queue, service = _service(tmp_path, lease_seconds=0.1)
    plan = build_create_memory_plan(
        memory_id="M-takeover",
        title="Takeover",
        created_at="2026-08-28T00:00:00+08:00",
    )
    queue.enqueue(**plan.enqueue_kwargs())
    other = queue.claim_writer(owner="other-process", lease_seconds=0.04)
    assert other is not None
    started = time.monotonic()

    descriptor = service.create_memory("Takeover", "M-takeover")

    assert descriptor == store.get_memory("M-takeover")
    assert time.monotonic() - started >= 0.02
    assert queue.get(plan.job_id).status == "succeeded"  # type: ignore[union-attr]


@pytest.mark.parametrize("operation", ("directory", "file"))
def test_heartbeat_renews_global_and_job_leases_during_slow_multistep_write(
    tmp_path: Path,
    operation: str,
) -> None:
    store, queue, service = _service(tmp_path, lease_seconds=0.06)
    if operation == "file":
        # Fixture setup is outside the behavior under test. Seed through the
        # low-level Store so only the slow research bundle uses the short lease.
        store.create_memory("Slow file", "M-slow-file")
    entered = threading.Event()
    release = threading.Event()
    failpoints: list[str] = []

    def slow_step(name: str) -> None:
        failpoints.append(name)
        if len(failpoints) == 1:
            entered.set()
            assert release.wait(timeout=2)
        time.sleep(0.012)

    service.writer.failpoint = slow_step
    results: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            if operation == "directory":
                results.append(service.create_memory("Slow directory", "M-slow-dir"))
            else:
                results.append(
                    service.persist_research(
                        _brief("M-slow-file"),
                        _research_result(),
                        _identity("slow-file-root"),
                        memory_id="M-slow-file",
                    )
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert entered.wait(timeout=2)
    time.sleep(0.14)

    assert queue.claim_writer(owner="forbidden-takeover", lease_seconds=0.1) is None
    running = queue.list(status="running")
    assert len(running) == 1
    assert running[0].lease_until is not None
    assert running[0].lease_until > time.time()

    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1
    assert len(failpoints) >= 3
    assert queue.list()[-1].status == "succeeded"


def test_heartbeat_accepts_job_renew_race_when_job_just_became_terminal() -> None:
    observed = threading.Event()

    class _TerminalRaceQueue:
        def renew_writer(self, *_args, **_kwargs):
            return object()

        def list(self, *, status):
            assert status == "running"
            return (SimpleNamespace(job_id="job-race"),)

        def renew_job(self, *_args, **_kwargs):
            observed.set()
            return None

        def get(self, job_id):
            assert job_id == "job-race"
            return SimpleNamespace(terminal=True)

        def assert_fence(self, _lease):
            return None

    heartbeat = _WriterHeartbeat(
        _TerminalRaceQueue(),  # type: ignore[arg-type]
        SimpleNamespace(generation=1),  # type: ignore[arg-type]
        0.03,
    )
    with heartbeat:
        assert observed.wait(timeout=1)
        time.sleep(0.02)
        heartbeat.verify()
    assert not heartbeat.lost.is_set()


def test_conflict_and_failed_jobs_map_to_public_errors(tmp_path: Path) -> None:
    _store, queue, service = _service(tmp_path)
    conflict_plan = build_create_memory_plan(
        memory_id="M-conflict",
        title="Conflict",
        created_at="2026-08-28T00:00:00+08:00",
    )
    conflict_job = queue.enqueue(**conflict_plan.enqueue_kwargs())
    lease = queue.claim_writer(owner="terminal-writer", lease_seconds=10)
    assert lease is not None
    queue.claim_next(lease, lease_seconds=10, job_id=conflict_job.job_id)
    queue.conflict(conflict_job.job_id, lease, "vault_conflict")
    queue.release_writer(lease)

    with pytest.raises(MemoryWriteConflictError, match="vault_conflict"):
        service.create_memory("Conflict", "M-conflict")

    failed_plan = build_create_memory_plan(
        memory_id="M-failed",
        title="Failed",
        created_at="2026-08-28T00:00:00+08:00",
    )
    failed_job = queue.enqueue(**failed_plan.enqueue_kwargs())
    lease = queue.claim_writer(owner="terminal-writer", lease_seconds=10)
    assert lease is not None
    queue.claim_next(lease, lease_seconds=10, job_id=failed_job.job_id)
    queue.fail(failed_job.job_id, lease, "invalid_command")
    queue.release_writer(lease)

    with pytest.raises(RuntimeError, match="invalid_command"):
        service.create_memory("Failed", "M-failed")


def test_drain_processes_fifo_jobs_and_startup_recover_is_safe_when_empty(
    tmp_path: Path,
) -> None:
    store, queue, service = _service(tmp_path)
    for index in range(2):
        plan = build_create_memory_plan(
            memory_id=f"M-drain-{index}",
            title=f"Drain {index}",
            created_at=f"2026-08-28T00:00:0{index}+08:00",
        )
        queue.enqueue(**plan.enqueue_kwargs(), created_at=float(index))

    completed = service.drain()

    assert [job.memory_id for job in completed] == ["M-drain-0", "M-drain-1"]
    assert store.get_memory("M-drain-0").title == "Drain 0"
    assert store.get_memory("M-drain-1").title == "Drain 1"
    assert service.startup_recover() == ()


def test_second_startup_waits_for_first_writer_to_finish_snapshot_job(
    tmp_path: Path,
) -> None:
    store, queue, first = _service(tmp_path, lease_seconds=0.2)
    second_queue = VaultWriteQueue(
        queue.db_path,
        vault_scope=queue.vault_scope,
        poll_interval_seconds=0.005,
    )
    second = VaultWriteService(
        MarkdownMemoryStore(store.root),
        second_queue,
        VaultWriter(store.root, second_queue, job_lease_seconds=0.2),
        lease_seconds=0.2,
        coordination_interval_seconds=0.005,
        wait_timeout_seconds=2,
        startup_timeout_seconds=2,
    )
    plan = build_create_memory_plan(
        memory_id="M-startup-wait",
        title="Startup wait",
        created_at="2026-08-28T00:00:00+08:00",
    )
    queue.enqueue(**plan.enqueue_kwargs())
    lease = queue.claim_writer(owner="first-startup", lease_seconds=0.2)
    assert lease is not None
    claimed = queue.claim_next(
        lease, lease_seconds=0.2, job_id=plan.job_id
    )
    assert claimed is not None
    outcome: list[tuple] = []

    worker = threading.Thread(
        target=lambda: outcome.append(second.startup_recover_and_drain()),
        daemon=True,
    )
    worker.start()
    time.sleep(0.03)
    assert worker.is_alive()

    first.writer.execute(claimed, lease)
    queue.release_writer(lease)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert outcome == [()]
    assert second_queue.get(plan.job_id).status == "succeeded"  # type: ignore[union-attr]


def test_startup_convergence_takes_over_an_expired_writer_lease(
    tmp_path: Path,
) -> None:
    store, queue, service = _service(tmp_path, lease_seconds=0.1)
    plan = build_create_memory_plan(
        memory_id="M-startup-takeover",
        title="Startup takeover",
        created_at="2026-08-28T00:00:00+08:00",
    )
    queue.enqueue(**plan.enqueue_kwargs())
    dead = queue.claim_writer(owner="dead-startup", lease_seconds=0.04)
    assert dead is not None

    completed = service.startup_recover_and_drain(timeout=1)

    assert any(job.job_id == plan.job_id for job in completed)
    assert store.get_memory("M-startup-takeover").title == "Startup takeover"
    assert queue.get(plan.job_id).status == "succeeded"  # type: ignore[union-attr]


def test_startup_snapshot_does_not_drain_a_job_enqueued_during_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue, service = _service(tmp_path, lease_seconds=0.2)
    initial = build_create_memory_plan(
        memory_id="M-startup-initial",
        title="Initial",
        created_at="2026-08-28T00:00:00+08:00",
    )
    later = build_create_memory_plan(
        memory_id="M-startup-later",
        title="Later",
        created_at="2026-08-28T00:00:01+08:00",
    )
    queue.enqueue(**initial.enqueue_kwargs())
    original_execute = service.writer.execute
    inserted = False

    def execute_and_enqueue(job, lease):
        nonlocal inserted
        if not inserted:
            inserted = True
            queue.enqueue(**later.enqueue_kwargs())
        return original_execute(job, lease)

    monkeypatch.setattr(service.writer, "execute", execute_and_enqueue)

    completed = service.startup_recover_and_drain(timeout=1)

    assert [job.job_id for job in completed] == [initial.job_id]
    assert store.get_memory("M-startup-initial").title == "Initial"
    with pytest.raises(FileNotFoundError):
        store.get_memory("M-startup-later")
    later_job = queue.get(later.job_id)
    assert later_job is not None
    assert later_job.status == "queued"
    assert later_job.command_blob is not None


def test_normal_wait_drives_only_its_finite_target_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue, service = _service(tmp_path)
    later = build_create_memory_plan(
        memory_id="M-request-later",
        title="Later",
        created_at="2026-08-28T00:00:01+08:00",
    )
    original_recover = service.writer.recover
    inserted = False

    def recover_and_enqueue(lease, *, job_ids=None):
        nonlocal inserted
        completed = original_recover(lease, job_ids=job_ids)
        if not inserted:
            inserted = True
            queue.enqueue(**later.enqueue_kwargs())
        return completed

    monkeypatch.setattr(service.writer, "recover", recover_and_enqueue)

    descriptor = service.create_memory("Target", "M-request-target")

    assert descriptor == store.get_memory("M-request-target")
    later_job = queue.get(later.job_id)
    assert later_job is not None and later_job.status == "queued"
    assert not (store.root / "Memories/M-request-later").exists()


def test_target_wait_takes_over_running_prefix_without_draining_later_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, queue, service = _service(tmp_path, lease_seconds=0.1)
    predecessor = build_create_memory_plan(
        memory_id="M-prefix-running",
        title="Running predecessor",
        created_at="2026-08-28T00:00:00+08:00",
    )
    target = build_create_memory_plan(
        memory_id="M-prefix-target",
        title="Target",
        created_at="2026-08-28T00:00:01+08:00",
    )
    later = build_create_memory_plan(
        memory_id="M-prefix-later",
        title="Later",
        created_at="2026-08-28T00:00:02+08:00",
    )
    queue.enqueue(**predecessor.enqueue_kwargs(), created_at=1)
    queue.enqueue(**target.enqueue_kwargs(), created_at=2)
    dead = queue.claim_writer(owner="dead-prefix-writer", lease_seconds=0.03)
    assert dead is not None
    claimed = queue.claim_next(
        dead,
        lease_seconds=0.03,
        job_id=predecessor.job_id,
    )
    assert claimed is not None and claimed.status == "running"
    time.sleep(0.05)

    original_execute = service.writer.execute
    executed: list[str] = []

    def execute_and_enqueue(job, lease):
        executed.append(job.job_id)
        if job.job_id == predecessor.job_id:
            queue.enqueue(**later.enqueue_kwargs(), created_at=3)
        return original_execute(job, lease)

    monkeypatch.setattr(service.writer, "execute", execute_and_enqueue)

    descriptor = service.create_memory("Target", "M-prefix-target")

    assert descriptor == store.get_memory("M-prefix-target")
    assert executed == [predecessor.job_id, target.job_id]
    assert queue.get(predecessor.job_id).status == "succeeded"  # type: ignore[union-attr]
    assert queue.get(target.job_id).status == "succeeded"  # type: ignore[union-attr]
    later_job = queue.get(later.job_id)
    assert later_job is not None and later_job.status == "queued"
    assert not (store.root / "Memories/M-prefix-later").exists()


def test_drain_checks_timeout_between_jobs_and_retains_remaining_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, queue, service = _service(tmp_path)
    plans = [
        build_create_memory_plan(
            memory_id=f"M-deadline-{index}",
            title=f"Deadline {index}",
            created_at=f"2026-08-28T00:00:0{index}+08:00",
        )
        for index in range(2)
    ]
    for index, plan in enumerate(plans):
        queue.enqueue(**plan.enqueue_kwargs(), created_at=float(index))
    original_recover = service.writer.recover

    def slow_recover(lease, *, job_ids=None):
        completed = original_recover(lease, job_ids=job_ids)
        time.sleep(0.15)
        return completed

    monkeypatch.setattr(service.writer, "recover", slow_recover)
    service.wait_timeout_seconds = 0.1

    with pytest.raises(TimeoutError, match="between jobs"):
        service.drain()

    assert queue.get(plans[0].job_id).status == "succeeded"  # type: ignore[union-attr]
    remaining = queue.get(plans[1].job_id)
    assert remaining is not None and remaining.status == "queued"
    assert remaining.command_blob is not None


def test_startup_bounded_recovery_cleans_safe_orphan_staging(tmp_path: Path) -> None:
    store, _queue, service = _service(tmp_path, lease_seconds=0.2)
    orphan = (
        store.root
        / "Memories"
        / ".paperpilot-writer"
        / "jobs"
        / "VaultJob-orphan"
    )
    staged = orphan / "stage" / "000000"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"unpublished staging bytes")

    assert service.startup_recover_and_drain(timeout=1) == ()

    assert not orphan.exists()


def test_startup_timeout_and_unknown_writer_errors_are_explicit_and_retain_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, queue, service = _service(tmp_path, lease_seconds=0.2)
    plan = build_create_memory_plan(
        memory_id="M-startup-timeout",
        title="Startup timeout",
        created_at="2026-08-28T00:00:00+08:00",
    )
    job = queue.enqueue(**plan.enqueue_kwargs())
    held = queue.claim_writer(owner="held", lease_seconds=10)
    assert held is not None
    with pytest.raises(TimeoutError, match="startup convergence"):
        service.startup_recover_and_drain(timeout=0.02)
    queue.release_writer(held)

    def explode(_lease):
        raise OSError("injected unknown writer failure")

    monkeypatch.setattr(service.writer, "recover", lambda _lease: ())
    monkeypatch.setattr(service.writer, "run_once", explode)
    with pytest.raises(RuntimeError, match="durable command was retained"):
        service.drain()
    retained = queue.get(job.job_id)
    assert retained is not None
    assert retained.status == "queued"
    assert retained.command_blob is not None

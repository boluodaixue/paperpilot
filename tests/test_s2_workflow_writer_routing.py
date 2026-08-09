"""Focused S2 checks that product workflow writes use the Vault Writer facade."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research import (
    ExecutionIdentity,
    MarkdownMemoryStore,
    MemoryWriteConflictError,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from src.research.memory_workflows import (
    build_legacy_migration_workflow,
    build_memory_import_workflow,
    build_memory_note_workflow,
    create_legacy_migration_workflow_state,
    create_memory_note_workflow_state,
    create_memory_text_import_workflow_state,
    resume_memory_workflow,
)
from src.research.vault_write_queue import VaultWriteQueue
from src.research.vault_write_service import VaultWriteService
from src.research.vault_writer import VaultWriter
from tests.test_n6_report_review import (
    FixedWebTool,
    ReviewPolicy,
    TrackingMemoryStore,
    _successful_blue,
)
from tests.test_s1_memory_workflows import (
    _ImportPolicy,
    _NotePolicy,
    _decision,
    _write_legacy,
    _write_source,
)
from tests.test_s1_research_persist_replay import _Policy, _Tool


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _unexpected_store_write(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("workflow bypassed the injected VaultWriteService")


class _CheckpointWindowCrash(BaseException):
    pass


class _RoutingService:
    """Test double that records routing while using captured Store commits."""

    def __init__(self, store: MarkdownMemoryStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str | None]] = []
        self._persist = MarkdownMemoryStore.persist_research.__get__(
            store, MarkdownMemoryStore
        )
        self._replace = MarkdownMemoryStore.replace_report.__get__(
            store, MarkdownMemoryStore
        )
        self._note = store.commit_memory_note
        self._import = store.commit_memory_import
        self._legacy = store.commit_legacy_memory_migration

    def persist_research(
        self,
        brief: object,
        result: object,
        identity: ExecutionIdentity,
        *,
        memory_id: str | None = None,
    ):
        self.calls.append(("research_bundle", identity.root_thread_id))
        report, manifest = self._persist(
            brief,
            result,
            identity,
            memory_id=memory_id,
        )
        if isinstance(self.store, TrackingMemoryStore):
            self.store.draft_report = report
            self.store.persisted_manifest = manifest
            self.store.persisted_result = result
            paths = (*manifest.evidence_paths, *manifest.source_paths)
            self.store.non_report_before = {
                path: self.store.read_text(path) for path in paths
            }
        return report, manifest

    def replace_report(
        self,
        report_path: str,
        markdown: str,
        *,
        memory_id: str | None = None,
        original_markdown: str | None = None,
        manifest: object | None = None,
        origin_thread_id: str | None = None,
    ) -> None:
        assert memory_id is not None
        assert original_markdown is not None
        assert manifest is not None
        self.calls.append(("report_review", origin_thread_id))
        self._replace(report_path, markdown)

    def commit_memory_note(
        self,
        proposal: object,
        *,
        origin_thread_id: str | None = None,
    ):
        self.calls.append(("memory_note", origin_thread_id))
        return self._note(proposal)

    def commit_memory_import(
        self,
        proposal: object,
        *,
        origin_thread_id: str | None = None,
    ):
        self.calls.append(("memory_import", origin_thread_id))
        return self._import(proposal)

    def commit_legacy_memory_migration(
        self,
        proposal: object,
        *,
        origin_thread_id: str | None = None,
    ):
        self.calls.append(("legacy_copy", origin_thread_id))
        return self._legacy(proposal)


@pytest.mark.asyncio
async def test_managed_research_and_review_route_through_writer_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TrackingMemoryStore(tmp_path)
    store.create_memory("Routing", "M-routing")
    service = _RoutingService(store)
    monkeypatch.setattr(store, "persist_research", _unexpected_store_write)
    monkeypatch.setattr(store, "replace_report", _unexpected_store_write)
    policy = ReviewPolicy(
        store,
        red_payload={"issues": [
            {
                "category": "factual",
                "target": "Attention replaced recurrence.",
                "description": "Use more precise wording without changing the source.",
            }
        ]},
        blue_payload=_successful_blue,
    )
    thread_id = "writer-managed-research"
    graph = build_research_workflow(
        policy,
        [FixedWebTool()],
        store,
        checkpointer=InMemorySaver(),
        report_review_enabled=True,
        vault_write_service=service,
    )
    identity = ExecutionIdentity(thread_id, None, thread_id, 0)

    await graph.ainvoke(
        create_research_workflow_state(
            "How do Transformers work?",
            identity,
            memory_id="M-routing",
        ),
        config=_config(thread_id),
    )
    final = await resume_research_workflow(
        graph,
        thread_id=thread_id,
        action="confirm",
    )

    assert final["workflow_status"] == "completed"
    assert service.calls == [
        ("research_bundle", thread_id),
        ("report_review", thread_id),
    ]
    outcome = final["workflow_result"]
    assert outcome.report_review.applied is True
    assert store.read_text(outcome.memory_manifest.report_path) == outcome.report_markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("external_edit", (False, True))
async def test_report_review_writer_checkpoint_window_replays_without_review_calls(
    tmp_path: Path,
    external_edit: bool,
) -> None:
    store = TrackingMemoryStore(tmp_path / "vault")
    store.create_memory("Checkpoint review", "M-checkpoint-review")
    queue = VaultWriteQueue(
        tmp_path / "runtime.db",
        vault_scope=f"checkpoint-review-{external_edit}",
        poll_interval_seconds=0.005,
    )
    service = VaultWriteService(
        store,
        queue,
        VaultWriter(store.root, queue, job_lease_seconds=1),
        lease_seconds=1,
        coordination_interval_seconds=0.01,
        wait_timeout_seconds=2,
        startup_timeout_seconds=2,
    )
    persisted = service.persist_research

    def persist_and_track(*args, **kwargs):
        report, manifest = persisted(*args, **kwargs)
        store.draft_report = report
        store.persisted_manifest = manifest
        store.persisted_result = args[1]
        store.non_report_before = {
            path: store.read_text(path)
            for path in (*manifest.evidence_paths, *manifest.source_paths)
        }
        return report, manifest

    service.persist_research = persist_and_track  # type: ignore[method-assign]
    replace = service.replace_report
    crash_once = True

    def replace_then_crash(*args, **kwargs):
        nonlocal crash_once
        replace(*args, **kwargs)
        if crash_once:
            crash_once = False
            raise _CheckpointWindowCrash

    service.replace_report = replace_then_crash  # type: ignore[method-assign]
    policy = ReviewPolicy(
        store,
        red_payload={
            "issues": [
                {
                    "category": "factual",
                    "target": "Attention replaced recurrence.",
                    "description": "Use more precise wording.",
                }
            ]
        },
        blue_payload=_successful_blue,
    )
    thread_id = f"review-checkpoint-window-{external_edit}"
    graph = build_research_workflow(
        policy,
        [FixedWebTool()],
        store,
        checkpointer=InMemorySaver(),
        report_review_enabled=True,
        vault_write_service=service,
    )
    await graph.ainvoke(
        create_research_workflow_state(
            "How do Transformers work?",
            ExecutionIdentity(thread_id, None, thread_id, 0),
            memory_id="M-checkpoint-review",
        ),
        config=_config(thread_id),
    )

    with pytest.raises(_CheckpointWindowCrash):
        await resume_research_workflow(
            graph,
            thread_id=thread_id,
            action="confirm",
        )

    checkpoint = await graph.aget_state(_config(thread_id))
    checkpoint_values = dict(checkpoint.values)
    reviewed = checkpoint_values["report_markdown"]
    manifest = checkpoint_values["memory_manifest"]
    assert checkpoint.next == ("postprocess_report",)
    assert policy.red_calls == policy.blue_calls == 1
    assert store.read_text(manifest.report_path) == reviewed
    if external_edit:
        latest = reviewed + "\nObsidian edit after Writer success.\n"
        (store.root / manifest.report_path).write_text(latest, encoding="utf-8")
    else:
        latest = reviewed

    final = await graph.ainvoke(None, config=_config(thread_id))

    assert policy.red_calls == policy.blue_calls == 1
    assert store.read_text(manifest.report_path) == latest
    assert final["report_markdown"] == latest
    assert final["workflow_result"].report_markdown == latest
    if external_edit:
        assert final["report_review"].applied is False
        assert "changed after the report review" in final["report_review"].fallback_reason
    else:
        assert final["report_review"].applied is True
        assert final["report_review"].fallback_reason is None
    review_jobs = [job for job in queue.list() if job.operation_type == "report_review"]
    assert len(review_jobs) == 1
    assert review_jobs[0].status == "succeeded"


@pytest.mark.asyncio
async def test_root_legacy_research_keeps_low_level_store_compatibility(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    service = _RoutingService(store)

    def reject_managed_route(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("root compatibility write entered the managed Writer route")

    service.persist_research = reject_managed_route  # type: ignore[method-assign]
    thread_id = "writer-root-compat"
    graph = build_research_workflow(
        _Policy(),
        [_Tool()],
        store,
        checkpointer=InMemorySaver(),
        vault_write_service=service,
    )
    identity = ExecutionIdentity(thread_id, None, thread_id, 0)
    await graph.ainvoke(
        create_research_workflow_state(
            "Can the stable finding be verified?",
            identity,
        ),
        config=_config(thread_id),
    )
    final = await resume_research_workflow(
        graph,
        thread_id=thread_id,
        action="confirm",
    )

    assert final["workflow_status"] == "completed"
    assert final["memory_id"] is None
    assert final["memory_manifest"].report_path.startswith("reports/")
    assert service.calls == []


@pytest.mark.asyncio
async def test_memory_commit_nodes_route_through_writer_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    store.create_memory("Imports", "M-imports")
    _write_source(tmp_path)
    _write_legacy(tmp_path)
    service = _RoutingService(store)
    monkeypatch.setattr(store, "commit_memory_note", _unexpected_store_write)
    monkeypatch.setattr(store, "commit_memory_import", _unexpected_store_write)
    monkeypatch.setattr(
        store,
        "commit_legacy_memory_migration",
        _unexpected_store_write,
    )

    note_thread = "writer-note"
    note_graph = build_memory_note_workflow(
        store,
        _NotePolicy(),
        checkpointer=InMemorySaver(),
        clock=lambda: 110,
        vault_write_service=service,
    )
    note_pause = await note_graph.ainvoke(
        create_memory_note_workflow_state(
            thread_id=note_thread,
            session_id="session-note",
            memory_id="M-notes",
            question="What claim is grounded?",
            created_at=100,
            expires_at=200,
        ),
        config=_config(note_thread),
    )
    note_proposal_pause = await resume_memory_workflow(
        note_graph,
        thread_id=note_thread,
        decision=_decision(
            "propose",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="answer_id",
            identity_value=note_pause["answer"].answer_id,
        ),
    )
    note_final = await resume_memory_workflow(
        note_graph,
        thread_id=note_thread,
        decision=_decision(
            "confirm",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="proposal_id",
            identity_value=note_proposal_pause["proposal"].proposal_id,
        ),
    )
    assert note_final["workflow_status"] == "committed"

    import_thread = "writer-import"
    import_graph = build_memory_import_workflow(
        store,
        _ImportPolicy(),
        checkpointer=InMemorySaver(),
        clock=lambda: 110,
        vault_write_service=service,
    )
    import_pause = await import_graph.ainvoke(
        create_memory_text_import_workflow_state(
            thread_id=import_thread,
            session_id="session-import",
            memory_id="M-imports",
            title="Inline",
            text="alpha\nbeta",
            created_at=100,
            expires_at=200,
        ),
        config=_config(import_thread),
    )
    import_final = await resume_memory_workflow(
        import_graph,
        thread_id=import_thread,
        decision=_decision(
            "confirm",
            session_id="session-import",
            memory_id="M-imports",
            identity_name="proposal_id",
            identity_value=import_pause["proposal"].proposal_id,
        ),
    )
    assert import_final["workflow_status"] == "committed"

    legacy_thread = "writer-legacy"
    legacy_graph = build_legacy_migration_workflow(
        store,
        checkpointer=InMemorySaver(),
        clock=lambda: 110,
        vault_write_service=service,
    )
    legacy_pause = await legacy_graph.ainvoke(
        create_legacy_migration_workflow_state(
            thread_id=legacy_thread,
            session_id="session-legacy",
            title="Migrated",
            target_memory_id="M-migrated",
            created_at=100,
            expires_at=200,
        ),
        config=_config(legacy_thread),
    )
    legacy_final = await resume_memory_workflow(
        legacy_graph,
        thread_id=legacy_thread,
        decision=_decision(
            "confirm",
            session_id="session-legacy",
            memory_id="M-legacy",
            identity_name="proposal_id",
            identity_value=legacy_pause["proposal"]["proposal_id"],
        ),
    )
    assert legacy_final["workflow_status"] == "committed"
    assert service.calls == [
        ("memory_note", note_thread),
        ("memory_import", import_thread),
        ("legacy_copy", legacy_thread),
    ]


@pytest.mark.asyncio
async def test_writer_conflict_keeps_memory_workflow_failure_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    _write_source(tmp_path)
    service = _RoutingService(store)

    def conflict(*_args: object, **_kwargs: object) -> None:
        raise MemoryWriteConflictError("writer compare-and-swap conflict")

    monkeypatch.setattr(service, "commit_memory_note", conflict)
    monkeypatch.setattr(store, "commit_memory_note", _unexpected_store_write)
    thread_id = "writer-note-conflict"
    graph = build_memory_note_workflow(
        store,
        _NotePolicy(),
        checkpointer=InMemorySaver(),
        clock=lambda: 110,
        vault_write_service=service,
    )
    answer_pause = await graph.ainvoke(
        create_memory_note_workflow_state(
            thread_id=thread_id,
            session_id="session-note",
            memory_id="M-notes",
            question="What claim is grounded?",
            created_at=100,
            expires_at=200,
        ),
        config=_config(thread_id),
    )
    proposal_pause = await resume_memory_workflow(
        graph,
        thread_id=thread_id,
        decision=_decision(
            "propose",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="answer_id",
            identity_value=answer_pause["answer"].answer_id,
        ),
    )
    final = await resume_memory_workflow(
        graph,
        thread_id=thread_id,
        decision=_decision(
            "confirm",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="proposal_id",
            identity_value=proposal_pause["proposal"].proposal_id,
        ),
    )

    assert final["workflow_status"] == "failed"
    assert final["result"]["status"] == "failed"
    assert final["result"]["error"] == "writer compare-and-swap conflict"
    assert not (tmp_path / proposal_pause["proposal"].target_path).exists()

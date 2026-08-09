"""Focused S1 tests for checkpointed Memory confirmation workflows."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from src.research.memory import MarkdownMemoryStore
from src.research.memory_workflows import (
    build_legacy_migration_workflow,
    build_memory_import_workflow,
    build_memory_note_workflow,
    continue_memory_workflow,
    create_legacy_migration_workflow_state,
    create_memory_note_workflow_state,
    create_memory_text_import_workflow_state,
    resume_memory_workflow,
)
from src.research.models import MemoryAnswer, MemoryImportProposal, MemoryNoteProposal


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _vault_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


def _write_source(root: Path, memory_id: str = "M-notes") -> str:
    relative = f"Memories/{memory_id}/notes/N-source.md"
    (root / relative).write_text(
        (
            "---\n"
            'id: "N-source"\n'
            'type: "note"\n'
            f'memory_id: "{memory_id}"\n'
            'title: "Grounded source"\n'
            'created_at: "2026-08-28T00:00:00+08:00"\n'
            'updated_at: "2026-08-28T00:00:00+08:00"\n'
            'origin: "user"\n'
            'status: "confirmed"\n'
            "tags:\n  - paperpilot\n"
            "---\n\n# Grounded source\n\nThe scoped memory claim is grounded.\n"
        ),
        encoding="utf-8",
    )
    return relative


class _NotePolicy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages, *, tools=None):
        del tools
        self.calls += 1
        system = str(messages[0]["content"])
        if "Answer only from" in system:
            context = json.loads(
                str(messages[-1]["content"]).split("MEMORY_CONTEXT_JSON:\n", 1)[1]
            )
            return {
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The scoped claim is grounded.",
                                "source_paths": [context["hits"][0]["path"]],
                            }
                        ],
                        "insufficient_evidence": [],
                    }
                )
            }
        contract = json.loads(
            str(messages[-1]["content"])
            .split("FIXED_NOTE_CONTRACT_JSON:\n", 1)[1]
            .split("\n\nMEMORY_ANSWER:", 1)[0]
        )
        fixed = contract["frontmatter"]
        sources = "\n".join(
            f"- [[{path[:-3]}]]" for path in contract["allowed_source_paths"]
        ) or "- None"
        markdown = (
            "---\n"
            f'id: {json.dumps(fixed["id"])}\n'
            f'type: {json.dumps(fixed["type"])}\n'
            f'memory_id: {json.dumps(fixed["memory_id"])}\n'
            'title: "Saved answer"\n'
            f'created_at: {json.dumps(fixed["created_at"])}\n'
            f'updated_at: {json.dumps(fixed["updated_at"])}\n'
            f'origin: {json.dumps(fixed["origin"])}\n'
            f'status: {json.dumps(fixed["status"])}\n'
            "tags:\n  - paperpilot\n"
            "---\n\n# Saved answer\n\nGrounded.\n\n"
            f"## Sources\n\n{sources}\n"
        )
        return {"content": json.dumps({"markdown": markdown})}


class _ImportPolicy:
    def __call__(self, messages, *, tools=None):
        del tools
        context = json.loads(
            str(messages[-1]["content"]).split("IMPORT_CONTEXT_JSON:\n", 1)[1]
        )
        locator = context["excerpts"][0]["locator"]
        return {
            "content": json.dumps(
                {
                    "title": "Imported source",
                    "summary": "Bounded source summary.",
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


def _decision(
    action: str,
    *,
    session_id: str,
    memory_id: str,
    identity_name: str,
    identity_value: str,
) -> dict[str, str]:
    return {
        "action": action,
        "session_id": session_id,
        "memory_id": memory_id,
        identity_name: identity_value,
    }


def _write_legacy(root: Path) -> None:
    values = {
        "sources/Source-fixed.md": "---\nid: Source-fixed\ntype: source\n---\n\n# Source\n",
        "evidence/Evidence-fixed.md": (
            "---\nid: Evidence-fixed\ntype: evidence\n---\n\n"
            "# Evidence\n\n[[sources/Source-fixed]]\n"
        ),
        "reports/Report-fixed.md": (
            "---\nid: Report-fixed\ntype: report\n---\n\n"
            "# Report\n\n[[evidence/Evidence-fixed]]\n"
        ),
    }
    for relative, markdown in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")


@pytest.mark.asyncio
async def test_note_two_interrupts_survive_graph_rebuild_and_commit(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    _write_source(tmp_path)
    policy = _NotePolicy()
    saver = InMemorySaver()
    thread_id = "note-two-stage"
    initial = create_memory_note_workflow_state(
        thread_id=thread_id,
        session_id="session-note",
        memory_id="M-notes",
        question="What claim is grounded?",
        created_at=100,
        expires_at=200,
    )
    graph = build_memory_note_workflow(store, policy, checkpointer=saver, clock=lambda: 110)

    answer_pause = await graph.ainvoke(initial, config=_config(thread_id))
    answer = answer_pause["answer"]
    assert isinstance(answer, MemoryAnswer)
    assert answer_pause["workflow_status"] == "waiting_answer_decision"
    assert policy.calls == 1

    graph = build_memory_note_workflow(store, policy, checkpointer=saver, clock=lambda: 120)
    proposal_pause = await resume_memory_workflow(
        graph,
        thread_id=thread_id,
        decision=_decision(
            "propose",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="answer_id",
            identity_value=answer.answer_id,
        ),
    )
    proposal = proposal_pause["proposal"]
    assert isinstance(proposal, MemoryNoteProposal)
    assert proposal_pause["workflow_status"] == "waiting_confirmation"
    assert policy.calls == 2
    assert not (tmp_path / proposal.target_path).exists()

    graph = build_memory_note_workflow(store, policy, checkpointer=saver, clock=lambda: 130)
    final = await resume_memory_workflow(
        graph,
        thread_id=thread_id,
        decision=_decision(
            "confirm",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="proposal_id",
            identity_value=proposal.proposal_id,
        ),
    )
    assert final["workflow_status"] == "committed", final.get("result", {}).get("error")
    assert (tmp_path / proposal.target_path).read_text(encoding="utf-8") == proposal.markdown
    assert policy.calls == 2
    with pytest.raises(ValueError, match="already terminal"):
        await resume_memory_workflow(
            graph,
            thread_id=thread_id,
            decision=_decision(
                "confirm",
                session_id="session-note",
                memory_id="M-notes",
                identity_name="proposal_id",
                identity_value=proposal.proposal_id,
            ),
        )


@pytest.mark.asyncio
async def test_note_cancel_and_expiry_are_zero_write(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    _write_source(tmp_path)
    baseline = _vault_files(tmp_path)

    saver = InMemorySaver()
    cancel_graph = build_memory_note_workflow(
        store, _NotePolicy(), checkpointer=saver, clock=lambda: 110
    )
    cancel_state = create_memory_note_workflow_state(
        thread_id="note-cancel",
        session_id="session-note",
        memory_id="M-notes",
        question="What claim is grounded?",
        created_at=100,
        expires_at=200,
    )
    paused = await cancel_graph.ainvoke(cancel_state, config=_config("note-cancel"))
    cancelled = await resume_memory_workflow(
        cancel_graph,
        thread_id="note-cancel",
        decision=_decision(
            "cancel",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="answer_id",
            identity_value=paused["answer"].answer_id,
        ),
    )
    assert cancelled["workflow_status"] == "cancelled"
    assert _vault_files(tmp_path) == baseline

    clock = [110.0]
    expire_graph = build_memory_note_workflow(
        store, _NotePolicy(), checkpointer=saver, clock=lambda: clock[0]
    )
    expire_state = create_memory_note_workflow_state(
        thread_id="note-expire",
        session_id="session-note",
        memory_id="M-notes",
        question="What claim is grounded?",
        created_at=100,
        expires_at=120,
    )
    answer_pause = await expire_graph.ainvoke(expire_state, config=_config("note-expire"))
    proposal_pause = await resume_memory_workflow(
        expire_graph,
        thread_id="note-expire",
        decision=_decision(
            "propose",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="answer_id",
            identity_value=answer_pause["answer"].answer_id,
        ),
    )
    clock[0] = 121
    expired = await resume_memory_workflow(
        expire_graph,
        thread_id="note-expire",
        decision=_decision(
            "confirm",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="proposal_id",
            identity_value=proposal_pause["proposal"].proposal_id,
        ),
    )
    assert expired["workflow_status"] == "expired"
    assert _vault_files(tmp_path) == baseline


@pytest.mark.asyncio
async def test_note_rejects_cross_session_memory_and_answer_identity(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    _write_source(tmp_path)
    graph = build_memory_note_workflow(
        store, _NotePolicy(), checkpointer=InMemorySaver(), clock=lambda: 110
    )
    state = create_memory_note_workflow_state(
        thread_id="note-identity",
        session_id="session-note",
        memory_id="M-notes",
        question="What claim is grounded?",
        created_at=100,
        expires_at=200,
    )
    paused = await graph.ainvoke(state, config=_config("note-identity"))
    answer_id = paused["answer"].answer_id

    for session_id, memory_id, candidate_answer in (
        ("other-session", "M-notes", answer_id),
        ("session-note", "M-other", answer_id),
        ("session-note", "M-notes", "Answer-forged"),
    ):
        with pytest.raises(ValueError, match="does not match"):
            await resume_memory_workflow(
                graph,
                thread_id="note-identity",
                decision=_decision(
                    "propose",
                    session_id=session_id,
                    memory_id=memory_id,
                    identity_name="answer_id",
                    identity_value=candidate_answer,
                ),
            )


@pytest.mark.asyncio
async def test_import_confirm_and_cancel_use_checkpointed_proposals(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Imports", "M-imports")
    saver = InMemorySaver()
    policy = _ImportPolicy()

    confirm_graph = build_memory_import_workflow(
        store, policy, checkpointer=saver, clock=lambda: 110
    )
    confirm_state = create_memory_text_import_workflow_state(
        thread_id="import-confirm",
        session_id="session-import",
        memory_id="M-imports",
        title="Inline",
        text="alpha\nbeta",
        created_at=100,
        expires_at=200,
    )
    paused = await confirm_graph.ainvoke(confirm_state, config=_config("import-confirm"))
    proposal = paused["proposal"]
    assert isinstance(proposal, MemoryImportProposal)
    assert not (tmp_path / proposal.import_path).exists()
    rebuilt = build_memory_import_workflow(
        store, policy, checkpointer=saver, clock=lambda: 120
    )
    final = await resume_memory_workflow(
        rebuilt,
        thread_id="import-confirm",
        decision=_decision(
            "confirm",
            session_id="session-import",
            memory_id="M-imports",
            identity_name="proposal_id",
            identity_value=proposal.proposal_id,
        ),
    )
    assert final["workflow_status"] == "committed"
    assert (tmp_path / proposal.import_path).is_file()

    before_cancel = _vault_files(tmp_path)
    cancel_graph = build_memory_import_workflow(
        store, policy, checkpointer=saver, clock=lambda: 130
    )
    cancel_state = create_memory_text_import_workflow_state(
        thread_id="import-cancel",
        session_id="session-import",
        memory_id="M-imports",
        title="Other",
        text="different content",
        created_at=100,
        expires_at=200,
    )
    cancel_pause = await cancel_graph.ainvoke(cancel_state, config=_config("import-cancel"))
    cancelled = await resume_memory_workflow(
        cancel_graph,
        thread_id="import-cancel",
        decision=_decision(
            "cancel",
            session_id="session-import",
            memory_id="M-imports",
            identity_name="proposal_id",
            identity_value=cancel_pause["proposal"].proposal_id,
        ),
    )
    assert cancelled["workflow_status"] == "cancelled"
    assert _vault_files(tmp_path) == before_cancel


@pytest.mark.asyncio
async def test_legacy_migration_confirm_and_cancel_keep_legacy(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    _write_legacy(tmp_path)
    legacy_before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for directory in ("reports", "evidence", "sources")
        for path in (tmp_path / directory).glob("*.md")
    }
    saver = InMemorySaver()
    graph = build_legacy_migration_workflow(store, checkpointer=saver, clock=lambda: 110)
    state = create_legacy_migration_workflow_state(
        thread_id="legacy-confirm",
        session_id="session-legacy",
        title="Migrated",
        target_memory_id="M-migrated",
        created_at=100,
        expires_at=200,
    )
    paused = await graph.ainvoke(state, config=_config("legacy-confirm"))
    proposal = paused["proposal"]
    rebuilt = build_legacy_migration_workflow(store, checkpointer=saver, clock=lambda: 120)
    final = await resume_memory_workflow(
        rebuilt,
        thread_id="legacy-confirm",
        decision=_decision(
            "confirm",
            session_id="session-legacy",
            memory_id="M-legacy",
            identity_name="proposal_id",
            identity_value=proposal["proposal_id"],
        ),
    )
    assert final["workflow_status"] == "committed"
    assert store.get_memory("M-migrated").title == "Migrated"
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for directory in ("reports", "evidence", "sources")
        for path in (tmp_path / directory).glob("*.md")
    } == legacy_before

    cancel_state = create_legacy_migration_workflow_state(
        thread_id="legacy-cancel",
        session_id="session-legacy",
        title="Not published",
        target_memory_id="M-not-published",
        created_at=100,
        expires_at=200,
    )
    cancel_pause = await graph.ainvoke(cancel_state, config=_config("legacy-cancel"))
    cancelled = await resume_memory_workflow(
        graph,
        thread_id="legacy-cancel",
        decision=_decision(
            "cancel",
            session_id="session-legacy",
            memory_id="M-legacy",
            identity_name="proposal_id",
            identity_value=cancel_pause["proposal"]["proposal_id"],
        ),
    )
    assert cancelled["workflow_status"] == "cancelled"
    with pytest.raises(FileNotFoundError):
        store.get_memory("M-not-published")


@pytest.mark.asyncio
async def test_note_exact_commit_is_adopted_after_node_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    _write_source(tmp_path)
    saver = InMemorySaver()
    graph = build_memory_note_workflow(
        store, _NotePolicy(), checkpointer=saver, clock=lambda: 110
    )
    state = create_memory_note_workflow_state(
        thread_id="note-replay",
        session_id="session-note",
        memory_id="M-notes",
        question="What claim is grounded?",
        created_at=100,
        expires_at=200,
    )
    answer_pause = await graph.ainvoke(state, config=_config("note-replay"))
    proposal_pause = await resume_memory_workflow(
        graph,
        thread_id="note-replay",
        decision=_decision(
            "propose",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="answer_id",
            identity_value=answer_pause["answer"].answer_id,
        ),
    )
    proposal = proposal_pause["proposal"]
    original_commit = store.commit_memory_note
    calls = 0

    def commit_then_crash(value: MemoryNoteProposal):
        nonlocal calls
        calls += 1
        original_commit(value)
        raise RuntimeError("simulated process termination after commit")

    monkeypatch.setattr(store, "commit_memory_note", commit_then_crash)
    with pytest.raises(RuntimeError, match="simulated process termination"):
        await resume_memory_workflow(
            graph,
            thread_id="note-replay",
            decision=_decision(
                "confirm",
                session_id="session-note",
                memory_id="M-notes",
                identity_name="proposal_id",
                identity_value=proposal.proposal_id,
            ),
        )
    assert calls == 1

    monkeypatch.setattr(store, "commit_memory_note", original_commit)
    rebuilt = build_memory_note_workflow(
        store, _NotePolicy(), checkpointer=saver, clock=lambda: 120
    )
    final = await continue_memory_workflow(rebuilt, thread_id="note-replay")
    assert final["workflow_status"] == "committed"
    assert final["result"]["target_path"] == proposal.target_path
    assert calls == 1


@pytest.mark.asyncio
async def test_note_half_commit_is_a_conflict_not_s2_repair(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Notes", "M-notes")
    _write_source(tmp_path)
    graph = build_memory_note_workflow(
        store, _NotePolicy(), checkpointer=InMemorySaver(), clock=lambda: 110
    )
    state = create_memory_note_workflow_state(
        thread_id="note-half",
        session_id="session-note",
        memory_id="M-notes",
        question="What claim is grounded?",
        created_at=100,
        expires_at=200,
    )
    answer_pause = await graph.ainvoke(state, config=_config("note-half"))
    proposal_pause = await resume_memory_workflow(
        graph,
        thread_id="note-half",
        decision=_decision(
            "propose",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="answer_id",
            identity_value=answer_pause["answer"].answer_id,
        ),
    )
    proposal = proposal_pause["proposal"]
    target = tmp_path / proposal.target_path
    target.write_text(proposal.markdown, encoding="utf-8")
    final = await resume_memory_workflow(
        graph,
        thread_id="note-half",
        decision=_decision(
            "confirm",
            session_id="session-note",
            memory_id="M-notes",
            identity_name="proposal_id",
            identity_value=proposal.proposal_id,
        ),
    )
    assert final["workflow_status"] == "failed"
    assert "without its exact Home update" in final["result"]["error"]
    assert target.is_file()


@pytest.mark.asyncio
async def test_note_interrupts_survive_sqlite_saver_close_and_reopen(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path / "vault")
    store.create_memory("Notes", "M-notes")
    _write_source(store.root)
    database = tmp_path / "checkpoints.db"
    policy = _NotePolicy()
    thread_id = "note-sqlite-restart"
    state = create_memory_note_workflow_state(
        thread_id=thread_id,
        session_id="session-note",
        memory_id="M-notes",
        question="What claim is grounded?",
        created_at=100,
        expires_at=200,
    )

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_memory_note_workflow(
            store, policy, checkpointer=saver, clock=lambda: 110
        )
        answer_pause = await graph.ainvoke(state, config=_config(thread_id))
        answer_id = answer_pause["answer"].answer_id

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_memory_note_workflow(
            store, policy, checkpointer=saver, clock=lambda: 120
        )
        proposal_pause = await resume_memory_workflow(
            graph,
            thread_id=thread_id,
            decision=_decision(
                "propose",
                session_id="session-note",
                memory_id="M-notes",
                identity_name="answer_id",
                identity_value=answer_id,
            ),
        )
        proposal = proposal_pause["proposal"]

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        graph = build_memory_note_workflow(
            store, policy, checkpointer=saver, clock=lambda: 130
        )
        final = await resume_memory_workflow(
            graph,
            thread_id=thread_id,
            decision=_decision(
                "confirm",
                session_id="session-note",
                memory_id="M-notes",
                identity_name="proposal_id",
                identity_value=proposal.proposal_id,
            ),
        )

    assert final["workflow_status"] == "committed"
    assert (store.root / proposal.target_path).is_file()
    assert policy.calls == 2

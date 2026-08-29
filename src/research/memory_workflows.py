"""Checkpointed confirmation workflows for notes, imports, and legacy migration.

These graphs deliberately keep proposal bodies and decisions in LangGraph state.
They still call the existing synchronous Markdown store commits; persistent Vault
write coordination and crash repair belong to S2.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .memory import MarkdownMemoryStore, MemoryWriteConflictError
from .memory_dialogue import answer_memory, propose_memory_note
from .memory_import import (
    prepare_memory_file_import,
    prepare_memory_text_import,
    prepare_memory_url_import,
)
from .models import (
    MemoryAnswer,
    MemoryImportDuplicate,
    MemoryImportProposal,
    MemoryNoteProposal,
)
from .vault import LEGACY_MEMORY_ID
from .vault_write_service import VaultWriteService


WorkflowStatus = Literal[
    "preparing",
    "waiting_answer_decision",
    "waiting_confirmation",
    "committed",
    "duplicate",
    "cancelled",
    "expired",
    "failed",
]


class MemoryNoteWorkflowState(TypedDict, total=False):
    workflow_type: Literal["memory_note"]
    thread_id: str
    session_id: str
    memory_id: str
    created_at: float
    expires_at: float
    workflow_status: WorkflowStatus
    question: str
    answer: MemoryAnswer
    proposal: MemoryNoteProposal
    decision: str | None
    result: dict[str, Any] | None


class MemoryImportWorkflowState(TypedDict, total=False):
    workflow_type: Literal["memory_import"]
    thread_id: str
    session_id: str
    memory_id: str
    created_at: float
    expires_at: float
    workflow_status: WorkflowStatus
    source: dict[str, Any]
    proposal: MemoryImportProposal
    duplicate: MemoryImportDuplicate
    decision: str | None
    result: dict[str, Any] | None


class LegacyMigrationWorkflowState(TypedDict, total=False):
    workflow_type: Literal["legacy_migration"]
    thread_id: str
    session_id: str
    memory_id: str
    created_at: float
    expires_at: float
    workflow_status: WorkflowStatus
    title: str
    target_memory_id: str
    proposal: dict[str, object]
    decision: str | None
    result: dict[str, Any] | None


def _identity(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a canonical non-empty string")
    return value


def _times(
    *,
    created_at: float | None,
    expires_at: float | None,
    ttl_seconds: float,
    clock: Callable[[], float],
) -> tuple[float, float]:
    created = float(clock() if created_at is None else created_at)
    expires = float(created + ttl_seconds if expires_at is None else expires_at)
    if ttl_seconds <= 0 or expires <= created:
        raise ValueError("workflow expiry must be later than creation")
    return created, expires


def _validate_state(
    state: Mapping[str, Any],
    config: RunnableConfig,
    *,
    workflow_type: str,
) -> None:
    if state.get("workflow_type") != workflow_type:
        raise ValueError(f"checkpoint does not belong to {workflow_type}")
    thread_id = _identity(str(state.get("thread_id") or ""), field_name="thread_id")
    _identity(str(state.get("session_id") or ""), field_name="session_id")
    _identity(str(state.get("memory_id") or ""), field_name="memory_id")
    if config.get("configurable", {}).get("thread_id") != thread_id:
        raise ValueError("configurable.thread_id must match workflow thread_id")
    created_at = state.get("created_at")
    expires_at = state.get("expires_at")
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        raise ValueError("workflow created_at must be numeric")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        raise ValueError("workflow expires_at must be numeric")
    if float(expires_at) <= float(created_at):
        raise ValueError("workflow expiry must be later than creation")


def _preview(value: Any) -> dict[str, Any]:
    if isinstance(value, MemoryImportProposal):
        payload = asdict(value)
        payload.pop("attachment_bytes", None)
        return payload
    if isinstance(value, (MemoryAnswer, MemoryNoteProposal, MemoryImportDuplicate)):
        return asdict(value)
    if isinstance(value, Mapping):
        payload = dict(value)
        files = payload.get("files")
        if isinstance(files, tuple):
            payload["files"] = [dict(item) for item in files]
        return payload
    raise TypeError("workflow preview value is unsupported")


def _decision(
    raw: Any,
    state: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    identity_key: str,
    identity_value: str,
    clock: Callable[[], float],
) -> str:
    if not isinstance(raw, Mapping):
        raise ValueError("workflow decision must be an object")
    expected = {"action", "session_id", "memory_id", identity_key}
    if set(raw) != expected:
        raise ValueError("workflow decision fields do not match the contract")
    if raw["session_id"] != state["session_id"]:
        raise ValueError("workflow decision session_id does not match")
    if raw["memory_id"] != state["memory_id"]:
        raise ValueError("workflow decision memory_id does not match")
    if raw[identity_key] != identity_value:
        raise ValueError(f"workflow decision {identity_key} does not match")
    action = str(raw["action"])
    if action not in allowed:
        raise ValueError("workflow decision action is invalid at this interrupt")
    expired = clock() >= float(state["expires_at"])
    if action == "expire" and not expired:
        raise ValueError("workflow cannot expire before expires_at")
    return "expire" if expired else action


def _terminal_without_write(state: Mapping[str, Any]) -> dict[str, Any]:
    decision = str(state.get("decision") or "")
    if decision not in {"cancel", "expire"}:
        raise ValueError("workflow cannot terminate without a cancel or expiry decision")
    status = "cancelled" if decision == "cancel" else "expired"
    return {
        "workflow_status": status,
        "result": {
            "status": status,
            "workflow_type": state["workflow_type"],
            "thread_id": state["thread_id"],
            "session_id": state["session_id"],
            "memory_id": state["memory_id"],
        },
    }


def _failure(state: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "workflow_status": "failed",
        "result": {
            "status": "failed",
            "workflow_type": state["workflow_type"],
            "thread_id": state["thread_id"],
            "session_id": state["session_id"],
            "memory_id": state["memory_id"],
            "error": str(exc),
        },
    }


def _exact_note_result(
    memory_store: MarkdownMemoryStore,
    proposal: MemoryNoteProposal,
) -> dict[str, Any] | None:
    try:
        target = memory_store.read_text(proposal.target_path)
    except FileNotFoundError:
        return None
    if target != proposal.markdown:
        raise MemoryWriteConflictError("Memory note target already contains different content")
    try:
        home = memory_store.read_text(proposal.home_path)
    except FileNotFoundError as exc:
        raise MemoryWriteConflictError("Memory Home.md is missing after note commit") from exc
    if home != proposal.home_markdown:
        raise MemoryWriteConflictError("Memory note exists without its exact Home update")
    return {
        "status": "committed",
        "memory_id": proposal.memory_id,
        "target_path": proposal.target_path,
        "home_path": proposal.home_path,
        "wikilink": proposal.wikilink,
    }


def _normalized_note_proposal(proposal: MemoryNoteProposal) -> MemoryNoteProposal:
    """Restore tuple fields normalized by checkpoint serialization."""
    return replace(proposal, source_paths=tuple(proposal.source_paths))


def _normalized_import_proposal(
    proposal: MemoryImportProposal,
) -> MemoryImportProposal:
    return replace(proposal, note_source_paths=tuple(proposal.note_source_paths))


def _normalized_legacy_proposal(
    proposal: Mapping[str, object],
) -> dict[str, object]:
    normalized = dict(proposal)
    files = normalized.get("files")
    if isinstance(files, list):
        normalized["files"] = tuple(dict(item) for item in files)
    return normalized


def _exact_import_result(
    memory_store: MarkdownMemoryStore,
    proposal: MemoryImportProposal,
) -> dict[str, Any] | None:
    duplicate = memory_store.find_memory_import(
        proposal.memory_id,
        proposal.source_ref,
        proposal.locator,
        proposal.content_hash,
    )
    if duplicate is None:
        return None
    if (
        duplicate.attachment_path != proposal.attachment_path
        or duplicate.import_path != proposal.import_path
        or duplicate.note_path != proposal.note_path
    ):
        return None
    return {
        "status": "committed",
        "memory_id": proposal.memory_id,
        "attachment_path": proposal.attachment_path,
        "import_path": proposal.import_path,
        "note_path": proposal.note_path,
        "home_path": proposal.home_path,
        "wikilinks": duplicate.wikilinks,
    }


def _exact_legacy_result(
    memory_store: MarkdownMemoryStore,
    proposal: Mapping[str, object],
) -> dict[str, Any] | None:
    target_memory_id = str(proposal["target_memory_id"])
    try:
        descriptor = memory_store.get_memory(target_memory_id)
    except FileNotFoundError:
        return None
    try:
        home_markdown = memory_store.read_text(str(proposal["home_path"]))
    except FileNotFoundError as exc:
        raise MemoryWriteConflictError(
            "legacy migration target exists without its Home.md"
        ) from exc
    if (
        descriptor.title != proposal["title"]
        or descriptor.created_at != proposal["created_at"]
        or home_markdown != proposal["home_markdown"]
    ):
        raise MemoryWriteConflictError("legacy migration target already differs")
    files = proposal.get("files")
    if not isinstance(files, tuple):
        raise ValueError("legacy migration proposal files are invalid")
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("legacy migration proposal file entry is invalid")
        try:
            target_markdown = memory_store.read_text(str(item["target_path"]))
        except FileNotFoundError as exc:
            raise MemoryWriteConflictError(
                "legacy migration target is only partially committed"
            ) from exc
        if target_markdown != item["markdown"]:
            raise MemoryWriteConflictError("legacy migration target already differs")
    return {
        "status": "committed",
        "source_memory_id": LEGACY_MEMORY_ID,
        "memory_id": descriptor.memory_id,
        "descriptor": asdict(descriptor),
    }


def create_memory_note_workflow_state(
    *,
    thread_id: str,
    session_id: str,
    memory_id: str,
    question: str,
    created_at: float | None = None,
    expires_at: float | None = None,
    ttl_seconds: float = 24 * 60 * 60,
    clock: Callable[[], float] = time.time,
) -> MemoryNoteWorkflowState:
    created, expires = _times(
        created_at=created_at,
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
        clock=clock,
    )
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    return MemoryNoteWorkflowState(
        workflow_type="memory_note",
        thread_id=_identity(thread_id, field_name="thread_id"),
        session_id=_identity(session_id, field_name="session_id"),
        memory_id=_identity(memory_id, field_name="memory_id"),
        created_at=created,
        expires_at=expires,
        workflow_status="preparing",
        question=question.strip(),
        decision=None,
        result=None,
    )


def build_memory_note_workflow(
    memory_store: MarkdownMemoryStore,
    policy: Any,
    *,
    checkpointer: BaseCheckpointSaver,
    clock: Callable[[], float] = time.time,
    vault_write_service: VaultWriteService | None = None,
) -> Any:
    async def build_answer(
        state: MemoryNoteWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_note")
        answer = await answer_memory(
            memory_store,
            policy,
            state["memory_id"],
            state["question"],
        )
        return {"answer": answer, "workflow_status": "waiting_answer_decision"}

    def review_answer(
        state: MemoryNoteWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_note")
        answer = state.get("answer")
        if not isinstance(answer, MemoryAnswer):
            raise TypeError("memory note workflow has no answer")
        raw = interrupt({"kind": "memory_answer_decision", "answer": _preview(answer)})
        return {
            "decision": _decision(
                raw,
                state,
                allowed=frozenset({"propose", "cancel", "expire"}),
                identity_key="answer_id",
                identity_value=answer.answer_id,
                clock=clock,
            )
        }

    async def prepare_note(
        state: MemoryNoteWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_note")
        answer = state.get("answer")
        if not isinstance(answer, MemoryAnswer):
            raise TypeError("memory note workflow has no answer")
        proposal = await propose_memory_note(memory_store, policy, answer)
        return {
            "proposal": proposal,
            "decision": None,
            "workflow_status": "waiting_confirmation",
        }

    def review_note(
        state: MemoryNoteWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_note")
        proposal = state.get("proposal")
        if not isinstance(proposal, MemoryNoteProposal):
            raise TypeError("memory note workflow has no proposal")
        raw = interrupt({"kind": "memory_note_confirmation", "proposal": _preview(proposal)})
        return {
            "decision": _decision(
                raw,
                state,
                allowed=frozenset({"confirm", "cancel", "expire"}),
                identity_key="proposal_id",
                identity_value=proposal.proposal_id,
                clock=clock,
            )
        }

    def commit_note(
        state: MemoryNoteWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_note")
        proposal = state.get("proposal")
        if state.get("decision") != "confirm" or not isinstance(
            proposal, MemoryNoteProposal
        ):
            raise ValueError("memory note commit requires a confirmed proposal")
        proposal = _normalized_note_proposal(proposal)
        try:
            result = _exact_note_result(memory_store, proposal)
            if result is None:
                committed = (
                    vault_write_service.commit_memory_note(
                        proposal,
                        origin_thread_id=state["thread_id"],
                    )
                    if vault_write_service is not None
                    else memory_store.commit_memory_note(proposal)
                )
                result = {"status": "committed", **committed}
        except (MemoryWriteConflictError, FileExistsError, ValueError) as exc:
            return _failure(state, exc)
        return {"workflow_status": "committed", "result": result}

    def route_answer(state: MemoryNoteWorkflowState) -> str:
        return "prepare_note" if state.get("decision") == "propose" else "finish"

    def route_note(state: MemoryNoteWorkflowState) -> str:
        return "commit_note" if state.get("decision") == "confirm" else "finish"

    builder = StateGraph(MemoryNoteWorkflowState)
    builder.add_node("answer", build_answer)
    builder.add_node("review_answer", review_answer)
    builder.add_node("prepare_note", prepare_note)
    builder.add_node("review_note", review_note)
    builder.add_node("commit_note", commit_note)
    builder.add_node("finish", _terminal_without_write)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", "review_answer")
    builder.add_conditional_edges(
        "review_answer", route_answer, {"prepare_note": "prepare_note", "finish": "finish"}
    )
    builder.add_edge("prepare_note", "review_note")
    builder.add_conditional_edges(
        "review_note", route_note, {"commit_note": "commit_note", "finish": "finish"}
    )
    builder.add_edge("commit_note", END)
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


def _create_import_state(
    *,
    thread_id: str,
    session_id: str,
    memory_id: str,
    source: dict[str, Any],
    created_at: float | None,
    expires_at: float | None,
    ttl_seconds: float,
    clock: Callable[[], float],
) -> MemoryImportWorkflowState:
    created, expires = _times(
        created_at=created_at,
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
        clock=clock,
    )
    return MemoryImportWorkflowState(
        workflow_type="memory_import",
        thread_id=_identity(thread_id, field_name="thread_id"),
        session_id=_identity(session_id, field_name="session_id"),
        memory_id=_identity(memory_id, field_name="memory_id"),
        created_at=created,
        expires_at=expires,
        workflow_status="preparing",
        source=source,
        decision=None,
        result=None,
    )


def create_memory_file_import_workflow_state(
    *, thread_id: str, session_id: str, memory_id: str, file_name: str, content: bytes,
    created_at: float | None = None, expires_at: float | None = None,
    ttl_seconds: float = 24 * 60 * 60, clock: Callable[[], float] = time.time,
) -> MemoryImportWorkflowState:
    return _create_import_state(
        thread_id=thread_id, session_id=session_id, memory_id=memory_id,
        source={"kind": "file", "file_name": file_name, "content": content},
        created_at=created_at, expires_at=expires_at, ttl_seconds=ttl_seconds, clock=clock,
    )


def create_memory_text_import_workflow_state(
    *, thread_id: str, session_id: str, memory_id: str, title: str, text: str,
    created_at: float | None = None, expires_at: float | None = None,
    ttl_seconds: float = 24 * 60 * 60, clock: Callable[[], float] = time.time,
) -> MemoryImportWorkflowState:
    return _create_import_state(
        thread_id=thread_id, session_id=session_id, memory_id=memory_id,
        source={"kind": "text", "title": title, "text": text},
        created_at=created_at, expires_at=expires_at, ttl_seconds=ttl_seconds, clock=clock,
    )


def create_memory_url_import_workflow_state(
    *, thread_id: str, session_id: str, memory_id: str, url: str,
    created_at: float | None = None, expires_at: float | None = None,
    ttl_seconds: float = 24 * 60 * 60, clock: Callable[[], float] = time.time,
) -> MemoryImportWorkflowState:
    return _create_import_state(
        thread_id=thread_id, session_id=session_id, memory_id=memory_id,
        source={"kind": "url", "url": url}, created_at=created_at,
        expires_at=expires_at, ttl_seconds=ttl_seconds, clock=clock,
    )


def build_memory_import_workflow(
    memory_store: MarkdownMemoryStore,
    policy: Any,
    *,
    checkpointer: BaseCheckpointSaver,
    clock: Callable[[], float] = time.time,
    url_fetcher: Callable[[str], Any] | None = None,
    vault_write_service: VaultWriteService | None = None,
) -> Any:
    async def prepare_import(
        state: MemoryImportWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_import")
        source = state.get("source")
        if not isinstance(source, dict):
            raise TypeError("memory import workflow has no source")
        kind = source.get("kind")
        if kind == "file" and set(source) == {"kind", "file_name", "content"}:
            value = await prepare_memory_file_import(
                memory_store, policy, state["memory_id"], source["file_name"], source["content"]
            )
        elif kind == "text" and set(source) == {"kind", "title", "text"}:
            value = await prepare_memory_text_import(
                memory_store, policy, state["memory_id"], source["title"], source["text"]
            )
        elif kind == "url" and set(source) == {"kind", "url"}:
            value = await prepare_memory_url_import(
                memory_store,
                policy,
                state["memory_id"],
                source["url"],
                _fetcher=url_fetcher,
            )
        else:
            raise ValueError("memory import source does not match its kind")
        if isinstance(value, MemoryImportDuplicate):
            return {
                "duplicate": value,
                "workflow_status": "duplicate",
                "result": {"status": "duplicate", **_preview(value)},
            }
        if not isinstance(value, MemoryImportProposal):
            raise TypeError("memory import preparation returned an invalid result")
        return {"proposal": value, "workflow_status": "waiting_confirmation"}

    def review_import(
        state: MemoryImportWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_import")
        proposal = state.get("proposal")
        if not isinstance(proposal, MemoryImportProposal):
            raise TypeError("memory import workflow has no proposal")
        raw = interrupt({"kind": "memory_import_confirmation", "proposal": _preview(proposal)})
        return {
            "decision": _decision(
                raw,
                state,
                allowed=frozenset({"confirm", "cancel", "expire"}),
                identity_key="proposal_id",
                identity_value=proposal.proposal_id,
                clock=clock,
            )
        }

    def commit_import(
        state: MemoryImportWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="memory_import")
        proposal = state.get("proposal")
        if state.get("decision") != "confirm" or not isinstance(
            proposal, MemoryImportProposal
        ):
            raise ValueError("memory import commit requires a confirmed proposal")
        proposal = _normalized_import_proposal(proposal)
        try:
            result = _exact_import_result(memory_store, proposal)
            if result is None:
                result = dict(
                    vault_write_service.commit_memory_import(
                        proposal,
                        origin_thread_id=state["thread_id"],
                    )
                    if vault_write_service is not None
                    else memory_store.commit_memory_import(proposal)
                )
        except (MemoryWriteConflictError, FileExistsError, ValueError) as exc:
            return _failure(state, exc)
        return {"workflow_status": "committed", "result": result}

    def route_prepared(state: MemoryImportWorkflowState) -> str:
        return "end_duplicate" if state.get("workflow_status") == "duplicate" else "review"

    def route_review(state: MemoryImportWorkflowState) -> str:
        return "commit" if state.get("decision") == "confirm" else "finish"

    builder = StateGraph(MemoryImportWorkflowState)
    builder.add_node("prepare", prepare_import)
    builder.add_node("review", review_import)
    builder.add_node("commit", commit_import)
    builder.add_node("finish", _terminal_without_write)
    builder.add_node("end_duplicate", lambda _state: {})
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges(
        "prepare", route_prepared, {"end_duplicate": "end_duplicate", "review": "review"}
    )
    builder.add_edge("end_duplicate", END)
    builder.add_conditional_edges(
        "review", route_review, {"commit": "commit", "finish": "finish"}
    )
    builder.add_edge("commit", END)
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


def create_legacy_migration_workflow_state(
    *,
    thread_id: str,
    session_id: str,
    title: str,
    target_memory_id: str,
    created_at: float | None = None,
    expires_at: float | None = None,
    ttl_seconds: float = 24 * 60 * 60,
    clock: Callable[[], float] = time.time,
) -> LegacyMigrationWorkflowState:
    created, expires = _times(
        created_at=created_at,
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
        clock=clock,
    )
    return LegacyMigrationWorkflowState(
        workflow_type="legacy_migration",
        thread_id=_identity(thread_id, field_name="thread_id"),
        session_id=_identity(session_id, field_name="session_id"),
        memory_id=LEGACY_MEMORY_ID,
        created_at=created,
        expires_at=expires,
        workflow_status="preparing",
        title=_identity(title, field_name="title"),
        target_memory_id=_identity(target_memory_id, field_name="target_memory_id"),
        decision=None,
        result=None,
    )


def build_legacy_migration_workflow(
    memory_store: MarkdownMemoryStore,
    *,
    checkpointer: BaseCheckpointSaver,
    clock: Callable[[], float] = time.time,
    vault_write_service: VaultWriteService | None = None,
) -> Any:
    def prepare_migration(
        state: LegacyMigrationWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="legacy_migration")
        proposal = (
            vault_write_service.prepare_legacy_memory_migration(
                state["title"], state["target_memory_id"]
            )
            if vault_write_service is not None
            and hasattr(vault_write_service, "prepare_legacy_memory_migration")
            else memory_store.prepare_legacy_memory_migration(
                state["title"], state["target_memory_id"]
            )
        )
        return {"proposal": proposal, "workflow_status": "waiting_confirmation"}

    def review_migration(
        state: LegacyMigrationWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="legacy_migration")
        proposal = state.get("proposal")
        if not isinstance(proposal, dict):
            raise TypeError("legacy migration workflow has no proposal")
        raw = interrupt({"kind": "legacy_migration_confirmation", "proposal": _preview(proposal)})
        return {
            "decision": _decision(
                raw,
                state,
                allowed=frozenset({"confirm", "cancel", "expire"}),
                identity_key="proposal_id",
                identity_value=str(proposal["proposal_id"]),
                clock=clock,
            )
        }

    def commit_migration(
        state: LegacyMigrationWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_state(state, config, workflow_type="legacy_migration")
        proposal = state.get("proposal")
        if state.get("decision") != "confirm" or not isinstance(proposal, dict):
            raise ValueError("legacy migration commit requires a confirmed proposal")
        proposal = _normalized_legacy_proposal(proposal)
        try:
            result = (
                None
                if proposal.get("retirement") is not None
                else _exact_legacy_result(memory_store, proposal)
            )
            if result is None:
                descriptor = (
                    vault_write_service.commit_legacy_memory_migration(
                        proposal,
                        origin_thread_id=state["thread_id"],
                    )
                    if vault_write_service is not None
                    else memory_store.commit_legacy_memory_migration(proposal)
                )
                result = {
                    "status": "committed",
                    "source_memory_id": LEGACY_MEMORY_ID,
                    "memory_id": descriptor.memory_id,
                    "descriptor": asdict(descriptor),
                    "retired": proposal.get("retirement") is not None,
                }
        except (MemoryWriteConflictError, FileExistsError, ValueError) as exc:
            return _failure(state, exc)
        return {"workflow_status": "committed", "result": result}

    def route_review(state: LegacyMigrationWorkflowState) -> str:
        return "commit" if state.get("decision") == "confirm" else "finish"

    builder = StateGraph(LegacyMigrationWorkflowState)
    builder.add_node("prepare", prepare_migration)
    builder.add_node("review", review_migration)
    builder.add_node("commit", commit_migration)
    builder.add_node("finish", _terminal_without_write)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "review")
    builder.add_conditional_edges(
        "review", route_review, {"commit": "commit", "finish": "finish"}
    )
    builder.add_edge("commit", END)
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


async def resume_memory_workflow(
    graph: Any,
    *,
    thread_id: str,
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Resume exactly one interrupt and reject every terminal/repeated decision."""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        raise FileNotFoundError(f"workflow checkpoint does not exist: {thread_id}")
    status = snapshot.values.get("workflow_status")
    if status in {"committed", "duplicate", "cancelled", "expired", "failed"}:
        raise ValueError(f"workflow is already terminal: {status}")
    if not getattr(snapshot, "interrupts", ()):
        raise ValueError("workflow is not waiting for a decision")
    return await graph.ainvoke(Command(resume=dict(decision)), config=config)


async def continue_memory_workflow(graph: Any, *, thread_id: str) -> dict[str, Any]:
    """Continue a non-interrupted pending node after a process-level failure."""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        raise FileNotFoundError(f"workflow checkpoint does not exist: {thread_id}")
    if getattr(snapshot, "interrupts", ()):
        raise ValueError("workflow is waiting for a decision, not automatic recovery")
    if not snapshot.next:
        raise ValueError("workflow has no pending node to recover")
    return await graph.ainvoke(None, config=config)


__all__ = [
    "LegacyMigrationWorkflowState",
    "MemoryImportWorkflowState",
    "MemoryNoteWorkflowState",
    "build_legacy_migration_workflow",
    "build_memory_import_workflow",
    "build_memory_note_workflow",
    "continue_memory_workflow",
    "create_legacy_migration_workflow_state",
    "create_memory_file_import_workflow_state",
    "create_memory_note_workflow_state",
    "create_memory_text_import_workflow_state",
    "create_memory_url_import_workflow_state",
    "resume_memory_workflow",
]

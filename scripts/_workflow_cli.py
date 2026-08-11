"""Shared command-line interaction for the PaperPilot research workflow."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.research.models import (
    MemoryAnswer,
    MemoryImportDuplicate,
    MemoryImportProposal,
    MemoryNoteProposal,
    ResearchBrief,
    ResearchWorkflowResult,
)
from src.research.obsidian import build_obsidian_open_uri
from src.research.runtime import ResearchRuntime
from src.research.runtime_registry import RuntimeRegistry, WorkflowRecord
from src.research.workflow_recovery import (
    derive_workflow_status,
    workflow_outbox_events,
)


class UserCancelled(Exception):
    """Raised when a user declines to run a reviewed research brief."""


class _CliLeaseGuard:
    """Keep one CLI executor's existing Registry lease alive."""

    def __init__(
        self,
        registry: RuntimeRegistry,
        record: WorkflowRecord,
        token: str,
        lease_seconds: float,
    ) -> None:
        self.registry = registry
        self.record = record
        self.token = token
        self.lease_seconds = lease_seconds

    async def verify(self) -> None:
        if not self.registry.renew_lease(
            self.record.task_id,
            self.token,
            lease_seconds=self.lease_seconds,
        ):
            raise RuntimeError(
                f"workflow lease lost: {self.record.thread_id}"
            )


async def _cli_input(
    input_fn: Callable[[str], str],
    prompt: str,
    *,
    lease: _CliLeaseGuard | None,
) -> str:
    """Wait for terminal input without starving the lease heartbeat."""
    value = await asyncio.to_thread(input_fn, prompt)
    if lease is not None:
        await lease.verify()
    return value


async def _lease_call(
    lease: _CliLeaseGuard | None,
    operation: Callable[[], Any],
) -> Any:
    """Fence one checkpoint mutation on both sides of the await."""
    if lease is not None:
        await lease.verify()
    result = await operation()
    if lease is not None:
        await lease.verify()
    return result


@asynccontextmanager
async def cli_workflow_lease(
    runtime: ResearchRuntime,
    registry: RuntimeRegistry,
    record: WorkflowRecord,
):
    """Claim, renew, and release one Registry lease around CLI execution."""
    lease_seconds = float(getattr(runtime, "lease_seconds", 60))
    token = registry.claim_lease(
        record.task_id,
        lease_seconds=lease_seconds,
    )
    if token is None:
        raise RuntimeError(
            f"workflow is already being executed: {record.thread_id}"
        )
    guard = _CliLeaseGuard(registry, record, token, lease_seconds)
    await guard.verify()
    owner = asyncio.current_task()
    lost = asyncio.Event()

    async def heartbeat() -> None:
        interval = max(0.05, min(5.0, lease_seconds / 3.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await guard.verify()
            except Exception:
                lost.set()
                if owner is not None:
                    owner.cancel()
                return

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        yield guard
    except asyncio.CancelledError:
        if lost.is_set():
            raise RuntimeError(
                f"workflow lease lost: {record.thread_id}"
            ) from None
        raise
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        registry.release_lease(record.task_id, token)


def register_cli_workflow(
    registry: RuntimeRegistry,
    *,
    workflow_id: str,
    session_id: str,
    memory_id: str,
    workflow_type: str,
    runtime: ResearchRuntime,
) -> WorkflowRecord:
    """Create or validate the minimal locator for one CLI-owned workflow."""
    existing = registry.get(workflow_id)
    if existing is not None:
        expected = (workflow_id, session_id, memory_id, workflow_type)
        actual = (
            existing.thread_id,
            existing.session_id,
            existing.memory_id,
            existing.workflow_type,
        )
        if actual != expected:
            raise ValueError("CLI workflow locator identity does not match")
        return existing
    created_at = time.time()
    expires_at = created_at + float(getattr(runtime, "proposal_ttl_seconds", 86400))
    return registry.register(
        task_id=workflow_id,
        thread_id=workflow_id,
        session_id=session_id,
        memory_id=memory_id,
        workflow_type=workflow_type,
        created_at=created_at,
        expires_at=expires_at,
    )


async def _checkpoint_values(
    runtime: ResearchRuntime,
    workflow_type: str,
    workflow_id: str,
) -> dict[str, Any]:
    snapshot = await runtime.get_workflow_snapshot(workflow_type, workflow_id)
    return dict(snapshot.values)


def _expire_memory_decision(
    record: WorkflowRecord,
    snapshot: Any,
) -> dict[str, Any]:
    interrupts = getattr(snapshot, "interrupts", ())
    if len(interrupts) != 1 or not isinstance(interrupts[0].value, Mapping):
        raise ValueError("waiting workflow has no recoverable interrupt")
    payload = interrupts[0].value
    decision: dict[str, Any] = {
        "action": "expire",
        "session_id": record.session_id,
        "memory_id": record.memory_id,
    }
    kind = payload.get("kind")
    if kind == "memory_answer_decision":
        decision["answer_id"] = payload["answer"]["answer_id"]
    elif kind in {
        "memory_note_confirmation",
        "memory_import_confirmation",
        "legacy_migration_confirmation",
    }:
        decision["proposal_id"] = payload["proposal"]["proposal_id"]
    else:
        raise ValueError("waiting workflow interrupt cannot expire")
    return decision


async def reconcile_cli_workflow(
    runtime: ResearchRuntime,
    registry: RuntimeRegistry,
    record: WorkflowRecord,
    *,
    lease: _CliLeaseGuard | None = None,
    expire_waiting: bool = True,
) -> tuple[Any, str]:
    """Expire and durably project one checkpoint while holding its lease."""
    if lease is None:
        async with cli_workflow_lease(runtime, registry, record) as owned:
            return await reconcile_cli_workflow(
                runtime,
                registry,
                record,
                lease=owned,
                expire_waiting=expire_waiting,
            )

    await lease.verify()
    snapshot = await runtime.get_workflow_snapshot(
        record.workflow_type, record.thread_id
    )
    status = derive_workflow_status(record, snapshot)
    if (
        expire_waiting
        and status == "waiting_confirmation"
        and record.expires_at is not None
        and time.time() >= record.expires_at
    ):
        if record.workflow_type == "research":
            await _lease_call(
                lease,
                lambda: runtime.review(
                    record.thread_id,
                    "expire",
                    session_id=record.session_id,
                    memory_id=record.memory_id,
                ),
            )
        else:
            decision = _expire_memory_decision(record, snapshot)
            await _lease_call(
                lease,
                lambda: runtime.resume_memory_operation(
                    record.workflow_type,
                    record.thread_id,
                    decision,
                ),
            )
        snapshot = await runtime.get_workflow_snapshot(
            record.workflow_type, record.thread_id
        )
        status = derive_workflow_status(record, snapshot)

    for event_type, payload in workflow_outbox_events(record, snapshot, status):
        registry.append_event(record.thread_id, event_type, payload)
    return snapshot, status


def require_memory(
    runtime: ResearchRuntime,
    memory_id: str | None,
    *,
    writable: bool = True,
) -> str:
    """Validate one explicit Memory selection through the shared Runtime facade."""
    value = (memory_id or "").strip()
    if not value:
        raise ValueError("Please select a Memory first.")
    if hasattr(runtime, "get_memory_option"):
        option = runtime.get_memory_option(value)
        if writable and option.get("read_only"):
            raise ValueError(
                "M-legacy is read-only; migrate it or select a managed Memory first."
            )
    elif hasattr(runtime, "get_memory"):
        runtime.get_memory(value)
    return value


def format_memory_answer(answer: MemoryAnswer) -> str:
    lines = [f"\nMemory answer ({answer.memory_id})", answer.markdown]
    if answer.citations:
        lines.append("Citations:")
        lines.extend(f"  - {item.wikilink}" for item in answer.citations)
    if answer.insufficient_evidence:
        lines.append("Insufficient evidence:")
        lines.extend(f"  - {item}" for item in answer.insufficient_evidence)
    return "\n".join(lines)


async def _run_memory_question_unleased(
    runtime: ResearchRuntime,
    memory_id: str,
    question: str,
    *,
    session_id: str = "cli-memory",
    workflow_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    lease: _CliLeaseGuard | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> MemoryAnswer:
    """Answer from one Memory and optionally preview/confirm a new note."""
    selected = require_memory(runtime, memory_id, writable=False)
    checkpointed = hasattr(runtime, "start_memory_note_workflow")
    if checkpointed:
        workflow_id = workflow_id or runtime.new_workflow_id("memory_note")
        record = (
            register_cli_workflow(
                registry,
                workflow_id=workflow_id,
                session_id=session_id,
                memory_id=selected,
                workflow_type="memory_note",
                runtime=runtime,
            )
            if registry is not None
            else None
        )
        state = await _checkpoint_values(runtime, "memory_note", workflow_id)
        if not state:
            state = await _lease_call(
                lease,
                lambda: runtime.start_memory_note_workflow(
                    session_id=session_id,
                    memory_id=selected,
                    question=question,
                    thread_id=workflow_id,
                    expires_at=record.expires_at if record is not None else None,
                ),
            )
        elif (
            state.get("session_id") != session_id
            or state.get("memory_id") != selected
            or state.get("question") != question.strip()
        ):
            raise ValueError("Memory note checkpoint identity does not match")
        output_fn(f"Workflow ID: {workflow_id}")
        answer = state.get("answer")
        if not isinstance(answer, MemoryAnswer):
            raise RuntimeError("Memory answer workflow returned an invalid answer")
    else:
        answer = await runtime.answer_memory(selected, question)
    output_fn(format_memory_answer(answer))
    if hasattr(runtime, "get_memory_option"):
        option = runtime.get_memory_option(selected)
        if option.get("read_only"):
            if checkpointed and workflow_id is not None:
                cancelled = await _lease_call(
                    lease,
                    lambda: runtime.resume_memory_operation(
                        "memory_note",
                        workflow_id,
                        {
                            "action": "cancel",
                            "session_id": session_id,
                            "memory_id": selected,
                            "answer_id": answer.answer_id,
                        },
                    ),
                )
                if cancelled.get("workflow_status") != "cancelled":
                    raise RuntimeError("Memory note workflow did not cancel")
                if registry is not None:
                    registry.append_event(
                        workflow_id, "cancelled", {"reason": "user_cancelled"}
                    )
            output_fn("This Memory is read-only; migrate it before saving notes.")
            return answer
    proposal = state.get("proposal") if checkpointed else None
    if not isinstance(proposal, MemoryNoteProposal):
        action = (
            await _cli_input(
                input_fn,
                "Save this answer as a note? [y/N]: ",
                lease=lease,
            )
        ).strip().lower()
        if action not in {"y", "yes"}:
            if checkpointed and workflow_id is not None:
                cancelled = await _lease_call(
                    lease,
                    lambda: runtime.resume_memory_operation(
                        "memory_note",
                        workflow_id,
                        {
                            "action": "cancel",
                            "session_id": session_id,
                            "memory_id": selected,
                            "answer_id": answer.answer_id,
                        },
                    ),
                )
                if cancelled.get("workflow_status") != "cancelled":
                    raise RuntimeError("Memory note workflow did not cancel")
                if registry is not None:
                    registry.append_event(
                        workflow_id, "cancelled", {"reason": "user_cancelled"}
                    )
            return answer

        if checkpointed and workflow_id is not None:
            proposal_state = await _lease_call(
                lease,
                lambda: runtime.resume_memory_operation(
                    "memory_note",
                    workflow_id,
                    {
                        "action": "propose",
                        "session_id": session_id,
                        "memory_id": selected,
                        "answer_id": answer.answer_id,
                    },
                ),
            )
            proposal = proposal_state.get("proposal")
        else:
            proposal = await runtime.propose_memory_note(answer)
    else:
        output_fn("Resuming checkpointed note proposal.")
    if not isinstance(proposal, MemoryNoteProposal):
        raise RuntimeError("Memory note proposal is invalid")
    output_fn(
        "\nMemory note preview\n"
        f"Target: {proposal.target_path}\n\n{proposal.markdown}"
    )
    confirm = (
        await _cli_input(
            input_fn,
            "Confirm this exact note write? [y/N]: ",
            lease=lease,
        )
    ).strip().lower()
    if confirm in {"y", "yes"}:
        if checkpointed and workflow_id is not None:
            final = await _lease_call(
                lease,
                lambda: runtime.resume_memory_operation(
                    "memory_note",
                    workflow_id,
                    {
                        "action": "confirm",
                        "session_id": session_id,
                        "memory_id": selected,
                        "proposal_id": proposal.proposal_id,
                    },
                ),
            )
            result = final.get("result")
            if not isinstance(result, Mapping):
                raise RuntimeError("Memory note workflow returned an invalid result")
            if registry is not None:
                registry.append_event(workflow_id, "confirmed")
                registry.append_event(workflow_id, "completed")
        else:
            result = runtime.commit_memory_note(proposal)
        output_fn(f"Saved: {result['wikilink']} ({result['target_path']})")
    else:
        if checkpointed and workflow_id is not None:
            cancelled = await _lease_call(
                lease,
                lambda: runtime.resume_memory_operation(
                    "memory_note",
                    workflow_id,
                    {
                        "action": "cancel",
                        "session_id": session_id,
                        "memory_id": selected,
                        "proposal_id": proposal.proposal_id,
                    },
                ),
            )
            if cancelled.get("workflow_status") != "cancelled":
                raise RuntimeError("Memory note workflow did not cancel")
            if registry is not None:
                registry.append_event(
                    workflow_id, "cancelled", {"reason": "user_cancelled"}
                )
        output_fn("Note proposal cancelled; Memory was not changed.")
    return answer


async def run_memory_question(
    runtime: ResearchRuntime,
    memory_id: str,
    question: str,
    *,
    session_id: str = "cli-memory",
    workflow_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> MemoryAnswer:
    """Run one note workflow under its Registry execution lease."""
    if registry is None or not hasattr(runtime, "start_memory_note_workflow"):
        return await _run_memory_question_unleased(
            runtime,
            memory_id,
            question,
            session_id=session_id,
            workflow_id=workflow_id,
            registry=registry,
            input_fn=input_fn,
            output_fn=output_fn,
        )
    selected = require_memory(runtime, memory_id, writable=False)
    workflow_id = workflow_id or runtime.new_workflow_id("memory_note")
    record = register_cli_workflow(
        registry,
        workflow_id=workflow_id,
        session_id=session_id,
        memory_id=selected,
        workflow_type="memory_note",
        runtime=runtime,
    )
    async with cli_workflow_lease(runtime, registry, record) as lease:
        snapshot, status = await reconcile_cli_workflow(
            runtime, registry, record, lease=lease
        )
        if status in {"completed", "cancelled", "expired", "failed"}:
            raise RuntimeError(f"memory note workflow is already {status}")
        try:
            return await _run_memory_question_unleased(
                runtime,
                selected,
                question,
                session_id=session_id,
                workflow_id=workflow_id,
                registry=registry,
                lease=lease,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        finally:
            await reconcile_cli_workflow(
                runtime,
                registry,
                record,
                lease=lease,
                expire_waiting=False,
            )


def confirm_memory_import(
    runtime: ResearchRuntime,
    value: MemoryImportProposal | MemoryImportDuplicate,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> Any:
    """Display one complete import preview and commit only after confirmation."""
    if isinstance(value, MemoryImportDuplicate):
        output_fn(f"Already imported: {value.import_path}")
        return value
    output_fn(
        "\nMemory import preview\n"
        f"Attachment: {value.attachment_path}\n"
        f"Import: {value.import_path}\n"
        f"Note: {value.note_path}\n\n"
        f"{value.import_markdown}\n\n{value.note_markdown}"
    )
    confirm = input_fn("Confirm this exact import write? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        output_fn("Import proposal cancelled; Memory was not changed.")
        return value
    result = runtime.commit_memory_import(value)
    output_fn(f"Imported: {result['import_path']}")
    return result


def confirm_legacy_memory_migration(
    runtime: ResearchRuntime,
    proposal: Mapping[str, object],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> Any:
    """Display the complete legacy conversion and publish only after confirmation."""
    files = proposal.get("files")
    if not isinstance(files, tuple):
        raise ValueError("legacy migration proposal files are invalid")
    sections = [
        "\nLegacy Memory migration preview",
        f"Source: {proposal.get('source_memory_id')} (archived outside the active Vault after confirmation)",
        f"Target: {proposal.get('target_memory_id')}",
        f"Home: {proposal.get('home_path')}",
        f"Retirement: {proposal.get('retirement')}",
        "",
        str(proposal.get("home_markdown") or ""),
    ]
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("legacy migration proposal file entry is invalid")
        sections.extend(
            (
                "",
                f"{item.get('source_path')} -> {item.get('target_path')}",
                str(item.get("markdown") or ""),
            )
        )
    output_fn("\n".join(sections))
    confirm = input_fn("Publish and retire this exact legacy snapshot? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        output_fn("Migration proposal cancelled; the Vault was not changed.")
        return proposal
    descriptor = runtime.commit_legacy_memory_migration(proposal)
    output_fn(
        f"Migrated to {descriptor.memory_id}. The legacy root was archived and current-version sessions were rebound."
    )
    return descriptor


async def _run_memory_import_workflow_unleased(
    runtime: ResearchRuntime,
    memory_id: str,
    source: Mapping[str, Any],
    *,
    session_id: str,
    workflow_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    lease: _CliLeaseGuard | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> Any:
    """Run one checkpointed import confirmation in the product CLI."""
    workflow_id = workflow_id or runtime.new_workflow_id("memory_import")
    record = (
        register_cli_workflow(
            registry,
            workflow_id=workflow_id,
            session_id=session_id,
            memory_id=memory_id,
            workflow_type="memory_import",
            runtime=runtime,
        )
        if registry is not None
        else None
    )
    state = await _checkpoint_values(runtime, "memory_import", workflow_id)
    if not state:
        state = await _lease_call(
            lease,
            lambda: runtime.start_memory_import_workflow(
                session_id=session_id,
                memory_id=memory_id,
                source=source,
                thread_id=workflow_id,
                expires_at=record.expires_at if record is not None else None,
            ),
        )
    elif (
        state.get("session_id") != session_id
        or state.get("memory_id") != memory_id
        or state.get("source") != dict(source)
    ):
        raise ValueError("Memory import checkpoint identity does not match")
    output_fn(f"Workflow ID: {workflow_id}")
    duplicate = state.get("duplicate")
    if isinstance(duplicate, MemoryImportDuplicate):
        output_fn(f"Already imported: {duplicate.import_path}")
        if registry is not None:
            registry.append_event(workflow_id, "completed")
        return duplicate
    proposal = state.get("proposal")
    if not isinstance(proposal, MemoryImportProposal):
        raise RuntimeError("Memory import workflow returned an invalid proposal")
    output_fn(
        "\nMemory import preview\n"
        f"Attachment: {proposal.attachment_path}\n"
        f"Import: {proposal.import_path}\n"
        f"Note: {proposal.note_path}\n\n"
        f"{proposal.import_markdown}\n\n{proposal.note_markdown}"
    )
    action = (
        await _cli_input(
            input_fn,
            "Confirm this exact import write? [y/N]: ",
            lease=lease,
        )
    ).strip().lower()
    decision = "confirm" if action in {"y", "yes"} else "cancel"
    final = await _lease_call(
        lease,
        lambda: runtime.resume_memory_operation(
            "memory_import",
            workflow_id,
            {
                "action": decision,
                "session_id": session_id,
                "memory_id": memory_id,
                "proposal_id": proposal.proposal_id,
            },
        ),
    )
    if decision == "cancel":
        if final.get("workflow_status") != "cancelled":
            raise RuntimeError("Memory import workflow did not cancel")
        if registry is not None:
            registry.append_event(
                workflow_id, "cancelled", {"reason": "user_cancelled"}
            )
        output_fn("Import proposal cancelled; Memory was not changed.")
        return proposal
    result = final.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("Memory import workflow returned an invalid result")
    if registry is not None:
        registry.append_event(workflow_id, "confirmed")
        registry.append_event(workflow_id, "completed")
    output_fn(f"Imported: {result['import_path']}")
    return result


async def run_memory_import_workflow(
    runtime: ResearchRuntime,
    memory_id: str,
    source: Mapping[str, Any],
    *,
    session_id: str,
    workflow_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> Any:
    """Run one import workflow under its Registry execution lease."""
    if registry is None:
        return await _run_memory_import_workflow_unleased(
            runtime,
            memory_id,
            source,
            session_id=session_id,
            workflow_id=workflow_id,
            registry=registry,
            input_fn=input_fn,
            output_fn=output_fn,
        )
    workflow_id = workflow_id or runtime.new_workflow_id("memory_import")
    record = register_cli_workflow(
        registry,
        workflow_id=workflow_id,
        session_id=session_id,
        memory_id=memory_id,
        workflow_type="memory_import",
        runtime=runtime,
    )
    async with cli_workflow_lease(runtime, registry, record) as lease:
        _snapshot, status = await reconcile_cli_workflow(
            runtime, registry, record, lease=lease
        )
        if status in {"completed", "cancelled", "expired", "failed"}:
            raise RuntimeError(f"memory import workflow is already {status}")
        try:
            return await _run_memory_import_workflow_unleased(
                runtime,
                memory_id,
                source,
                session_id=session_id,
                workflow_id=workflow_id,
                registry=registry,
                lease=lease,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        finally:
            await reconcile_cli_workflow(
                runtime,
                registry,
                record,
                lease=lease,
                expire_waiting=False,
            )


async def _run_legacy_migration_workflow_unleased(
    runtime: ResearchRuntime,
    target_memory_id: str,
    title: str,
    *,
    session_id: str,
    workflow_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    lease: _CliLeaseGuard | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> Any:
    """Run one checkpointed legacy migration confirmation in the product CLI."""
    workflow_id = workflow_id or runtime.new_workflow_id("legacy_migration")
    record = (
        register_cli_workflow(
            registry,
            workflow_id=workflow_id,
            session_id=session_id,
            memory_id="M-legacy",
            workflow_type="legacy_migration",
            runtime=runtime,
        )
        if registry is not None
        else None
    )
    state = await _checkpoint_values(runtime, "legacy_migration", workflow_id)
    if not state:
        state = await _lease_call(
            lease,
            lambda: runtime.start_legacy_migration_workflow(
                session_id=session_id,
                title=title,
                target_memory_id=target_memory_id,
                thread_id=workflow_id,
                expires_at=record.expires_at if record is not None else None,
            ),
        )
    elif (
        state.get("session_id") != session_id
        or state.get("target_memory_id") != target_memory_id
        or state.get("title") != title
    ):
        raise ValueError("legacy migration checkpoint identity does not match")
    output_fn(f"Workflow ID: {workflow_id}")
    proposal = state.get("proposal")
    if not isinstance(proposal, Mapping):
        raise RuntimeError("Legacy migration workflow returned an invalid proposal")
    files = proposal.get("files")
    if not isinstance(files, tuple):
        raise ValueError("legacy migration proposal files are invalid")
    sections = [
        "\nLegacy Memory migration preview",
        f"Source: {proposal.get('source_memory_id')} (archived outside the active Vault after confirmation)",
        f"Target: {proposal.get('target_memory_id')}",
        f"Home: {proposal.get('home_path')}",
        f"Retirement: {proposal.get('retirement')}",
        "",
        str(proposal.get("home_markdown") or ""),
    ]
    for item in files:
        sections.extend(
            (
                "",
                f"{item.get('source_path')} -> {item.get('target_path')}",
                str(item.get("markdown") or ""),
            )
        )
    output_fn("\n".join(sections))
    action = (
        await _cli_input(
            input_fn,
            "Publish and retire this exact legacy snapshot? [y/N]: ",
            lease=lease,
        )
    ).strip().lower()
    decision = "confirm" if action in {"y", "yes"} else "cancel"
    final = await _lease_call(
        lease,
        lambda: runtime.resume_memory_operation(
            "legacy_migration",
            workflow_id,
            {
                "action": decision,
                "session_id": session_id,
                "memory_id": "M-legacy",
                "proposal_id": str(proposal["proposal_id"]),
            },
        ),
    )
    if decision == "cancel":
        if final.get("workflow_status") != "cancelled":
            raise RuntimeError("legacy migration workflow did not cancel")
        if registry is not None:
            registry.append_event(
                workflow_id, "cancelled", {"reason": "user_cancelled"}
            )
        output_fn("Migration proposal cancelled; the Vault was not changed.")
        return proposal
    result = final.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("Legacy migration workflow returned an invalid result")
    if registry is not None:
        registry.append_event(workflow_id, "confirmed")
        registry.append_event(workflow_id, "completed")
    output_fn(
        f"Migrated to {result['memory_id']}. The legacy root was archived and current-version sessions were rebound."
    )
    return result


async def run_legacy_migration_workflow(
    runtime: ResearchRuntime,
    target_memory_id: str,
    title: str,
    *,
    session_id: str,
    workflow_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> Any:
    """Run one legacy migration under its Registry execution lease."""
    if registry is None:
        return await _run_legacy_migration_workflow_unleased(
            runtime,
            target_memory_id,
            title,
            session_id=session_id,
            workflow_id=workflow_id,
            registry=registry,
            input_fn=input_fn,
            output_fn=output_fn,
        )
    workflow_id = workflow_id or runtime.new_workflow_id("legacy_migration")
    record = register_cli_workflow(
        registry,
        workflow_id=workflow_id,
        session_id=session_id,
        memory_id="M-legacy",
        workflow_type="legacy_migration",
        runtime=runtime,
    )
    async with cli_workflow_lease(runtime, registry, record) as lease:
        _snapshot, status = await reconcile_cli_workflow(
            runtime, registry, record, lease=lease
        )
        if status in {"completed", "cancelled", "expired", "failed"}:
            raise RuntimeError(f"legacy migration workflow is already {status}")
        try:
            return await _run_legacy_migration_workflow_unleased(
                runtime,
                target_memory_id,
                title,
                session_id=session_id,
                workflow_id=workflow_id,
                registry=registry,
                lease=lease,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        finally:
            await reconcile_cli_workflow(
                runtime,
                registry,
                record,
                lease=lease,
                expire_waiting=False,
            )


def _brief_from_state(state: dict[str, Any]) -> ResearchBrief:
    brief = state.get("brief")
    if isinstance(brief, ResearchBrief):
        return brief
    if isinstance(brief, dict):
        return ResearchBrief(**brief)

    interrupts = state.get("__interrupt__") or ()
    for interrupt_value in interrupts:
        value = getattr(interrupt_value, "value", interrupt_value)
        if isinstance(value, dict) and isinstance(value.get("brief"), dict):
            return ResearchBrief(**value["brief"])
    raise RuntimeError("research workflow did not return a reviewable brief")


def format_brief(brief: ResearchBrief) -> str:
    """Render the structured brief without changing its workflow contract."""
    values = asdict(brief)
    lines = [
        "\nResearch Brief",
        f"  Question: {values['question']}",
    ]
    if values.get("memory_id") is not None:
        lines.extend(
            (
                f"  Target Memory: {values['memory_id']}",
                "  Matched Memory files:",
                *(
                    f"    - {item}"
                    for item in values.get("memory_paths", ()) or ("(none)",)
                ),
                "  Known information:",
                *(
                    f"    - {item}"
                    for item in values.get("known_information", ()) or ("(none)",)
                ),
                "  New research gaps:",
                *(
                    f"    - {item}"
                    for item in values.get("research_gaps", ()) or ("(none)",)
                ),
            )
        )
    lines.extend(
        [
            f"  Objective: {values['objective']}",
            "  Scope:",
            *(f"    - {item}" for item in values["scope"]),
            "  Directions:",
            *(f"    - {item}" for item in values["directions"]),
            "  Constraints:",
            *(f"    - {item}" for item in values["constraints"]),
            f"  Expected output: {values['expected_output']}",
            f"  Revision: {values['revision']}",
        ]
    )
    return "\n".join(lines)


async def _run_reviewed_workflow_unleased(
    runtime: ResearchRuntime,
    question: str,
    *,
    thread_id: str,
    memory_id: str | None = None,
    session_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    lease: _CliLeaseGuard | None = None,
    auto_confirm: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> ResearchWorkflowResult:
    """Start one root workflow and keep it paused until the brief is accepted."""
    locator: WorkflowRecord | None = None
    if registry is not None:
        if memory_id is None or session_id is None:
            raise ValueError("registered CLI research requires session_id and memory_id")
        locator = register_cli_workflow(
            registry,
            workflow_id=thread_id,
            session_id=session_id,
            memory_id=memory_id,
            workflow_type="research",
            runtime=runtime,
        )
        output_fn(f"Workflow ID: {thread_id}")
    get_snapshot = getattr(runtime, "get_snapshot", None)
    snapshot = await get_snapshot(thread_id) if callable(get_snapshot) else None
    state = dict(snapshot.values) if snapshot is not None else {}
    next_nodes = tuple(getattr(snapshot, "next", ()))
    if state:
        saved_question = str(state.get("question") or "").strip()
        if saved_question and saved_question != question.strip():
            raise ValueError("thread_id already belongs to a different research question")
        if state.get("memory_id") != memory_id:
            raise ValueError("thread_id already belongs to a different Memory")
        existing_result = state.get("workflow_result")
        if isinstance(existing_result, ResearchWorkflowResult):
            return existing_result
        status = str(state.get("workflow_status") or "")
        if status in {"cancelled", "expired", "failed"}:
            raise RuntimeError(f"research workflow is already {status}")
        if "review_brief" not in next_nodes:
            continue_research = getattr(runtime, "continue_research", None)
            if not callable(continue_research):
                raise RuntimeError("runtime cannot continue the existing research workflow")
            state = await _lease_call(
                lease,
                lambda: continue_research(thread_id),
            )
            result = state.get("workflow_result")
            if isinstance(result, ResearchWorkflowResult):
                return result
            status = str(state.get("workflow_status") or "")
            if status in {"cancelled", "expired", "failed"}:
                raise RuntimeError(f"research workflow is already {status}")
    elif memory_id is None:
        state = await _lease_call(
            lease,
            lambda: runtime.start(question, thread_id=thread_id),
        )
    elif locator is not None:
        state = await _lease_call(
            lease,
            lambda: runtime.start(
                question,
                thread_id=thread_id,
                memory_id=memory_id,
                session_id=session_id,
                expires_at=locator.expires_at,
            ),
        )
    else:
        state = await _lease_call(
            lease,
            lambda: runtime.start(
                question,
                thread_id=thread_id,
                memory_id=memory_id,
            ),
        )
    while True:
        output_fn(format_brief(_brief_from_state(state)))
        if auto_confirm:
            action = "confirm"
        else:
            action = (
                await _cli_input(
                    input_fn,
                    "Confirm, modify, or cancel? [c/m/q]: ",
                    lease=lease,
                )
            ).strip().lower()

        if action in {"c", "confirm", "yes", "y"}:
            state = await _lease_call(
                lease,
                lambda: runtime.review(thread_id, "confirm"),
            )
            result = state.get("workflow_result")
            if not isinstance(result, ResearchWorkflowResult):
                raise RuntimeError("research workflow ended without a structured result")
            if registry is not None:
                registry.append_event(thread_id, "confirmed")
                registry.append_event(thread_id, "completed")
            return result
        if action in {"m", "modify"}:
            feedback = (
                await _cli_input(input_fn, "Brief changes: ", lease=lease)
            ).strip()
            if not feedback:
                output_fn("Modification feedback cannot be empty.")
                continue
            state = await _lease_call(
                lease,
                lambda: runtime.review(thread_id, "modify", feedback),
            )
            continue
        if action in {"q", "quit", "cancel", "n", "no"}:
            state = await _lease_call(
                lease,
                lambda: runtime.review(thread_id, "cancel"),
            )
            if state.get("workflow_status") != "cancelled":
                raise RuntimeError("research workflow did not reach cancelled state")
            if registry is not None:
                registry.append_event(
                    thread_id, "cancelled", {"reason": "user_cancelled"}
                )
            raise UserCancelled("research cancelled before confirmation")
        output_fn("Please enter c, m, or q.")


async def run_reviewed_workflow(
    runtime: ResearchRuntime,
    question: str,
    *,
    thread_id: str,
    memory_id: str | None = None,
    session_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    auto_confirm: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> ResearchWorkflowResult:
    """Run one research workflow under its Registry execution lease."""
    if registry is None:
        return await _run_reviewed_workflow_unleased(
            runtime,
            question,
            thread_id=thread_id,
            memory_id=memory_id,
            session_id=session_id,
            registry=registry,
            auto_confirm=auto_confirm,
            input_fn=input_fn,
            output_fn=output_fn,
        )
    if memory_id is None or session_id is None:
        raise ValueError("registered CLI research requires session_id and memory_id")
    record = register_cli_workflow(
        registry,
        workflow_id=thread_id,
        session_id=session_id,
        memory_id=memory_id,
        workflow_type="research",
        runtime=runtime,
    )
    async with cli_workflow_lease(runtime, registry, record) as lease:
        _snapshot, status = await reconcile_cli_workflow(
            runtime, registry, record, lease=lease
        )
        if status in {"cancelled", "expired", "failed"}:
            raise RuntimeError(f"research workflow is already {status}")
        try:
            return await _run_reviewed_workflow_unleased(
                runtime,
                question,
                thread_id=thread_id,
                memory_id=memory_id,
                session_id=session_id,
                registry=registry,
                lease=lease,
                auto_confirm=auto_confirm,
                input_fn=input_fn,
                output_fn=output_fn,
            )
        finally:
            await reconcile_cli_workflow(
                runtime,
                registry,
                record,
                lease=lease,
                expire_waiting=False,
            )


def report_path(runtime: ResearchRuntime, result: ResearchWorkflowResult) -> Path:
    """Resolve the persisted report manifest entry to its Markdown file."""
    return (runtime.memory_store.root / result.memory_manifest.report_path).resolve()


def vault_name_from_config(config: Mapping[str, Any]) -> str | None:
    """Read the optional explicit Obsidian Vault name without deriving one."""
    research = config.get("research", {})
    if not isinstance(research, Mapping):
        raise ValueError("research configuration must be a mapping")
    value = research.get("vault_name")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("research.vault_name must be a non-empty string")
    return value.strip()


def format_result_locations(
    runtime: ResearchRuntime,
    result: ResearchWorkflowResult,
    *,
    vault_name: str | None = None,
) -> str:
    """Format durable output locations for one completed workflow."""
    vault_root = Path(runtime.memory_store.root).resolve()
    lines = [f"Vault: {vault_root}"]
    if result.memory_id is None:
        lines.extend(
            (
                "Memory Home: unavailable (legacy Memory)",
                "Obsidian URI: unavailable (legacy Memory)",
            )
        )
    else:
        home_relative_path = f"Memories/{result.memory_id}/Home.md"
        uri = build_obsidian_open_uri(
            vault_root,
            home_relative_path,
            vault_name=vault_name,
        )
        lines.extend(
            (
                f"Memory Home: {(vault_root / home_relative_path).resolve()}",
                f"Obsidian URI: {uri}",
            )
        )
    lines.append(f"Report: {report_path(runtime, result)}")
    return "\n".join(lines)

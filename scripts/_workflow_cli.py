"""Shared command-line interaction for the PaperPilot research workflow."""
from __future__ import annotations

from collections.abc import Callable, Mapping
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


class UserCancelled(Exception):
    """Raised when a user declines to run a reviewed research brief."""


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


async def run_memory_question(
    runtime: ResearchRuntime,
    memory_id: str,
    question: str,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> MemoryAnswer:
    """Answer from one Memory and optionally preview/confirm a new note."""
    selected = require_memory(runtime, memory_id, writable=False)
    answer = await runtime.answer_memory(selected, question)
    output_fn(format_memory_answer(answer))
    if hasattr(runtime, "get_memory_option"):
        option = runtime.get_memory_option(selected)
        if option.get("read_only"):
            output_fn("This Memory is read-only; migrate it before saving notes.")
            return answer
    action = input_fn("Save this answer as a note? [y/N]: ").strip().lower()
    if action not in {"y", "yes"}:
        return answer

    proposal = await runtime.propose_memory_note(answer)
    if not isinstance(proposal, MemoryNoteProposal):
        raise RuntimeError("Memory note proposal is invalid")
    output_fn(
        "\nMemory note preview\n"
        f"Target: {proposal.target_path}\n\n{proposal.markdown}"
    )
    confirm = input_fn("Confirm this exact note write? [y/N]: ").strip().lower()
    if confirm in {"y", "yes"}:
        result = runtime.commit_memory_note(proposal)
        output_fn(f"Saved: {result['wikilink']} ({result['target_path']})")
    else:
        output_fn("Note proposal cancelled; Memory was not changed.")
    return answer


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
        f"Source: {proposal.get('source_memory_id')} (kept read-only)",
        f"Target: {proposal.get('target_memory_id')}",
        f"Home: {proposal.get('home_path')}",
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
    confirm = input_fn("Publish this exact managed copy? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        output_fn("Migration proposal cancelled; the Vault was not changed.")
        return proposal
    descriptor = runtime.commit_legacy_memory_migration(proposal)
    output_fn(
        f"Migrated to {descriptor.memory_id}. The legacy root remains read-only; "
        "switch explicitly when ready."
    )
    return descriptor


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


async def run_reviewed_workflow(
    runtime: ResearchRuntime,
    question: str,
    *,
    thread_id: str,
    memory_id: str | None = None,
    auto_confirm: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> ResearchWorkflowResult:
    """Start one root workflow and keep it paused until the brief is accepted."""
    if memory_id is None:
        state = await runtime.start(question, thread_id=thread_id)
    else:
        state = await runtime.start(
            question,
            thread_id=thread_id,
            memory_id=memory_id,
        )
    while True:
        output_fn(format_brief(_brief_from_state(state)))
        if auto_confirm:
            action = "confirm"
        else:
            action = input_fn("Confirm, modify, or cancel? [c/m/q]: ").strip().lower()

        if action in {"c", "confirm", "yes", "y"}:
            state = await runtime.review(thread_id, "confirm")
            result = state.get("workflow_result")
            if not isinstance(result, ResearchWorkflowResult):
                raise RuntimeError("research workflow ended without a structured result")
            return result
        if action in {"m", "modify"}:
            feedback = input_fn("Brief changes: ").strip()
            if not feedback:
                output_fn("Modification feedback cannot be empty.")
                continue
            state = await runtime.review(thread_id, "modify", feedback)
            continue
        if action in {"q", "quit", "cancel", "n", "no"}:
            raise UserCancelled("research cancelled before confirmation")
        output_fn("Please enter c, m, or q.")


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

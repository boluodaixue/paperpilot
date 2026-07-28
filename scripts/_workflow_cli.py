"""Shared command-line interaction for the PaperPilot research workflow."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.research.models import ResearchBrief, ResearchWorkflowResult
from src.research.obsidian import build_obsidian_open_uri
from src.research.runtime import ResearchRuntime


class UserCancelled(Exception):
    """Raised when a user declines to run a reviewed research brief."""


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

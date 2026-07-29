"""Human-readable Markdown rendering for the single PaperPilot Memory Store."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from .models import EvidenceItem, ResearchBrief, ResearchResult
from .vault import build_wikilink


_SAFE_NOTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_MANAGED_NOTE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")


def safe_note_id(prefix: str, value: str) -> str:
    clean = str(value or "").strip()
    if _SAFE_NOTE_ID.fullmatch(clean):
        return clean
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def source_note_id(evidence: EvidenceItem) -> str:
    digest = hashlib.sha256(evidence.source_ref.encode("utf-8")).hexdigest()[:16]
    return f"Source-{digest}"


def report_note_id(root_thread_id: str) -> str:
    digest = hashlib.sha256(root_thread_id.encode("utf-8")).hexdigest()[:16]
    return f"Report-{digest}"


def managed_note_id(prefix: str, value: str) -> str:
    """Return a W0-compatible ID without changing legacy note normalization."""
    candidate = safe_note_id(prefix, value)
    if _MANAGED_NOTE_ID.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _yaml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _frontmatter(**fields: object) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {_yaml_string(value)}" for key, value in fields.items())
    lines.append("---")
    return "\n".join(lines)


def _managed_frontmatter(
    *,
    note_id: str,
    note_type: str,
    memory_id: str,
    title: str,
    created_at: str,
    updated_at: str,
    origin: str,
    status: str = "confirmed",
    **extra: object,
) -> str:
    fields: dict[str, object] = {
        "id": note_id,
        "type": note_type,
        "memory_id": memory_id,
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "origin": origin,
        "status": status,
        **extra,
    }
    lines = ["---"]
    lines.extend(f"{key}: {_yaml_string(value)}" for key, value in fields.items())
    lines.extend(("tags:", "  - paperpilot", "---"))
    return "\n".join(lines)


def _managed_wikilink(markdown_path: str, alias: str) -> str:
    try:
        return build_wikilink(markdown_path, alias)
    except ValueError:
        return build_wikilink(markdown_path)


def render_memory_home(
    *,
    memory_id: str,
    title: str,
    created_at: str,
    updated_at: str,
) -> str:
    """Render the initial human-readable index for one long-lived Memory."""
    note_id = f"Home-{memory_id[2:]}"
    frontmatter = _managed_frontmatter(
        note_id=note_id,
        note_type="home",
        memory_id=memory_id,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        origin="user",
    )
    return (
        f"{frontmatter}\n\n"
        f"# {title}\n\n"
        "## Objective\n\nNot specified\n\n"
        "## Reports\n\n- None yet.\n\n"
        "## Notes\n\n- None yet.\n\n"
        "## Imports\n\n- None yet.\n\n"
        "## Known findings\n\n- None yet.\n\n"
        "## Open questions\n\n- None yet.\n\n"
        f"## Last updated\n\n{updated_at}\n"
    )


def render_source_note(
    note_id: str,
    evidence: EvidenceItem,
    *,
    memory_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    if memory_id is None:
        frontmatter = _frontmatter(id=note_id, type="source")
    else:
        if created_at is None or updated_at is None:
            raise ValueError("managed source notes require created_at and updated_at")
        frontmatter = _managed_frontmatter(
            note_id=note_id,
            note_type="source",
            memory_id=memory_id,
            title=evidence.title or evidence.source_ref,
            created_at=created_at,
            updated_at=updated_at,
            origin="research",
        )
    return (
        f"{frontmatter}\n\n"
        f"# {evidence.title or evidence.source_ref}\n\n"
        f"- Source type: {evidence.source_type}\n"
        f"- Reference: {evidence.source_ref}\n"
        f"- Locator: {evidence.locator or evidence.source_ref}\n"
    )


def render_evidence_note(
    evidence: EvidenceItem,
    *,
    evidence_note: str,
    source_note: str,
    memory_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    excerpt_heading = "Original quote" if evidence.excerpt_type == "quote" else "Paraphrase"
    limitations = evidence.limitations or "None recorded."
    if memory_id is None:
        frontmatter = _frontmatter(id=evidence_note, type="evidence")
        source_link = f"[[sources/{source_note}|{evidence.title or evidence.source_ref}]]"
    else:
        if created_at is None or updated_at is None:
            raise ValueError("managed evidence notes require created_at and updated_at")
        frontmatter = _managed_frontmatter(
            note_id=evidence_note,
            note_type="evidence",
            memory_id=memory_id,
            title=evidence.finding,
            created_at=created_at,
            updated_at=updated_at,
            origin="research",
        )
        source_link = _managed_wikilink(
            f"Memories/{memory_id}/sources/{source_note}.md",
            evidence.title or evidence.source_ref,
        )
    return (
        f"{frontmatter}\n\n"
        f"# Evidence: {evidence.finding}\n\n"
        f"## Finding\n\n{evidence.finding}\n\n"
        f"## {excerpt_heading}\n\n{evidence.excerpt or 'No excerpt recorded.'}\n\n"
        f"## Source\n\n"
        f"{source_link}\n\n"
        f"- Locator: {evidence.locator or evidence.source_ref}\n"
        f"- Limitations: {limitations}\n"
    )


def render_report(
    brief: ResearchBrief,
    result: ResearchResult,
    *,
    report_note: str,
    evidence_notes: Mapping[str, str],
    root_thread_id: str,
    memory_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    def evidence_link(note: str) -> str:
        if memory_id is None:
            return f"[[evidence/{note}|Evidence]]"
        return _managed_wikilink(
            f"Memories/{memory_id}/evidence/{note}.md",
            "Evidence",
        )

    all_links = [
        evidence_link(note)
        for evidence_id, note in evidence_notes.items()
        if any(item.evidence_id == evidence_id for item in result.evidence)
    ]
    summary_suffix = f" {' '.join(all_links)}" if all_links else ""

    scope = "\n".join(f"- {item}" for item in brief.scope) or "- Not specified"
    directions = "\n".join(f"- {item}" for item in brief.directions) or "- Not specified"
    constraints = "\n".join(f"- {item}" for item in brief.constraints) or "- None"
    memory_context = ""
    if brief.memory_id is not None:
        memory_paths = (
            "\n".join(f"- `{path}`" for path in brief.memory_paths)
            or "- No relevant Memory notes were used."
        )
        known_information = (
            "\n".join(f"- {item}" for item in brief.known_information)
            or "- None identified."
        )
        research_gaps = (
            "\n".join(f"- {item}" for item in brief.research_gaps)
            or "- None identified."
        )
        memory_context = (
            "## Memory Context\n\n"
            f"**Memory ID:** {brief.memory_id}\n\n"
            f"**Used notes**\n\n{memory_paths}\n\n"
            f"**Known information**\n\n{known_information}\n\n"
            f"**Research gaps**\n\n{research_gaps}\n\n"
        )

    finding_lines = [f"- {finding}" for finding in result.findings]
    if not finding_lines:
        finding_lines = ["- No completed findings."]

    evidence_lines: list[str] = []
    for evidence in result.evidence:
        note = evidence_notes[evidence.evidence_id]
        evidence_lines.append(
            f"- {evidence.finding} {evidence_link(note)}"
        )
    if not evidence_lines:
        evidence_lines = ["- No source-locatable evidence was collected."]

    unresolved = "\n".join(f"- {item}" for item in result.unresolved) or "- None"
    findings_text = "\n".join(finding_lines)
    evidence_text = "\n".join(evidence_lines)
    if memory_id is None:
        frontmatter = _frontmatter(
            id=report_note,
            type="report",
            root_thread_id=root_thread_id,
        )
    else:
        if created_at is None or updated_at is None:
            raise ValueError("managed report notes require created_at and updated_at")
        frontmatter = _managed_frontmatter(
            note_id=report_note,
            note_type="report",
            memory_id=memory_id,
            title=brief.question,
            created_at=created_at,
            updated_at=updated_at,
            origin="research",
            root_thread_id=root_thread_id,
        )
    return (
        f"{frontmatter}\n\n"
        f"# {brief.question}\n\n"
        f"## Research Brief\n\n"
        f"**Objective:** {brief.objective}\n\n"
        f"**Scope**\n\n{scope}\n\n"
        f"**Directions**\n\n{directions}\n\n"
        f"**Constraints**\n\n{constraints}\n\n"
        f"**Expected output:** {brief.expected_output}\n\n"
        f"{memory_context}"
        f"## Summary\n\n{result.summary or 'No summary was produced.'}{summary_suffix}\n\n"
        f"## Findings\n\n{findings_text}\n\n"
        f"## Evidence-backed Details\n\n{evidence_text}\n\n"
        f"## Unresolved\n\n{unresolved}\n\n"
        f"## Execution\n\n"
        f"- Status: {result.status.value}\n"
        f"- Stop reason: {result.stop_reason or 'normal completion'}\n"
        f"- Iterations: {result.iterations}\n"
        f"- Tool calls: {result.tool_calls_used}\n"
    )

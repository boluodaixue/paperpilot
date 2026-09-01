"""Human-readable Markdown rendering for the single PaperPilot Memory Store."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from .evidence_selection import select_representative_evidence
from .models import EvidenceItem, ResearchBrief, ResearchResult
from .vault import build_wikilink


_SAFE_NOTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_MANAGED_NOTE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_EVIDENCE_MARKER = re.compile(r"\[\[EVIDENCE:([A-Za-z0-9._-]+)\]\]")


def _bounded_evidence_finding(value: str, *, limit: int = 500) -> str:
    clean = str(value or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 3)].rstrip() + "..."


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


def render_evidence_references(
    markdown: str,
    evidence: tuple[EvidenceItem, ...],
    evidence_notes: Mapping[str, str],
    *,
    memory_id: str | None = None,
) -> str:
    """Resolve internal markers in stable first-source-appearance order."""
    inventory = {item.evidence_id: item for item in evidence}
    source_numbers: dict[str, int] = {}
    source_first_ids: dict[str, str] = {}

    def replace_marker(match: re.Match[str]) -> str:
        evidence_id = match.group(1)
        item = inventory.get(evidence_id)
        if item is None or evidence_id not in evidence_notes:
            raise ValueError(f"unknown or unpersisted Evidence ID: {evidence_id}")
        source_key = item.source_ref
        if source_key not in source_numbers:
            source_numbers[source_key] = len(source_numbers) + 1
            source_first_ids[source_key] = evidence_id
        return f"[{source_numbers[source_key]}]"

    rendered = _EVIDENCE_MARKER.sub(replace_marker, markdown)
    if not source_numbers:
        return rendered
    references: list[str] = []
    for source_ref, number in sorted(source_numbers.items(), key=lambda pair: pair[1]):
        evidence_id = source_first_ids[source_ref]
        item = inventory[evidence_id]
        note = evidence_notes[evidence_id]
        if memory_id is None:
            link = f"[[evidence/{note}|[{number}]]]"
        else:
            path = f"Memories/{memory_id}/evidence/{note}.md"
            link = build_wikilink(path, f"[{number}]")
        references.append(
            f"{number}. {link} — {item.title or item.source_ref}; {item.locator or item.source_ref}"
        )
    return rendered.rstrip() + "\n\n## References\n\n" + "\n".join(references) + "\n"


def render_v2_report(
    brief: ResearchBrief,
    result: ResearchResult,
    report_body_markdown: str,
    *,
    report_note: str,
    evidence_notes: Mapping[str, str],
    root_thread_id: str,
    memory_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    architecture: str = "supervisor_v2",
) -> str:
    """Render an already-audited V2 body into the normal report note envelope."""
    body = str(report_body_markdown or "").strip()
    if not body:
        raise ValueError("V2 report body cannot be empty")
    resolved = render_evidence_references(
        body,
        tuple(result.evidence),
        evidence_notes,
        memory_id=memory_id,
    )
    if memory_id is None:
        frontmatter = _frontmatter(
            id=report_note,
            type="report",
            root_thread_id=root_thread_id,
            architecture=architecture,
        )
    else:
        if created_at is None or updated_at is None:
            raise ValueError("managed V2 report notes require created_at and updated_at")
        frontmatter = _managed_frontmatter(
            note_id=report_note,
            note_type="report",
            memory_id=memory_id,
            title=brief.question,
            created_at=created_at,
            updated_at=updated_at,
            origin="research",
            root_thread_id=root_thread_id,
            architecture=architecture,
        )
    return f"{frontmatter}\n\n{resolved.lstrip()}"


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

    report_evidence = select_representative_evidence(
        result.evidence,
        result.coverage,
        limit=24,
        max_per_requirement=6,
        max_per_source=2,
    )
    all_links = [
        evidence_link(evidence_notes[item.evidence_id])
        for item in report_evidence[:12]
        if item.evidence_id in evidence_notes
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
    for evidence in report_evidence:
        note = evidence_notes[evidence.evidence_id]
        evidence_lines.append(
            f"- {_bounded_evidence_finding(evidence.finding)} {evidence_link(note)}"
        )
    if not evidence_lines:
        evidence_lines = ["- No source-locatable evidence was collected."]
    elif len(report_evidence) < len(result.evidence):
        evidence_lines.append(
            f"- Showing {len(report_evidence)} of {len(result.evidence)} collected evidence items; "
            "the complete evidence inventory remains stored in the Vault."
        )

    unresolved = "\n".join(f"- {item}" for item in result.unresolved) or "- None"
    availability = ""
    if result.tool_alerts:
        availability_lines = "\n".join(
            f"- **{item.category} / {item.tool} / {item.target or 'unknown'}:** "
            f"{item.message} {item.action_required}"
            for item in result.tool_alerts
        )
        availability = (
            "## External Information Availability\n\n"
            f"{availability_lines}\n\n"
        )
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
        f"{availability}"
        f"## Unresolved\n\n{unresolved}\n\n"
        f"## Execution\n\n"
        f"- Research status: {result.status.value}\n"
        f"- Termination reason: "
        f"{result.termination_reason.value if result.termination_reason else 'unspecified'}\n"
        f"- Output status: {result.output_status.value}\n"
        f"- Resource/error detail: {result.stop_reason or 'none'}\n"
        f"- Iterations: {result.iterations}\n"
        f"- Tool calls: {result.tool_calls_used}\n"
    )

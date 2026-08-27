"""Human-readable Markdown rendering for the single PaperPilot Memory Store."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from .models import EvidenceItem, ResearchBrief, ResearchResult


_SAFE_NOTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


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


def _yaml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _frontmatter(**fields: object) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {_yaml_string(value)}" for key, value in fields.items())
    lines.append("---")
    return "\n".join(lines)


def render_source_note(note_id: str, evidence: EvidenceItem) -> str:
    return (
        f"{_frontmatter(id=note_id, type='source')}\n\n"
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
) -> str:
    excerpt_heading = "Original quote" if evidence.excerpt_type == "quote" else "Paraphrase"
    limitations = evidence.limitations or "None recorded."
    return (
        f"{_frontmatter(id=evidence_note, type='evidence')}\n\n"
        f"# Evidence: {evidence.finding}\n\n"
        f"## Finding\n\n{evidence.finding}\n\n"
        f"## {excerpt_heading}\n\n{evidence.excerpt or 'No excerpt recorded.'}\n\n"
        f"## Source\n\n"
        f"[[sources/{source_note}|{evidence.title or evidence.source_ref}]]\n\n"
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
) -> str:
    all_links = [
        f"[[evidence/{note}|Evidence]]"
        for evidence_id, note in evidence_notes.items()
        if any(item.evidence_id == evidence_id for item in result.evidence)
    ]
    summary_suffix = f" {' '.join(all_links)}" if all_links else ""

    scope = "\n".join(f"- {item}" for item in brief.scope) or "- Not specified"
    directions = "\n".join(f"- {item}" for item in brief.directions) or "- Not specified"
    constraints = "\n".join(f"- {item}" for item in brief.constraints) or "- None"

    finding_lines = [f"- {finding}" for finding in result.findings]
    if not finding_lines:
        finding_lines = ["- No completed findings."]

    evidence_lines: list[str] = []
    for evidence in result.evidence:
        note = evidence_notes[evidence.evidence_id]
        evidence_lines.append(
            f"- {evidence.finding} [[evidence/{note}|Evidence]]"
        )
    if not evidence_lines:
        evidence_lines = ["- No source-locatable evidence was collected."]

    unresolved = "\n".join(f"- {item}" for item in result.unresolved) or "- None"
    findings_text = "\n".join(finding_lines)
    evidence_text = "\n".join(evidence_lines)
    return (
        f"{_frontmatter(id=report_note, type='report', root_thread_id=root_thread_id)}\n\n"
        f"# {brief.question}\n\n"
        f"## Research Brief\n\n"
        f"**Objective:** {brief.objective}\n\n"
        f"**Scope**\n\n{scope}\n\n"
        f"**Directions**\n\n{directions}\n\n"
        f"**Constraints**\n\n{constraints}\n\n"
        f"**Expected output:** {brief.expected_output}\n\n"
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

"""Deterministic and one-shot semantic citation audit for V2 reports."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, Iterable

from .models import EvidenceItem
from .policy import call_policy
from .report_composer import EVIDENCE_MARKER
from .report_review import parse_json_object
from .v2_contracts import CitationAuditOutcome, CitationIssue, SupervisorOutcome


_URL = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.IGNORECASE)
_WIKILINK = re.compile(r"\[\[(?!EVIDENCE:)[^\]]+\]\]")
_SEMANTIC_FIELDS = {
    "claim_text", "section", "evidence_ids", "category", "severity", "repair_action"
}
_EDIT_FIELDS = {"operation", "target", "replacement"}
_REPAIR_OPS = {"ADD_CITATION", "REPLACE_CITATION", "QUALIFY", "DELETE"}
_ISSUE_PRIORITY = {"invalid": 0, "conflict": 1, "overclaim": 2, "locator": 3, "missing": 4}
_NON_CLAIM_SECTIONS = {
    "unresolved",
    "references",
    "execution",
    "external information availability",
    "红方未解决问题 / unresolved red-team issues",
}


def citation_followup_directives(
    issues: Iterable[CitationIssue],
    outcome: SupervisorOutcome,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Map critical citation gaps to existing claim/question lineage.

    A report sentence without deterministic lineage is not allowed to launch
    open-ended research. It remains a disclosed/repairable citation issue.
    """
    claims = tuple(
        claim for result in outcome.worker_results for claim in result.claims
    )
    guidance: dict[str, list[str]] = {}
    for issue in issues:
        if issue.severity != "high" or issue.category not in {
            "missing",
            "conflict",
            "locator",
        }:
            continue
        issue_text = issue.claim_text.casefold()
        issue_evidence = set(issue.evidence_ids)
        related = tuple(
            claim
            for claim in claims
            if issue_evidence.intersection(claim.evidence_ids)
            or (
                claim.claim
                and (
                    claim.claim.casefold() in issue_text
                    or issue_text in claim.claim.casefold()
                )
            )
        )
        for claim in related:
            directives = tuple(item for item in (
                f"Citation issue {issue.issue_id} ({issue.category}): {issue.claim_text}",
                f"Required repair: {issue.repair_action}",
                f"Verify or replace challenged claim {claim.claim_id}: {claim.claim}",
                "Find newly opened, source-locatable evidence; do not reuse an unsupported locator.",
            ) if item)
            for question_id in claim.question_ids:
                guidance.setdefault(question_id, []).extend(directives)
    stable = {
        question_id: tuple(dict.fromkeys(items))
        for question_id, items in guidance.items()
    }
    return tuple(stable), stable


def _issue(
    claim_text: str,
    section: str,
    evidence_ids: tuple[str, ...],
    category: str,
    severity: str,
    action: str,
) -> CitationIssue:
    return CitationIssue.create(
        claim_text, section, evidence_ids, category, severity, action
    )


def _deduplicate_issues(issues: Iterable[CitationIssue]) -> tuple[CitationIssue, ...]:
    """Keep one terminal issue per report span, with structural errors first."""

    selected: dict[tuple[str, str], CitationIssue] = {}
    for issue in issues:
        key = (issue.section.casefold(), issue.claim_text.strip())
        current = selected.get(key)
        if current is None or _ISSUE_PRIORITY.get(issue.category, 99) < _ISSUE_PRIORITY.get(current.category, 99):
            selected[key] = issue
    return tuple(selected.values())


def deterministic_citation_issues(
    markdown: str,
    evidence: Iterable[EvidenceItem],
) -> tuple[CitationIssue, ...]:
    """Check identifiers and source inventory before any semantic model call."""
    items = tuple(evidence)
    inventory = {item.evidence_id: item for item in items}
    issues: list[CitationIssue] = []
    for evidence_id in dict.fromkeys(EVIDENCE_MARKER.findall(markdown)):
        item = inventory.get(evidence_id)
        marker = f"[[EVIDENCE:{evidence_id}]]"
        if item is None:
            issues.append(_issue(marker, "document", (evidence_id,), "invalid", "high", "delete"))
        elif not item.source_ref or not item.locator or not item.excerpt:
            issues.append(_issue(marker, "document", (evidence_id,), "locator", "high", "replace"))
        elif item.limitations.startswith("Search-result snippet"):
            issues.append(_issue(marker, "document", (evidence_id,), "invalid", "high", "delete"))

    for url in dict.fromkeys(_URL.findall(markdown)):
        issues.append(_issue(url, "document", (), "invalid", "high", "delete"))
    for wikilink in dict.fromkeys(_WIKILINK.findall(markdown)):
        issues.append(_issue(wikilink, "document", (), "invalid", "high", "delete"))

    section = "document"
    in_unresolved = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            section = line.lstrip("#").strip() or "document"
            in_unresolved = section.lower() == "unresolved"
            continue
        if (
            not line
            or in_unresolved
            or section.casefold() in _NON_CLAIM_SECTIONS
            or line.startswith(("---", "|---"))
        ):
            continue
        marker_ids = set(EVIDENCE_MARKER.findall(line))
        structurally_invalid = bool(
            _URL.search(line)
            or _WIKILINK.search(line)
            or any(
                evidence_id not in inventory
                or not inventory[evidence_id].source_ref
                or not inventory[evidence_id].locator
                or not inventory[evidence_id].excerpt
                or inventory[evidence_id].limitations.startswith("Search-result snippet")
                for evidence_id in marker_ids
            )
        )
        if structurally_invalid:
            continue
        plain = EVIDENCE_MARKER.sub("", line)
        plain = _URL.sub("", plain)
        word_count = len(re.findall(r"\w+", plain, flags=re.UNICODE))
        if word_count >= 6 and not EVIDENCE_MARKER.search(line):
            issues.append(_issue(line, section, (), "missing", "medium", "add_citation"))
    return _deduplicate_issues(issues)


def _semantic_prompt(markdown: str, evidence: tuple[EvidenceItem, ...]):
    return [
        {
            "role": "system",
            "content": """Audit whether each material statement is supported by its
supplied Evidence IDs. Do not research or use tools. Return exactly
{\"issues\": [...]} with fields claim_text, section, evidence_ids, category,
severity, repair_action. category is missing, invalid, overclaim, conflict, or
locator. Use only exact report text and supplied IDs; return [] when supported.""",
        },
        {"role": "user", "content": json.dumps({
            "report_markdown": markdown,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "source_type": item.source_type,
                    "title": item.title,
                    "source_ref": item.source_ref,
                    "locator": item.locator,
                    "excerpt": item.excerpt,
                    "limitations": item.limitations,
                }
                for item in evidence
            ],
        }, ensure_ascii=False)},
    ]


async def audit_citations(
    policy: Any,
    markdown: str,
    evidence: Iterable[EvidenceItem],
    *,
    semantic: bool = True,
) -> CitationAuditOutcome:
    items = tuple(evidence)
    deterministic = deterministic_citation_issues(markdown, items)
    if deterministic:
        return CitationAuditOutcome(status="issues", issues=deterministic)
    if not semantic:
        return CitationAuditOutcome(status="passed")
    response = await call_policy(policy, _semantic_prompt(markdown, items), [])
    payload = parse_json_object(response, role="Citation audit")
    if set(payload) != {"issues"} or not isinstance(payload["issues"], list):
        raise ValueError("Citation audit must contain only an issues list")
    known_ids = {item.evidence_id for item in items}
    issues: list[CitationIssue] = []
    for item in payload["issues"]:
        if not isinstance(item, dict) or set(item) != _SEMANTIC_FIELDS:
            raise ValueError("each CitationIssue must match the required schema")
        claim_text = str(item["claim_text"] or "").strip()
        if not claim_text or claim_text not in markdown:
            raise ValueError("CitationIssue claim_text must be exact report text")
        evidence_ids = tuple(dict.fromkeys(str(value) for value in item["evidence_ids"]))
        if set(evidence_ids) - known_ids:
            raise ValueError("Citation audit references unknown Evidence ID")
        issues.append(CitationIssue.create(
            claim_text=claim_text,
            section=item["section"],
            evidence_ids=evidence_ids,
            category=item["category"],
            severity=item["severity"],
            repair_action=item["repair_action"],
        ))
    deduplicated = _deduplicate_issues(issues)
    return CitationAuditOutcome(status="issues" if deduplicated else "passed", issues=deduplicated)


def _validate_repair_inventory(markdown: str, evidence: tuple[EvidenceItem, ...]) -> None:
    known_ids = {item.evidence_id for item in evidence}
    unknown_ids = set(EVIDENCE_MARKER.findall(markdown)) - known_ids
    if unknown_ids:
        raise ValueError(f"Citation repair added unknown Evidence IDs: {sorted(unknown_ids)}")
    raw_urls = set(_URL.findall(markdown))
    if raw_urls:
        raise ValueError(f"Citation repair cannot add raw URLs: {sorted(raw_urls)}")
    if _WIKILINK.search(markdown):
        raise ValueError("Citation repair cannot add WikiLinks")


def _repair_prompt(markdown: str, evidence: tuple[EvidenceItem, ...], issues: tuple[CitationIssue, ...]):
    return [
        {
            "role": "system",
            "content": """Repair only the supplied citation issues without tools or new
sources. Return exactly edits and report_markdown. Each edit has operation,
target, replacement. Allowed operations: ADD_CITATION, REPLACE_CITATION,
QUALIFY, DELETE. Targets must be exact and unique at that replay step. Use only
supplied Evidence IDs and never write raw URLs. QUALIFY may narrow a statement; DELETE requires
empty replacement.""",
        },
        {"role": "user", "content": json.dumps({
            "report_markdown": markdown,
            "issues": [asdict(item) for item in issues],
            "evidence": [asdict(item) for item in evidence],
        }, ensure_ascii=False)},
    ]


async def repair_citations(
    policy: Any,
    markdown: str,
    evidence: Iterable[EvidenceItem],
    issues: Iterable[CitationIssue],
) -> CitationAuditOutcome:
    evidence_items = tuple(evidence)
    issue_items = tuple(issues)
    if not issue_items:
        return CitationAuditOutcome(status="passed", repaired_markdown=markdown)
    response = await call_policy(policy, _repair_prompt(markdown, evidence_items, issue_items), [])
    payload = parse_json_object(response, role="Citation repair")
    if set(payload) != {"edits", "report_markdown"}:
        raise ValueError("Citation repair must contain only edits and report_markdown")
    if not isinstance(payload["edits"], list) or not payload["edits"]:
        raise ValueError("Citation repair requires at least one edit")
    revised = markdown
    known_ids = {item.evidence_id for item in evidence_items}
    for edit in payload["edits"]:
        if not isinstance(edit, dict) or set(edit) != _EDIT_FIELDS:
            raise ValueError("each Citation repair edit must match the required schema")
        operation = str(edit["operation"]).upper()
        target = str(edit["target"])
        replacement = str(edit["replacement"])
        if operation not in _REPAIR_OPS:
            raise ValueError("unsupported Citation repair operation")
        if not target or revised.count(target) != 1:
            raise ValueError("Citation repair target must occur exactly once")
        replacement_ids = set(EVIDENCE_MARKER.findall(replacement))
        if replacement_ids - known_ids:
            raise ValueError("Citation repair replacement references unknown Evidence ID")
        if operation == "DELETE":
            if replacement:
                raise ValueError("DELETE requires empty replacement")
            revised = revised.replace(target, "", 1)
        elif operation == "ADD_CITATION":
            if not replacement_ids or EVIDENCE_MARKER.sub("", replacement).strip():
                raise ValueError("ADD_CITATION can add only known Evidence markers")
            revised = revised.replace(target, target + replacement, 1)
        elif operation == "REPLACE_CITATION":
            if not EVIDENCE_MARKER.fullmatch(target) or not EVIDENCE_MARKER.fullmatch(replacement):
                raise ValueError("REPLACE_CITATION requires Evidence markers")
            revised = revised.replace(target, replacement, 1)
        else:
            if not replacement.strip():
                raise ValueError("QUALIFY requires replacement text")
            revised = revised.replace(target, replacement, 1)
    if revised != payload["report_markdown"]:
        raise ValueError("Citation repair report contains undeclared edits")
    _validate_repair_inventory(revised, evidence_items)
    remaining = deterministic_citation_issues(revised, evidence_items)
    repaired_issues = tuple(
        CitationIssue(
            issue_id=item.issue_id,
            claim_text=item.claim_text,
            section=item.section,
            evidence_ids=item.evidence_ids,
            category=item.category,
            severity=item.severity,
            repair_action=item.repair_action,
            status="repaired",
        )
        for item in issue_items
    )
    return CitationAuditOutcome(
        status="partial" if remaining else "repaired",
        issues=(*repaired_issues, *remaining),
        repaired_markdown=revised,
        unresolved=(
            (f"Citation audit left {len(remaining)} structural issue(s) unresolved.",)
            if remaining else ()
        ),
    )

"""Bounded Red/Blue post-processing for an already completed root report."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Any

from .models import (
    MemoryManifest,
    ReportEdit,
    ReportIssue,
    ReportReviewOutcome,
    ResearchResult,
)
from .policy import call_policy


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_URL = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.IGNORECASE)
_ISSUE_CATEGORIES = {"factual", "logical_consistency", "citation_quality"}
_EDIT_OPERATIONS = {"ADD", "DELETE", "MODIFY", "VERIFY"}


def _red_prompt(report_markdown: str, result: ResearchResult) -> list[dict[str, Any]]:
    evidence = [asdict(item) for item in result.evidence]
    return [
        {
            "role": "system",
            "content": """You are the Red reviewer for a completed PaperPilot report.
Review only the supplied report against the supplied evidence. Do not research,
retrieve, call tools, invent evidence, add sources, or score the report. Return
exactly one JSON object:
{
  "issues": [
    {
      "category": "factual | logical_consistency | citation_quality",
      "target": "exact report text or heading",
      "description": "specific problem grounded in the supplied material"
    }
  ]
}
Return an empty issues list when there is no valid issue. No other categories
or fields are allowed.
""",
        },
        {
            "role": "user",
            "content": (
                f"REPORT:\n{report_markdown}\n\n"
                f"SUPPLIED EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False)}"
            ),
        },
    ]


def _blue_prompt(
    report_markdown: str,
    result: ResearchResult,
    issues: tuple[ReportIssue, ...],
) -> list[dict[str, Any]]:
    evidence = [asdict(item) for item in result.evidence]
    return [
        {
            "role": "system",
            "content": """You are the Blue editor for a completed PaperPilot report.
Use only the supplied report, review findings, and supplied evidence. Do not research,
retrieve, call tools, invent evidence or sources, change YAML frontmatter, add or
remove URLs, or add/remove/retarget WikiLinks. Return exactly one JSON object:
{
  "edits": [
    {
      "operation": "ADD | DELETE | MODIFY | VERIFY",
      "target": "exact existing report text or heading",
      "replacement": "replacement/addition text, or empty for DELETE/VERIFY"
    }
  ],
  "report_markdown": "the complete final Markdown report"
}
Every edit must use one of the four operations. VERIFY records that the target
was checked against supplied evidence and does not itself change report text.
Apply edits in array order. At each step target must occur exactly once in the
current text: ADD appends non-empty replacement immediately after target;
DELETE removes target and requires empty replacement; MODIFY replaces target
with non-empty replacement; VERIFY leaves text unchanged and requires empty
replacement. report_markdown must equal the exact result of those operations.
""",
        },
        {
            "role": "user",
            "content": (
                f"REPORT:\n{report_markdown}\n\n"
                f"RED ISSUES:\n{json.dumps([asdict(item) for item in issues], ensure_ascii=False)}\n\n"
                f"SUPPLIED EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False)}"
            ),
        },
    ]


def _json_object(response: dict[str, Any], *, role: str) -> dict[str, Any]:
    candidate = str(response.get("content") or "").strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{role} review must return valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{role} review must return a JSON object")
    return value


def _parse_issues(
    response: dict[str, Any],
    *,
    original_report: str,
) -> tuple[ReportIssue, ...]:
    payload = _json_object(response, role="Red")
    if set(payload) != {"issues"} or not isinstance(payload["issues"], list):
        raise ValueError("Red review must contain only an issues list")
    issues: list[ReportIssue] = []
    for item in payload["issues"]:
        if not isinstance(item, dict) or set(item) != {
            "category",
            "target",
            "description",
        }:
            raise ValueError("each Red issue must match the required schema")
        category = str(item["category"] or "").strip()
        target = str(item["target"] or "").strip()
        description = str(item["description"] or "").strip()
        if category not in _ISSUE_CATEGORIES:
            raise ValueError(f"unsupported Red issue category: {category}")
        if not target or not description:
            raise ValueError("Red issue target and description cannot be empty")
        if target not in original_report:
            raise ValueError("Red issue target must be exact text in the report")
        issues.append(ReportIssue(category, target, description))
    return tuple(issues)


def _parse_blue(
    response: dict[str, Any],
    *,
    original_report: str,
) -> tuple[tuple[ReportEdit, ...], str]:
    payload = _json_object(response, role="Blue")
    if set(payload) != {"edits", "report_markdown"}:
        raise ValueError("Blue review must contain only edits and report_markdown")
    if not isinstance(payload["edits"], list) or not payload["edits"]:
        raise ValueError("Blue review must contain at least one edit")
    revised_report = payload["report_markdown"]
    if not isinstance(revised_report, str):
        raise ValueError("Blue report_markdown must be a string")

    edits: list[ReportEdit] = []
    applied_report = original_report
    for item in payload["edits"]:
        if not isinstance(item, dict) or set(item) != {
            "operation",
            "target",
            "replacement",
        }:
            raise ValueError("each Blue edit must match the required schema")
        operation = str(item["operation"] or "").strip().upper()
        target = str(item["target"] or "").strip()
        replacement = str(item["replacement"] or "")
        if operation not in _EDIT_OPERATIONS:
            raise ValueError(f"unsupported Blue operation: {operation}")
        if not target or applied_report.count(target) != 1:
            raise ValueError("Blue edit target must occur exactly once in the current report")
        if operation == "ADD":
            if not replacement:
                raise ValueError("Blue ADD requires replacement text")
            applied_report = applied_report.replace(target, target + replacement, 1)
        elif operation == "DELETE":
            if replacement:
                raise ValueError("Blue DELETE requires an empty replacement")
            applied_report = applied_report.replace(target, "", 1)
        elif operation == "MODIFY":
            if not replacement:
                raise ValueError("Blue MODIFY requires replacement text")
            applied_report = applied_report.replace(target, replacement, 1)
        else:
            if replacement:
                raise ValueError("Blue VERIFY requires an empty replacement")
        edits.append(ReportEdit(operation, target, replacement))

    if applied_report != revised_report:
        raise ValueError("Blue report_markdown contains undeclared or incorrectly applied edits")
    return tuple(edits), revised_report


def _frontmatter_bytes(markdown: str) -> str:
    if markdown.startswith("---\r\n"):
        newline = "\r\n"
    elif markdown.startswith("---\n"):
        newline = "\n"
    else:
        raise ValueError("report must start with YAML frontmatter")
    marker = f"{newline}---"
    closing = markdown.find(marker, len(f"---{newline}"))
    if closing < 0:
        raise ValueError("report YAML frontmatter is not closed")
    return markdown[: closing + len(marker)]


def _wikilink_targets(markdown: str) -> Counter[str]:
    return Counter(match.group(1).strip() for match in _WIKILINK.finditer(markdown))


def _manifest_targets(manifest: MemoryManifest) -> set[str]:
    paths = (
        manifest.report_path,
        *manifest.evidence_paths,
        *manifest.source_paths,
    )
    targets: set[str] = set()
    for path in paths:
        normalized = PurePosixPath(str(path).replace("\\", "/")).as_posix()
        if normalized.lower().endswith(".md"):
            normalized = normalized[:-3]
        targets.add(normalized)
    return targets


def validate_revised_report(
    original_report: str,
    revised_report: str,
    manifest: MemoryManifest,
) -> None:
    """Reject any revision that changes report identity or citation inventory."""
    if not revised_report.strip():
        raise ValueError("revised report cannot be empty")
    if _frontmatter_bytes(revised_report) != _frontmatter_bytes(original_report):
        raise ValueError("revised report changed YAML frontmatter")

    original_links = _wikilink_targets(original_report)
    revised_links = _wikilink_targets(revised_report)
    if revised_links != original_links:
        raise ValueError("revised report changed WikiLink targets")
    allowed_targets = _manifest_targets(manifest)
    for target in revised_links:
        base_target = target.split("#", 1)[0]
        if base_target.lower().endswith(".md"):
            base_target = base_target[:-3]
        if base_target not in allowed_targets:
            raise ValueError(f"WikiLink target is absent from the manifest: {target}")

    if Counter(_URL.findall(revised_report)) != Counter(_URL.findall(original_report)):
        raise ValueError("revised report changed external URLs")


async def review_final_report(
    policy: Any,
    report_markdown: str,
    result: ResearchResult,
    manifest: MemoryManifest,
) -> tuple[str, ReportReviewOutcome]:
    """Run one Red call and, only for valid issues, one Blue call."""
    red_response = await call_policy(policy, _red_prompt(report_markdown, result), [])
    issues = _parse_issues(red_response, original_report=report_markdown)
    if not issues:
        return report_markdown, ReportReviewOutcome(applied=False)

    blue_response = await call_policy(
        policy,
        _blue_prompt(report_markdown, result, issues),
        [],
    )
    edits, revised_report = _parse_blue(
        blue_response,
        original_report=report_markdown,
    )
    validate_revised_report(report_markdown, revised_report, manifest)
    return revised_report, ReportReviewOutcome(
        applied=revised_report != report_markdown,
        issues=issues,
        edits=edits,
    )

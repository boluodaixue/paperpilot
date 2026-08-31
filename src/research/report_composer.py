"""Evidence-only Lead report composition for Research Agent V2.

The separate final-report node boundary follows LangChain Deep Research From
Scratch at commit ``93f35e5d2a51590f9542207a9ff66a01901da5bc``. The refusal
to write without usable context is informed by GPT Researcher at commit
``6f998324006fd8e30d6e98e8815641da158d583c``. See third-party notices.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import OutputStatus
from .policy import call_policy
from .report_review import parse_json_object
from .v2_contracts import (
    EvidenceClaim,
    ReportDraft,
    ResearchChallenge,
    ResearchChallengeLoopOutcome,
    ResearchPlan,
    SupervisorOutcome,
)


EVIDENCE_MARKER = re.compile(r"\[\[EVIDENCE:([A-Za-z0-9._-]+)\]\]")
_URL = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.IGNORECASE)
_FALSE_CHALLENGE_ABSENCE = (
    re.compile(r"(?:不存在|没有|未提供|无).{0,40}(?:挑战|红方).{0,20}(?:信息|记录|项目)?"),
    re.compile(r"无需.{0,30}(?:挑战|红方)"),
    re.compile(
        r"\b(?:no|without)\s+(?:(?:accepted|deferred|pending|unresolved|red[- ]?team)\s+)?"
        r"challenge(?:s|\s+information)?\b",
        re.IGNORECASE,
    ),
)


def _is_markdown_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3


def _is_markdown_table_separator(line: str) -> bool:
    if not _is_markdown_table_row(line):
        return False
    cells = [item.strip() for item in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", item) for item in cells)


def _table_row_as_bullet(line: str) -> str:
    cells = [item.strip() for item in line.strip().strip("|").split("|")]
    return "- " + " — ".join(item for item in cells if item)


def drop_unsafe_markdown_lines(markdown: str, unsafe_tokens: set[str]) -> str:
    """Remove unsafe claims without leaving malformed Markdown tables.

    A bad body row can be removed independently. If the header or separator is
    unsafe, the remaining safe rows are downgraded to bullets because their
    column meaning can no longer be represented safely.
    """
    lines = markdown.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if not _is_markdown_table_row(lines[index]):
            line = lines[index]
            if not any(token in line for token in unsafe_tokens):
                output.append(line)
            index += 1
            continue

        end = index
        while end < len(lines) and _is_markdown_table_row(lines[end]):
            end += 1
        block = lines[index:end]
        unsafe = [any(token in line for token in unsafe_tokens) for line in block]
        header_or_separator_unsafe = bool(unsafe and unsafe[0]) or (
            len(unsafe) > 1 and unsafe[1]
        )
        if header_or_separator_unsafe:
            safe_rows = [
                line
                for position, line in enumerate(block)
                if not unsafe[position] and not _is_markdown_table_separator(line)
            ]
            output.extend(_table_row_as_bullet(line) for line in safe_rows)
        else:
            kept = [line for position, line in enumerate(block) if not unsafe[position]]
            if len(kept) >= 2 and _is_markdown_table_separator(kept[1]):
                output.extend(kept)
            else:
                output.extend(
                    _table_row_as_bullet(line)
                    for line in kept
                    if not _is_markdown_table_separator(line)
                )
        index = end
    return "\n".join(output).strip()


def _drop_false_challenge_absence(
    markdown: str,
    challenges: tuple[ResearchChallenge, ...],
) -> tuple[str, bool]:
    """Remove model assertions that contradict the supplied Red state."""

    if not challenges:
        return markdown, False
    kept: list[str] = []
    removed = False
    for line in markdown.splitlines():
        if any(pattern.search(line) for pattern in _FALSE_CHALLENGE_ABSENCE):
            removed = True
            continue
        kept.append(line)
    return "\n".join(kept).strip(), removed


_drop_unsafe_lines = drop_unsafe_markdown_lines


def _selected_claims(
    outcome: SupervisorOutcome,
    blocked_claim_ids: set[str] | None = None,
) -> tuple[EvidenceClaim, ...]:
    blocked = blocked_claim_ids or set()
    evidence = {
        item.evidence_id: item
        for result in outcome.worker_results
        for item in result.evidence
        if item.source_ref and item.locator and item.excerpt
        and not item.limitations.startswith("Search-result snippet")
    }
    return tuple(
        claim
        for result in outcome.worker_results
        for claim in result.claims
        if claim.claim_id not in blocked
        and claim.evidence_ids
        and all(item in evidence for item in claim.evidence_ids)
        and claim.source_ref and claim.locator and claim.excerpt
    )


def _bounded_claims(
    plan: ResearchPlan,
    claims: tuple[EvidenceClaim, ...],
    *,
    per_question: int = 2,
    total_limit: int = 12,
) -> tuple[EvidenceClaim, ...]:
    """Keep composer context bounded while preserving every Core Question."""
    confidence_rank = {"high": 0, "medium": 1, "low": 2}

    def rank(item: EvidenceClaim) -> tuple[Any, ...]:
        return (
            confidence_rank.get(item.confidence, 3),
            bool(item.limitations),
            not bool(item.comparability_notes),
            item.claim_id,
        )

    selected: list[EvidenceClaim] = []
    selected_ids: set[str] = set()
    for question in plan.core_questions:
        candidates = sorted(
            (item for item in claims if question.question_id in item.question_ids),
            key=rank,
        )
        for item in candidates[:per_question]:
            if item.claim_id not in selected_ids:
                selected.append(item)
                selected_ids.add(item.claim_id)
                if len(selected) >= total_limit:
                    return tuple(selected)
    for item in sorted(claims, key=rank):
        if item.claim_id not in selected_ids:
            selected.append(item)
            selected_ids.add(item.claim_id)
            if len(selected) >= total_limit:
                break
    return tuple(selected)


def _composer_prompt(
    plan: ResearchPlan,
    outcome: SupervisorOutcome,
    claims: tuple[EvidenceClaim, ...],
    challenges: tuple[ResearchChallenge, ...],
    adjudications: tuple[Any, ...],
):
    evidence_by_id = {
        item.evidence_id: item
        for result in outcome.worker_results
        for item in result.evidence
    }
    selected_evidence_ids = tuple(dict.fromkeys(item for claim in claims for item in claim.evidence_ids))
    payload = {
        "plan": {
            "plan_id": plan.plan_id,
            "core_questions": [
                {
                    "question_id": item.question_id,
                    "description": item.description[:800],
                    "required": item.required,
                    "priority": item.priority,
                }
                for item in plan.core_questions
            ],
            "report_outline": [item[:500] for item in plan.report_outline[:12]],
            "source_guidance": [item[:600] for item in plan.source_guidance[:10]],
        },
        "selected_claims": [
            {
                "claim_id": item.claim_id,
                "claim": item.claim[:1000],
                "question_ids": list(item.question_ids),
                "evidence_ids": list(item.evidence_ids),
                "source_ref": item.source_ref,
                "locator": item.locator,
                "excerpt": item.excerpt[:800],
                "limitations": item.limitations[:400],
                "confidence": item.confidence,
                "comparability_notes": item.comparability_notes[:400],
            }
            for item in claims
        ],
        "evidence": [
            {
                "evidence_id": evidence_by_id[item].evidence_id,
                "finding": evidence_by_id[item].finding[:800],
                "source_type": evidence_by_id[item].source_type,
                "title": evidence_by_id[item].title[:400],
                "source_ref": evidence_by_id[item].source_ref,
                "locator": evidence_by_id[item].locator,
                "excerpt": evidence_by_id[item].excerpt[:1000],
                "limitations": evidence_by_id[item].limitations[:400],
            }
            for item in selected_evidence_ids
        ],
        "unresolved_question_ids": list(outcome.unresolved_question_ids),
        "research_challenges": [
            {
                "challenge_id": item.challenge_id,
                "category": item.category,
                "target_question_ids": list(item.target_question_ids),
                "target_claim_ids": list(item.target_claim_ids),
                "reason": item.reason[:700],
                "severity": item.severity,
                "status": item.status,
                "resolution_evidence_ids": list(item.resolution_evidence_ids),
                "resolution_reason": item.resolution_reason[:500],
            }
            for item in challenges[:16]
        ],
        "lead_adjudications": [
            {
                "challenge_id": item.challenge_id,
                "decision": item.decision.value,
                "evidence_ids": list(item.evidence_ids),
                "reason": item.reason[:500],
            }
            for item in adjudications[:16]
        ],
    }
    return [
        {
            "role": "system",
            "content": """You are the Lead Researcher composing the final report draft.
Use only selected Evidence Claims; do not research, call tools, or use unseen
context. Every material factual statement must carry one or more exact internal
markers like [[EVIDENCE:evidence-id]]. If evidence does not support a statement,
omit it, qualify it as inference, or put it under Unresolved. Do not invent URLs
or WikiLinks. An accepted, deferred, or pending challenge is a binding limit:
do not restate its targeted claim as fact, and disclose it under Unresolved.
The same rule applies to unresolved_disclosed. If research_challenges is not
empty, never claim that no challenge information exists; the workflow will
append the authoritative Red-team issue inventory after your draft.
Return exactly a JSON object with report_markdown and unresolved.""",
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


async def compose_report(
    policy: Any,
    plan: ResearchPlan,
    reviewed: SupervisorOutcome | ResearchChallengeLoopOutcome,
) -> ReportDraft:
    """Compose a report without exposing research tools or raw Worker logs."""
    if isinstance(reviewed, ResearchChallengeLoopOutcome):
        outcome = reviewed.supervisor_outcome
        challenges = tuple(reviewed.challenges)
        adjudications = tuple(reviewed.adjudications)
    else:
        outcome = reviewed
        challenges = ()
        adjudications = ()
    if outcome.plan_id != plan.plan_id:
        raise ValueError("Supervisor outcome belongs to another ResearchPlan")
    unresolved_challenges = tuple(
        item
        for item in challenges
        if item.status in {"pending", "accepted", "deferred", "unresolved_disclosed"}
    )
    blocked_claim_ids = {
        claim_id
        for challenge in unresolved_challenges
        for claim_id in challenge.target_claim_ids
    }
    claims = _bounded_claims(
        plan,
        _selected_claims(outcome, blocked_claim_ids),
    )
    challenge_unresolved = tuple(
        f"Red challenge {item.challenge_id} ({item.category}, {item.severity}): {item.reason}"
        for item in unresolved_challenges
    )
    if not claims:
        unresolved = tuple(dict.fromkeys((
            "No source-locatable evidence was available; report generation abstained.",
            *outcome.unresolved_question_ids,
            *challenge_unresolved,
        )))
        return ReportDraft(
            plan_id=plan.plan_id,
            markdown=(
                "# Research result\n\n"
                "No source-locatable evidence was available, so PaperPilot abstained "
                "from drafting unsupported findings.\n\n## Unresolved\n\n"
                + "\n".join(f"- {item}" for item in unresolved)
            ),
            status="abstained",
            unresolved=unresolved,
            output_status=OutputStatus.FALLBACK,
        )
    messages = _composer_prompt(plan, outcome, claims, challenges, adjudications)
    response = await call_policy(policy, messages, [])
    try:
        payload = parse_json_object(response, role="Lead report composer")
    except ValueError:
        invalid_content = str(response.get("content") or "")[:16000]
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Repair the Lead report composer response. Return exactly one valid "
                    "JSON object with report_markdown (string) and unresolved (array of "
                    "strings). Preserve only the supplied Evidence markers and do not add "
                    "facts, URLs, citations, or commentary."
                ),
            },
            {
                "role": "user",
                "content": (
                    "ORIGINAL COMPOSER INPUT:\n"
                    + messages[-1]["content"]
                    + "\n\nINVALID RESPONSE:\n"
                    + invalid_content
                ),
            },
        ]
        response = await call_policy(policy, repair_messages, [])
        payload = parse_json_object(response, role="Lead report composer repair")
    required_fields = {"report_markdown"}
    missing_fields = required_fields - set(payload)
    if missing_fields:
        raise ValueError(
            "Lead report composer is missing required fields: "
            + ", ".join(sorted(missing_fields))
        )
    markdown = payload["report_markdown"]
    unresolved = payload.get("unresolved", [])
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("Lead report composer returned an empty report")
    if not isinstance(unresolved, list) or not all(isinstance(item, str) for item in unresolved):
        raise ValueError("Lead report unresolved must be a string list")
    known_ids = {
        item.evidence_id for result in outcome.worker_results for item in result.evidence
    }
    known_urls = {
        item.source_ref for result in outcome.worker_results for item in result.evidence
    }
    used_ids = tuple(dict.fromkeys(EVIDENCE_MARKER.findall(markdown)))
    unknown = set(used_ids) - known_ids
    unknown_urls = set(_URL.findall(markdown)) - known_urls
    inventory_repair_applied = False
    if unknown or unknown_urls:
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Repair the report's evidence inventory without tools. Preserve the "
                    "supported analysis, but replace or remove every unknown Evidence marker "
                    "and URL. Use only the supplied allowed Evidence IDs and URLs; do not add "
                    "facts. Return exactly one JSON object with report_markdown and unresolved."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "composer_evidence_package": json.loads(messages[-1]["content"]),
                        "invalid_report_markdown": markdown,
                        "unresolved": unresolved,
                        "unknown_evidence_ids": sorted(unknown),
                        "unknown_urls": sorted(unknown_urls),
                        "allowed_evidence_ids": sorted(known_ids),
                        "allowed_urls": sorted(known_urls),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            repaired_response = await call_policy(policy, repair_messages, [])
            repaired_payload = parse_json_object(
                repaired_response, role="Lead report evidence inventory repair"
            )
            repaired_markdown = repaired_payload.get("report_markdown")
            repaired_unresolved = repaired_payload.get("unresolved", [])
            if (
                isinstance(repaired_markdown, str)
                and repaired_markdown.strip()
                and isinstance(repaired_unresolved, list)
                and all(isinstance(item, str) for item in repaired_unresolved)
                and not (set(EVIDENCE_MARKER.findall(repaired_markdown)) - known_ids)
                and not (set(_URL.findall(repaired_markdown)) - known_urls)
            ):
                markdown = repaired_markdown
                unresolved = repaired_unresolved
                inventory_repair_applied = True
        except Exception:
            pass
        used_ids = tuple(dict.fromkeys(EVIDENCE_MARKER.findall(markdown)))
        unknown = set(used_ids) - known_ids
        unknown_urls = set(_URL.findall(markdown)) - known_urls
    safety_repairs: list[str] = []
    if unknown:
        markdown = drop_unsafe_markdown_lines(
            markdown,
            {f"[[EVIDENCE:{item}]]" for item in unknown},
        )
        safety_repairs.append(
            "Removed draft lines containing unknown Evidence IDs: "
            + ", ".join(sorted(unknown))
        )
        used_ids = tuple(dict.fromkeys(EVIDENCE_MARKER.findall(markdown)))
    if unknown_urls:
        markdown = drop_unsafe_markdown_lines(markdown, unknown_urls)
        safety_repairs.append(
            "Removed draft lines containing unknown URLs: "
            + ", ".join(sorted(unknown_urls))
        )
        used_ids = tuple(dict.fromkeys(EVIDENCE_MARKER.findall(markdown)))
    markdown, removed_false_challenge_absence = _drop_false_challenge_absence(
        markdown,
        challenges,
    )
    if removed_false_challenge_absence:
        safety_repairs.append(
            "Removed draft statement contradicting supplied Red challenges."
        )
    if not markdown.strip():
        markdown = (
            "# Research result\n\n"
            "All drafted findings were removed because they contained unknown "
            "evidence references or URLs.\n"
        )
    blocked_claims = {
        claim.claim_id: claim.claim
        for result in outcome.worker_results
        for claim in result.claims
        if claim.claim_id in blocked_claim_ids
    }
    leaked = tuple(
        claim_id
        for claim_id, claim_text in blocked_claims.items()
        if claim_text and claim_text in markdown
    )
    if leaked:
        raise ValueError(
            f"Lead report restates unresolved challenged Claims: {sorted(leaked)}"
        )
    return ReportDraft(
        plan_id=plan.plan_id,
        markdown=markdown,
        status=(
            "drafted_repaired"
            if inventory_repair_applied or safety_repairs
            else "drafted"
        ),
        evidence_ids=used_ids,
        unresolved=tuple(dict.fromkeys((
            *(item.strip() for item in unresolved if item.strip()),
            *outcome.unresolved_question_ids,
            *challenge_unresolved,
            *safety_repairs,
        ))),
        output_status=(
            OutputStatus.REPAIRED
            if inventory_repair_applied or safety_repairs
            else OutputStatus.VALID
        ),
    )

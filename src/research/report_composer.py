"""Evidence-only Lead report composition for Research Agent V2.

The separate final-report node boundary follows LangChain Deep Research From
Scratch at commit ``93f35e5d2a51590f9542207a9ff66a01901da5bc``. The refusal
to write without usable context is informed by GPT Researcher at commit
``6f998324006fd8e30d6e98e8815641da158d583c``. See third-party notices.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from .claim_hygiene import (
    normalize_report_text,
    reportable_claim_text,
    safe_disclosure_text,
)
from .models import OutputStatus
from .evidence_claim_pipeline import claim_has_verified_lineage
from .policy import call_policy
from .report_review import parse_json_object
from .v2_contracts import (
    EvidenceClaim,
    ReportAssertion,
    ReportDraft,
    ReportSection,
    ResearchChallenge,
    research_challenge_blocks_claim,
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
        r"\b(?:no|without)\s+(?:(?:accepted|deferred|pending|unresolved|red[- ]?team)\s+)*"
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
    selected: list[EvidenceClaim] = []
    for result in outcome.worker_results:
        for claim in result.claims:
            if (
                claim.claim_id in blocked
                or not claim.evidence_ids
                or not all(item in evidence for item in claim.evidence_ids)
                or not claim.source_ref
                or not claim.locator
                or not claim.excerpt
            ):
                continue
            clean = reportable_claim_text(claim.claim)
            if clean is None:
                continue
            if clean == claim.claim.strip():
                selected.append(claim)
            else:
                selected.append(EvidenceClaim.create(
                    claim=clean,
                    question_ids=claim.question_ids,
                    evidence_ids=claim.evidence_ids,
                    source_ref=claim.source_ref,
                    locator=claim.locator,
                    excerpt=claim.excerpt,
                    limitations=claim.limitations,
                    confidence=claim.confidence,
                    comparability_notes=claim.comparability_notes,
                    requirement_ids=claim.requirement_ids,
                    passage_ids=claim.passage_ids,
                    support_assessment_ids=claim.support_assessment_ids,
                    verification_status=claim.verification_status,
                ))
    return tuple(selected)


def _bounded_claims(
    plan: ResearchPlan,
    claims: tuple[EvidenceClaim, ...],
    *,
    per_question: int = 8,
    total_limit: int | None = None,
    claim_context_char_budget: int = 18000,
) -> tuple[EvidenceClaim, ...]:
    """Pack Agent-ranked Claims into a context budget, not a tiny fixed count."""
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    claim_order = {item.claim_id: index for index, item in enumerate(claims)}

    def rank(item: EvidenceClaim) -> tuple[Any, ...]:
        return (
            confidence_rank.get(item.confidence, 3),
            bool(item.limitations),
            not bool(item.comparability_notes),
            claim_order[item.claim_id],
        )

    required_requirements = tuple(
        item for item in plan.evidence_requirements if item.required
    ) or tuple(plan.evidence_requirements)
    dynamic_limit = min(40, max(24, len(required_requirements) * 8))
    total_limit = max(
        dynamic_limit if total_limit is None else total_limit,
        len(required_requirements),
    )
    selected: list[EvidenceClaim] = []
    selected_ids: set[str] = set()
    selected_chars = 0
    known_question_ids = {item.question_id for item in plan.core_questions}
    candidates_by_requirement = {
        requirement.requirement_id: sorted(
            (
                item
                for item in claims
                if requirement.requirement_id in item.requirement_ids
                or (
                    requirement.question_id in item.question_ids
                    and set(item.requirement_ids) <= set(item.question_ids)
                )
            ),
            key=rank,
        )
        for requirement in required_requirements
    }
    def add(item: EvidenceClaim, *, required_slot: bool = False) -> bool:
        nonlocal selected_chars
        if item.claim_id in selected_ids:
            return False
        estimated_chars = sum((
            len(item.claim),
            len(item.locator),
            len(item.limitations),
            len(item.comparability_notes),
            240,
        ))
        if (
            selected
            and not required_slot
            and selected_chars + estimated_chars > claim_context_char_budget
        ):
            return False
        selected.append(item)
        selected_ids.add(item.claim_id)
        selected_chars += estimated_chars
        return True

    # Round-robin guarantees one Claim per proof obligation before adding more.
    for position in range(per_question):
        for requirement in required_requirements:
            candidates = candidates_by_requirement[requirement.requirement_id]
            if position >= len(candidates):
                continue
            item = candidates[position]
            add(item, required_slot=(position == 0))
            if len(selected) >= total_limit:
                return tuple(selected)
    for item in sorted(
        (claim for claim in claims if set(claim.question_ids) & known_question_ids),
        key=rank,
    ):
        add(item)
        if len(selected) >= total_limit:
            break
    return tuple(selected)


def _drop_false_challenge_assertions(
    sections: tuple[ReportSection, ...],
    challenges: tuple[ResearchChallenge, ...],
) -> tuple[tuple[ReportSection, ...], bool]:
    if not challenges:
        return sections, False
    removed = False
    filtered: list[ReportSection] = []
    for section in sections:
        assertions = tuple(
            item
            for item in section.assertions
            if not any(pattern.search(item.text) for pattern in _FALSE_CHALLENGE_ABSENCE)
        )
        removed = removed or len(assertions) != len(section.assertions)
        if assertions:
            filtered.append(ReportSection(section.heading, assertions))
    return tuple(filtered), removed


def _deterministic_sections(
    plan: ResearchPlan,
    claims: tuple[EvidenceClaim, ...],
) -> tuple[ReportSection, ...]:
    """Build a plan-shaped report from already-validated immutable Claims."""

    clean_claims = tuple(
        (claim, clean)
        for claim in claims
        if (clean := reportable_claim_text(claim.claim)) is not None
    )
    limitations = tuple(
        ReportAssertion(text=clean, claim_ids=(claim.claim_id,))
        for claim in claims
        if claim.limitations
        and (clean := reportable_claim_text(claim.limitations, limit=500)) is not None
    )
    sections: list[ReportSection] = []
    assigned_claim_ids: set[str] = set()
    for index, question in enumerate(plan.core_questions):
        heading = (
            plan.report_outline[index]
            if index < len(plan.report_outline)
            else question.description
        )
        assertions = tuple(
            ReportAssertion(text=clean, claim_ids=(claim.claim_id,))
            for claim, clean in clean_claims
            if question.question_id in claim.question_ids
            and claim.claim_id not in assigned_claim_ids
        )
        assigned_claim_ids.update(
            claim_id
            for assertion in assertions
            for claim_id in assertion.claim_ids
        )
        if assertions:
            sections.append(ReportSection(heading, assertions))
        elif question.required:
            disclosure = (
                "未形成经验证的综合结论；本节仅披露现有证据边界。"
                if not question.requires_external_evidence
                else "该部分缺少可报告的已验证 Claim，保留为未解决证据缺口。"
            )
            sections.append(ReportSection(
                heading,
                (ReportAssertion(text=disclosure, claim_ids=()),),
            ))

    remaining = tuple(
        ReportAssertion(text=clean, claim_ids=(claim.claim_id,))
        for claim, clean in clean_claims
        if claim.claim_id not in assigned_claim_ids
    )
    if remaining:
        sections.append(ReportSection("其他已验证发现", remaining))
    if limitations:
        sections.append(ReportSection("证据限制", limitations))
    return tuple(sections)


def render_structured_report(
    sections: tuple[ReportSection, ...],
    claims: tuple[EvidenceClaim, ...],
) -> tuple[str, tuple[str, ...]]:
    """Render citations deterministically from Assertion-to-Claim lineage."""

    claim_by_id = {item.claim_id: item for item in claims}
    used_evidence: list[str] = []
    lines = ["# Research result"]
    for section in sections:
        lines.extend(("", f"## {section.heading}", ""))
        for assertion in section.assertions:
            evidence_ids = tuple(dict.fromkeys(
                evidence_id
                for claim_id in assertion.claim_ids
                for evidence_id in claim_by_id[claim_id].evidence_ids
            ))
            used_evidence.extend(evidence_ids)
            markers = " ".join(
                f"[[EVIDENCE:{evidence_id}]]" for evidence_id in evidence_ids
            )
            lines.append(f"- {assertion.text} {markers}".rstrip())
    return "\n".join(lines).strip(), tuple(dict.fromkeys(used_evidence))


def _parse_structured_composition(
    payload: dict[str, Any],
    claims: tuple[EvidenceClaim, ...],
    required_question_ids: tuple[str, ...],
    required_requirement_ids: tuple[str, ...] = (),
) -> tuple[tuple[ReportSection, ...], list[str]]:
    """Validate the model's structure before any Markdown is produced."""

    raw_sections = payload.get("sections")
    raw_unresolved = payload.get("unresolved", [])
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValueError("Lead report composer requires a non-empty sections list")
    if not isinstance(raw_unresolved, list):
        raise ValueError("Lead report unresolved must be a string list")
    known_claim_ids = {item.claim_id for item in claims}
    claim_by_id = {item.claim_id: item for item in claims}
    sections: list[ReportSection] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict) or set(raw_section) != {"heading", "assertions"}:
            raise ValueError("each report section requires heading and assertions")
        heading = normalize_report_text(raw_section["heading"], limit=100).lstrip("# ")
        assertions = raw_section["assertions"]
        if not heading or not isinstance(assertions, list):
            raise ValueError("report section heading/assertions are invalid")
        parsed: list[ReportAssertion] = []
        for raw_assertion in assertions:
            if not isinstance(raw_assertion, dict) or set(raw_assertion) != {"text", "claim_ids"}:
                raise ValueError("each report assertion requires text and claim_ids")
            original = str(raw_assertion["text"] or "").strip()
            text = reportable_claim_text(original)
            if text is None or text != normalize_report_text(original):
                raise ValueError("report assertion contains raw or non-reportable content")
            claim_ids_value = raw_assertion["claim_ids"]
            if not isinstance(claim_ids_value, list) or not claim_ids_value:
                raise ValueError("report assertion requires Claim lineage")
            claim_ids = tuple(dict.fromkeys(str(item) for item in claim_ids_value))
            if set(claim_ids) - known_claim_ids:
                raise ValueError("report assertion references unknown Claim IDs")
            key = (text, claim_ids)
            if key not in seen:
                parsed.append(ReportAssertion(text, claim_ids))
                seen.add(key)
        if parsed:
            sections.append(ReportSection(heading, tuple(parsed)))
    if not sections:
        raise ValueError("Lead report composer returned no reportable assertions")
    represented_claim_ids = {
        claim_id
        for section in sections
        for assertion in section.assertions
        for claim_id in assertion.claim_ids
    }
    represented_questions = {
        question_id
        for claim_id in represented_claim_ids
        for question_id in claim_by_id[claim_id].question_ids
    }
    missing_questions = set(required_question_ids) - represented_questions
    if missing_questions:
        raise ValueError(
            "Lead report composer omitted selected required questions: "
            + ", ".join(sorted(missing_questions))
        )
    represented_requirements = {
        requirement_id
        for claim_id in represented_claim_ids
        for requirement_id in claim_by_id[claim_id].requirement_ids
    }
    missing_requirements = set(required_requirement_ids) - represented_requirements
    if missing_requirements:
        raise ValueError(
            "Lead report composer omitted supported Evidence Requirements: "
            + ", ".join(sorted(missing_requirements))
        )
    unresolved = [
        clean
        for item in raw_unresolved
        if isinstance(item, str)
        and (clean := safe_disclosure_text(item, limit=400))
    ]
    return tuple(sections), unresolved


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
    payload = {
        "plan": {
            "plan_id": plan.plan_id,
            "core_questions": [
                {
                    "question_id": item.question_id,
                    "description": item.description[:800],
                    "required": item.required,
                    "priority": item.priority,
                    "requires_external_evidence": item.requires_external_evidence,
                }
                for item in plan.core_questions
            ],
            "evidence_requirements": [
                asdict(item) for item in plan.evidence_requirements
            ],
            "report_outline": [item[:500] for item in plan.report_outline[:12]],
        },
        "selected_claims": [
            {
                "claim_id": item.claim_id,
                "claim": reportable_claim_text(item.claim),
                "question_ids": list(item.question_ids),
                "requirement_ids": list(item.requirement_ids),
                "source": {
                    "title": evidence_by_id[item.evidence_ids[0]].title[:300],
                    "source_type": evidence_by_id[item.evidence_ids[0]].source_type,
                    "locator": evidence_by_id[item.evidence_ids[0]].locator[:300],
                },
                "limitations": safe_disclosure_text(item.limitations, limit=400),
                "confidence": item.confidence,
                "comparability_notes": safe_disclosure_text(
                    item.comparability_notes, limit=400
                ),
            }
            for item in claims
        ],
        "unresolved_question_ids": list(outcome.unresolved_question_ids),
        "requirement_coverage": [
            {
                "requirement_id": item.requirement_id,
                "question_id": item.question_id,
                "status": item.status,
                "reason": item.reason,
            }
            for item in outcome.requirement_coverage
        ],
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
                "blocks_claim": research_challenge_blocks_claim(item),
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
context. Return structured sections, not Markdown. Each assertion must contain
plain report prose plus one or more exact claim_ids from selected_claims. Never
write Evidence markers, URLs, Markdown links, images, HTML, navigation text, or
raw source excerpts; the application renders citations from claim_ids. If the
Claims do not support a statement, omit it or put a concise note in unresolved.
Core Questions with requires_external_evidence=false are synthesis requirements:
do not create standalone evidence for them. Integrate already verified Claims
into the corresponding report_outline section, use report_outline headings
verbatim, cite every material conclusion through claim_ids, and disclose gaps.
An accepted, deferred, pending, or unresolved_disclosed challenge with
blocks_claim=true is a hard exclusion: do not use its targeted Claim. For
blocks_claim=false, you may state only the narrow verified Claim, must not make
the challenged comparison or inference, and must preserve its limitation. If research_challenges is not
empty, never claim that no challenge information exists; the workflow will
append the authoritative Red-team issue inventory after your draft.
Return exactly {"sections":[{"heading":"...","assertions":[{"text":"...",
"claim_ids":["claim-id"]}]}],"unresolved":["..."]}. Synthesize a useful
answer, keep every factual assertion traceable to the selected Claim IDs, and
label unsupported extrapolation as inference or unresolved.""",
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
        if research_challenge_blocks_claim(challenge)
        for claim_id in challenge.target_claim_ids
    }
    reportable_claims = _selected_claims(
        outcome,
        blocked_claim_ids,
    )
    unblocked_claim_count = sum(
        claim.claim_id not in blocked_claim_ids
        for result in outcome.worker_results
        for claim in result.claims
    )
    quarantined_claim_count = max(0, unblocked_claim_count - len(reportable_claims))
    claims = _bounded_claims(plan, reportable_claims)
    required_question_ids = tuple(
        item.question_id
        for item in plan.core_questions
        if item.required and item.requires_external_evidence
    ) or tuple(
        item.question_id
        for item in plan.core_questions
        if item.requires_external_evidence
    )
    synthesis_questions = tuple(
        item
        for item in plan.core_questions
        if item.required and not item.requires_external_evidence
    )
    reportable_question_ids = {
        question_id for claim in reportable_claims for question_id in claim.question_ids
    }
    uncovered_question_ids = tuple(
        item for item in required_question_ids if item not in reportable_question_ids
    )
    selected_question_ids = tuple(
        item
        for item in required_question_ids
        if any(item in claim.question_ids for claim in claims)
    )
    selected_requirement_ids = tuple(
        item.requirement_id
        for item in plan.evidence_requirements
        if item.required
        and any(item.requirement_id in claim.requirement_ids for claim in claims)
    )
    challenge_unresolved = tuple(
        safe_disclosure_text(
            f"Red challenge {item.challenge_id} ({item.category}, {item.severity}): {item.reason}",
            limit=400,
        )
        for item in unresolved_challenges
    )
    if not claims:
        unresolved = tuple(dict.fromkeys((
            "No source-locatable evidence was available; report generation abstained.",
            *outcome.unresolved_question_ids,
            *uncovered_question_ids,
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
            sections=(),
            quarantined_claim_count=quarantined_claim_count,
            uncovered_question_ids=uncovered_question_ids,
            incomplete_synthesis_requirement_ids=tuple(
                item.question_id for item in synthesis_questions
            ),
        )
    messages = _composer_prompt(plan, outcome, claims, challenges, adjudications)
    response = await call_policy(policy, messages, [])
    composition_repair_applied = False
    composition_fallback_applied = False
    try:
        payload = parse_json_object(response, role="Lead report composer")
        sections, unresolved = _parse_structured_composition(
            payload, claims, selected_question_ids, selected_requirement_ids
        )
    except ValueError:
        invalid_content = str(response.get("content") or "")[:16000]
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Repair the Lead report composer response. Return exactly one valid "
                    "JSON object with sections and unresolved. Each section has heading and "
                    "assertions; each assertion has plain text and selected claim_ids. Do "
                    "not add facts, Evidence markers, URLs, links, HTML, or commentary."
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
        try:
            payload = parse_json_object(response, role="Lead report composer repair")
            sections, unresolved = _parse_structured_composition(
                payload, claims, selected_question_ids, selected_requirement_ids
            )
            composition_repair_applied = True
        except ValueError:
            sections = _deterministic_sections(plan, claims)
            unresolved = [
                "Lead report composer returned invalid structured output twice; used a "
                "deterministic Evidence Claim report."
            ]
            composition_fallback_applied = True
    safety_repairs: list[str] = []
    if composition_repair_applied:
        safety_repairs.append("Repaired malformed Lead composer structure.")
    if composition_fallback_applied:
        safety_repairs.append(
            "Replaced invalid Lead composer output with a deterministic "
            "Evidence Claim report."
        )
    sections, removed_false_challenge_absence = _drop_false_challenge_assertions(
        sections,
        challenges,
    )
    if removed_false_challenge_absence:
        safety_repairs.append(
            "Removed draft statement contradicting supplied Red challenges."
        )
    if not sections:
        sections = _deterministic_sections(plan, claims)
        composition_fallback_applied = True
        safety_repairs.append("Replaced an empty structured draft with validated Claims.")
    markdown, used_ids = render_structured_report(sections, claims)
    synthesis_targets = (
        tuple(plan.report_outline[-len(synthesis_questions):])
        if synthesis_questions
        else ()
    )
    normalized_headings = {
        " ".join(section.heading.casefold().split()) for section in sections
    }
    incomplete_synthesis = tuple(
        question.question_id
        for question, target in zip(synthesis_questions, synthesis_targets)
        if " ".join(target.casefold().split()) not in normalized_headings
    )
    if len(synthesis_targets) < len(synthesis_questions):
        incomplete_synthesis = tuple(dict.fromkeys((
            *incomplete_synthesis,
            *(item.question_id for item in synthesis_questions[len(synthesis_targets):]),
        )))
    return ReportDraft(
        plan_id=plan.plan_id,
        markdown=markdown,
        status=(
            "drafted_repaired"
            if safety_repairs
            else "drafted"
        ),
        evidence_ids=used_ids,
        unresolved=tuple(dict.fromkeys((
            *(item.strip() for item in unresolved if item.strip()),
            *outcome.unresolved_question_ids,
            *uncovered_question_ids,
            *(
                f"Synthesis requirement {item} lacks its report structure."
                for item in incomplete_synthesis
            ),
            *challenge_unresolved,
            *safety_repairs,
        ))),
        output_status=(
            OutputStatus.REPAIRED
            if safety_repairs
            else OutputStatus.VALID
        ),
        sections=sections,
        quarantined_claim_count=quarantined_claim_count,
        uncovered_question_ids=uncovered_question_ids,
        incomplete_synthesis_requirement_ids=incomplete_synthesis,
    )

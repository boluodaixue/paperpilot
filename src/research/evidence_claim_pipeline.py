"""Question-bound Passage extraction and pre-Composer Claim verification."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, Iterable
from urllib.parse import urlparse

from .claim_hygiene import normalize_report_text, reportable_claim_text
from .models import EvidenceItem
from .policy import call_policy
from .report_review import parse_json_object
from .v2_contracts import (
    CandidateClaim,
    EvidenceClaim,
    EvidencePassage,
    EvidenceRequirement,
    ResearchPlan,
    SourceDocument,
    SupportAssessment,
    WorkPacket,
)


_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？])|(?<=[.!?])\s+|[\r\n]+")
_NAVIGATION = re.compile(
    r"\b(?:cookie|privacy policy|terms of use|sign in|subscribe|navigation|"
    r"all rights reserved|orcid|download pdf|share this|last updated|"
    r"page was last updated)\b",
    re.IGNORECASE,
)
_FACT_SIGNAL = re.compile(
    r"\b(?:reported|found|showed|requires?|required|approved|authori[sz]ed|"
    r"increased|decreased|compared|measured|identified|provides?|states?|"
    r"applies?|permits?|prohibits?|includes?|means|defines?|achieved)\b|"
    r"(?:报告|显示|发现|要求|批准|增加|降低|相比|测得|识别|规定|指出|适用|包括|禁止|允许)",
    re.IGNORECASE,
)
_MARKDOWN_LINK_LINE = re.compile(r"^\s*(?:[-*+]\s+)?\[[^\]]+\]\([^)]*\)\s*$")


def source_authority_tier(item: EvidenceItem) -> str:
    """Classify authority without treating every .org page as primary."""

    host = (urlparse(item.source_ref).hostname or "").casefold()
    source_type = item.source_type.casefold()
    title = item.title.casefold()
    if source_type in {"official", "dataset"} or host.endswith((
        ".gov", ".gov.cn", ".europa.eu", ".int", "icmagroup.org",
        "ifc.org", "nafmii.org.cn",
    )):
        return "primary"
    if source_type == "paper" and not re.search(
        r"\b(?:systematic review|review article|narrative review|meta-analysis)\b",
        title,
    ):
        return "primary"
    if source_type == "paper" or host.endswith((
        ".edu", ".edu.cn", ".ac.uk"
    )):
        return "institutional"
    if any(token in host or token in title for token in (
        "medium.com", "substack.com", "blog", "forum", "answers"
    )):
        return "weak"
    return "secondary"


def _context_terms(value: str) -> set[str]:
    terms = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]{2,}", value)
    }
    for run in re.findall(r"[\u3400-\u9fff]{2,}", value):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def _best_exact_sentences(
    text: str,
    context: str,
    *,
    limit: int = 2,
) -> tuple[tuple[str, str], ...]:
    """Select multiple atomic Passage sentences without generated summaries."""

    anchors = _context_terms(context)
    candidates: list[tuple[float, int, str, str]] = []
    for position, fragment in enumerate(_SENTENCE_BOUNDARY.split(str(text or ""))):
        exact = fragment.strip()
        if not exact or exact.startswith(("|---", "---")):
            continue
        if exact.startswith("|") or "\ufffd" in exact:
            continue
        if _MARKDOWN_LINK_LINE.fullmatch(exact) or exact.startswith("[["):
            continue
        if re.match(r"^[a-z]", exact):
            continue
        if re.match(r"^(?:and|or|but|require|requires|falling|depending)\b", exact):
            continue
        if _NAVIGATION.search(exact):
            continue
        if len(exact) > 900:
            exact = exact[:900].rstrip()
        clean = normalize_report_text(exact, limit=700)
        if len(clean) < 20 or reportable_claim_text(clean, limit=700) is None:
            continue
        terms = _context_terms(clean)
        score = 4.0 * len(terms & anchors)
        score += 2.0 if re.search(r"\d", clean) else 0.0
        score += 2.0 if _FACT_SIGNAL.search(clean) else 0.0
        score += min(len(clean), 240) / 240.0
        score -= 3.0 if clean.endswith((":", "：")) else 0.0
        sentence_terminal = clean.endswith((".", "。", "!", "！", "?", "？"))
        factual_shape = bool(
            _FACT_SIGNAL.search(clean)
            or (
                sentence_terminal
                and len(re.findall(r"\w+", clean, flags=re.UNICODE)) >= 6
            )
        )
        if not factual_shape:
            continue
        candidates.append((score, -position, clean, exact))
    if not candidates:
        return ()
    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for score, _, claim_text, exact_quote in sorted(candidates, reverse=True):
        if score <= 0 or claim_text in seen:
            continue
        selected.append((claim_text, exact_quote))
        seen.add(claim_text)
        if len(selected) >= max(1, limit):
            break
    return tuple(selected)


def extract_candidate_claims(
    packet: WorkPacket,
    plan: ResearchPlan,
    evidence: Iterable[EvidenceItem],
) -> tuple[
    tuple[SourceDocument, ...],
    tuple[EvidencePassage, ...],
    tuple[CandidateClaim, ...],
]:
    """Build document/passages and question-bound extractive candidates."""

    questions = {item.question_id: item for item in plan.core_questions}
    requirements_by_id = {
        item.requirement_id: item for item in plan.evidence_requirements
    }
    default_requirement_by_question = {
        item.question_id: item
        for item in plan.evidence_requirements
    }
    documents_by_source: dict[str, SourceDocument] = {}
    passages: list[EvidencePassage] = []
    candidates: list[CandidateClaim] = []
    candidate_counts: dict[str, int] = {}
    question_count = max(1, len(packet.question_ids))
    candidate_limit = min(40, max(12, question_count * 8))
    candidate_quota = max(2, candidate_limit // question_count)
    extractable: list[tuple[Any, Any, EvidencePassage, tuple[tuple[str, str], ...]]] = []
    for item in evidence:
        requirement = requirements_by_id.get(item.requirement_id)
        if requirement is None and item.requirement_id in packet.question_ids:
            requirement = default_requirement_by_question.get(item.requirement_id)
        if requirement is None or requirement.question_id not in packet.question_ids:
            continue
        document = documents_by_source.get(item.source_ref)
        if document is None:
            document = SourceDocument.create(
                item.source_ref,
                item.title,
                item.source_type,
                source_authority_tier(item),
            )
            documents_by_source[item.source_ref] = document
        passage = EvidencePassage.create(
            document.document_id,
            item.evidence_id,
            item.requirement_id,
            item.excerpt,
            item.locator,
        )
        passages.append(passage)
        question = questions[requirement.question_id]
        extracted = _best_exact_sentences(
            passage.exact_text,
            " ".join((packet.objective, question.description, *packet.source_guidance)),
        )
        if extracted:
            extractable.append((requirement, question, passage, extracted))

    # Give every Agent-ranked Evidence item one opportunity before taking a
    # second Claim from any item. This preserves source breadth and prevents
    # the first long page from exhausting a Requirement's Candidate capacity.
    for position in range(2):
        for requirement, question, passage, extracted in extractable:
            if position >= len(extracted):
                continue
            if len(candidates) >= candidate_limit:
                break
            if candidate_counts.get(requirement.requirement_id, 0) >= candidate_quota:
                continue
            claim_text, exact_quote = extracted[position]
            candidates.append(CandidateClaim.create(
                claim_text,
                (question.question_id,),
                (requirement.requirement_id,),
                (passage.passage_id,),
                exact_quote,
            ))
            candidate_counts[requirement.requirement_id] = (
                candidate_counts.get(requirement.requirement_id, 0) + 1
            )
        if len(candidates) >= candidate_limit:
            break
    return tuple(documents_by_source.values()), tuple(passages), tuple(candidates)


def _deterministic_assessment(
    candidate: CandidateClaim,
    passage: EvidencePassage,
    requirements: Iterable[EvidenceRequirement] = (),
) -> SupportAssessment:
    quote_in_passage = candidate.exact_quote in passage.exact_text
    exact_match = normalize_report_text(candidate.exact_quote, limit=700) == candidate.text
    requirement_by_id = {item.requirement_id: item for item in requirements}
    requirement_text = " ".join(
        requirement_by_id[item].description
        for item in candidate.requirement_ids
        if item in requirement_by_id
    )
    relevance_known = (
        not requirement_text
        or bool(_context_terms(candidate.text) & _context_terms(requirement_text))
    )
    verdict = (
        "entailed"
        if quote_in_passage and exact_match and relevance_known
        else "partially_entailed"
    )
    return SupportAssessment.create(
        candidate.candidate_id,
        candidate.passage_ids,
        verdict,
        1.0 if verdict == "entailed" else 0.5,
        supported_scope=(candidate.text if verdict == "entailed" else ""),
        unsupported_scope=("Candidate is not an exact extractive assertion." if verdict != "entailed" else ""),
        reason="Deterministic exact-quote verification fallback.",
        method="deterministic_exact_quote",
    )


def deterministic_verify_candidate_claims(
    candidates: Iterable[CandidateClaim],
    passages: Iterable[EvidencePassage],
    requirements: Iterable[EvidenceRequirement] = (),
) -> tuple[SupportAssessment, ...]:
    passage_by_id = {item.passage_id: item for item in passages}
    requirement_items = tuple(requirements)
    return tuple(
        _deterministic_assessment(
            item,
            passage_by_id[item.passage_ids[0]],
            requirement_items,
        )
        for item in candidates
    )


def _verification_prompt(
    candidates: tuple[CandidateClaim, ...],
    passages: tuple[EvidencePassage, ...],
    requirements: tuple[EvidenceRequirement, ...],
) -> list[dict[str, str]]:
    passage_by_id = {item.passage_id: item for item in passages}
    requirement_by_id = {item.requirement_id: item for item in requirements}
    used_requirement_ids = tuple(dict.fromkeys(
        requirement_id
        for item in candidates
        for requirement_id in item.requirement_ids
        if requirement_id in requirement_by_id
    ))
    return [
        {
            "role": "system",
            "content": """You are an independent evidence-support verifier. Do not
research, use tools, rewrite Claims, or rely on outside knowledge. For every
candidate decide whether the supplied exact Passage entails the complete Claim
and whether that Claim satisfies the supplied Evidence Requirement.
Return exactly {"assessments":[...]} with candidate_id, verdict, confidence,
supported_scope, unsupported_scope, reason. verdict must be entailed,
partially_entailed, contradicted, or irrelevant. Be strict about quantities,
denominators, dates, jurisdictions, causality, scope, source role, and relevance.
Use entailed when the Passage fully supports the complete atomic Claim and the
Claim materially contributes to the Requirement; one atomic Claim does not need
to answer the entire broad Requirement by itself. Use partially_entailed only
when the Passage supports only part of the Claim. Use irrelevant when the
quotation is real but does not contribute to the Requirement. confidence must
be a JSON number from 0.0 to 1.0, never a label such as high or medium.""",
        },
        {
            "role": "user",
            "content": json.dumps({
                "requirements": [
                    asdict(requirement_by_id[item])
                    for item in used_requirement_ids
                ],
                "candidates": [
                    {
                        "candidate_id": item.candidate_id,
                        "text": item.text,
                        "question_ids": list(item.question_ids),
                        "requirement_ids": list(item.requirement_ids),
                        "passage_ids": list(item.passage_ids),
                        "passage_quote": item.exact_quote,
                        "locator": passage_by_id[item.passage_ids[0]].locator,
                    }
                    for item in candidates
                ]
            }, ensure_ascii=False),
        },
    ]


_VERIFIER_INPUT_CHAR_BUDGET = 28000


def _verification_batches(
    candidates: tuple[CandidateClaim, ...],
    passages: tuple[EvidencePassage, ...],
    requirements: tuple[EvidenceRequirement, ...],
) -> tuple[tuple[CandidateClaim, ...], ...]:
    """Keep every Candidate while preventing transport-level truncation."""

    batches: list[tuple[CandidateClaim, ...]] = []
    current: list[CandidateClaim] = []
    for candidate in candidates:
        trial = (*current, candidate)
        messages = _verification_prompt(trial, passages, requirements)
        size = sum(len(str(item.get("content") or "")) for item in messages)
        if current and size > _VERIFIER_INPUT_CHAR_BUDGET:
            batches.append(tuple(current))
            current = [candidate]
        else:
            current.append(candidate)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _unverified_assessment(
    candidate: CandidateClaim,
    *,
    reason: str,
) -> SupportAssessment:
    return SupportAssessment.create(
        candidate.candidate_id,
        candidate.passage_ids,
        "irrelevant",
        0.0,
        unsupported_scope="Semantic support was not verified.",
        reason=reason,
        method="semantic_verifier_unavailable",
    )


def _agent_ranked_assessment(
    candidate: CandidateClaim,
    passage: EvidencePassage,
) -> SupportAssessment:
    """Trust Agent relevance only after deterministic quote-lineage checks."""

    exact = (
        candidate.exact_quote in passage.exact_text
        and normalize_report_text(candidate.exact_quote, limit=700) == candidate.text
    )
    return SupportAssessment.create(
        candidate.candidate_id,
        candidate.passage_ids,
        "entailed" if exact else "irrelevant",
        0.75 if exact else 0.0,
        supported_scope=(candidate.text if exact else ""),
        unsupported_scope=("" if exact else "Exact quote lineage failed."),
        reason=(
            "Agent-ranked Evidence with deterministic exact-quote lineage."
            if exact
            else "Agent-ranked Evidence failed exact-quote lineage."
        ),
        method="agent_ranked_exact_quote",
    )


def _confidence_value(value: Any) -> float:
    if isinstance(value, str):
        label = value.strip().casefold()
        if label in {"high", "strong"}:
            return 0.9
        if label in {"medium", "moderate"}:
            return 0.75
        if label in {"low", "weak"}:
            return 0.5
    return float(value)


async def verify_candidate_claims(
    policy: Any,
    candidates: Iterable[CandidateClaim],
    passages: Iterable[EvidencePassage],
    requirements: Iterable[EvidenceRequirement] = (),
    *,
    trusted_candidate_ids: Iterable[str] = (),
) -> tuple[SupportAssessment, ...]:
    """Run one tool-free independent verification pass with safe fallback."""

    candidate_items = tuple(candidates)
    passage_items = tuple(passages)
    requirement_items = tuple(requirements)
    trusted = set(trusted_candidate_ids)
    if not candidate_items:
        return ()
    passage_by_id = {item.passage_id: item for item in passage_items}
    assessments: dict[str, SupportAssessment] = {
        item.candidate_id: _agent_ranked_assessment(
            item,
            passage_by_id[item.passage_ids[0]],
        )
        for item in candidate_items
        if item.candidate_id in trusted
    }

    def unavailable(candidate: CandidateClaim, reason: str) -> SupportAssessment:
        if candidate.candidate_id in trusted:
            return _agent_ranked_assessment(
                candidate,
                passage_by_id[candidate.passage_ids[0]],
            )
        return _unverified_assessment(candidate, reason=reason)

    unranked_candidates = tuple(
        item for item in candidate_items if item.candidate_id not in trusted
    )
    for batch in _verification_batches(
        unranked_candidates, passage_items, requirement_items
    ):
        candidate_by_id = {item.candidate_id: item for item in batch}
        try:
            response = await call_policy(
                policy,
                _verification_prompt(batch, passage_items, requirement_items),
                [],
            )
            payload = parse_json_object(response, role="Evidence support verifier")
            raw = payload.get("assessments")
            if set(payload) != {"assessments"} or not isinstance(raw, list):
                raise ValueError("support verifier requires assessments")
            parsed: dict[str, SupportAssessment] = {}
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError("support assessment must be an object")
                candidate_id = str(item.get("candidate_id") or "").strip()
                candidate = candidate_by_id.get(candidate_id)
                if candidate is None or candidate_id in parsed:
                    raise ValueError(
                        "support verifier returned unknown/duplicate candidate"
                    )
                parsed[candidate_id] = SupportAssessment.create(
                    candidate_id,
                    candidate.passage_ids,
                    item.get("verdict"),
                    _confidence_value(item.get("confidence", 0.0)),
                    supported_scope=item.get("supported_scope", ""),
                    unsupported_scope=item.get("unsupported_scope", ""),
                    reason=item.get("reason", ""),
                )
            for candidate in batch:
                assessments[candidate.candidate_id] = parsed.get(
                    candidate.candidate_id,
                    unavailable(
                        candidate,
                        "Evidence support verifier omitted this Candidate.",
                    ),
                )
        except Exception as exc:
            for candidate in batch:
                assessments[candidate.candidate_id] = unavailable(
                    candidate,
                    (
                        "Evidence support verifier failed: "
                        + type(exc).__name__
                    ),
                )
    return tuple(assessments[item.candidate_id] for item in candidate_items)


def narrow_partially_entailed_candidates(
    candidates: Iterable[CandidateClaim],
    assessments: Iterable[SupportAssessment],
    passages: Iterable[EvidencePassage],
) -> tuple[CandidateClaim, ...]:
    """Create extractive narrower Candidates; callers must verify them again."""

    candidate_by_id = {item.candidate_id: item for item in candidates}
    passage_by_id = {item.passage_id: item for item in passages}
    narrowed: list[CandidateClaim] = []
    for assessment in assessments:
        if assessment.verdict != "partially_entailed":
            continue
        original = candidate_by_id.get(assessment.candidate_id)
        if original is None or not assessment.supported_scope:
            continue
        exact_scope = assessment.supported_scope.strip()
        passage = passage_by_id[original.passage_ids[0]]
        if exact_scope not in passage.exact_text:
            continue
        clean = reportable_claim_text(exact_scope, limit=700)
        if clean is None or clean == original.text:
            continue
        narrowed.append(CandidateClaim.create(
            clean,
            original.question_ids,
            original.requirement_ids,
            original.passage_ids,
            exact_scope,
        ))
    return tuple(narrowed)


def verified_evidence_claims(
    evidence: Iterable[EvidenceItem],
    documents: Iterable[SourceDocument],
    passages: Iterable[EvidencePassage],
    candidates: Iterable[CandidateClaim],
    assessments: Iterable[SupportAssessment],
    *,
    minimum_confidence: float = 0.7,
) -> tuple[EvidenceClaim, ...]:
    """Materialize report Claims only after a successful support decision."""

    evidence_by_id = {item.evidence_id: item for item in evidence}
    document_by_id = {item.document_id: item for item in documents}
    passage_by_id = {item.passage_id: item for item in passages}
    assessment_by_candidate = {item.candidate_id: item for item in assessments}
    claims: list[EvidenceClaim] = []
    for candidate in candidates:
        assessment = assessment_by_candidate.get(candidate.candidate_id)
        if (
            assessment is None
            or assessment.verdict != "entailed"
            or assessment.confidence < minimum_confidence
        ):
            continue
        passage = passage_by_id[candidate.passage_ids[0]]
        if candidate.exact_quote not in passage.exact_text:
            continue
        if reportable_claim_text(candidate.text, limit=700) is None:
            continue
        item = evidence_by_id[passage.evidence_id]
        document = document_by_id[passage.document_id]
        claims.append(EvidenceClaim.create(
            claim=candidate.text,
            question_ids=candidate.question_ids,
            evidence_ids=(passage.evidence_id,),
            source_ref=document.source_ref,
            locator=passage.locator,
            excerpt=candidate.exact_quote,
            limitations=item.limitations,
            confidence=("high" if assessment.confidence >= 0.9 else "medium"),
            requirement_ids=candidate.requirement_ids,
            passage_ids=candidate.passage_ids,
            support_assessment_ids=(assessment.assessment_id,),
            verification_status="verified",
        ))
    return tuple(claims)


def claim_has_verified_lineage(
    claim: EvidenceClaim,
    candidates: Iterable[CandidateClaim],
    assessments: Iterable[SupportAssessment],
    passages: Iterable[EvidencePassage],
    *,
    minimum_confidence: float = 0.7,
) -> bool:
    """Validate a materialized Claim against its immutable proof graph."""

    if claim.verification_status != "verified":
        return False
    candidate_by_id = {item.candidate_id: item for item in candidates}
    assessment_by_id = {item.assessment_id: item for item in assessments}
    passage_by_id = {item.passage_id: item for item in passages}
    if not claim.passage_ids or not claim.support_assessment_ids:
        return False
    for assessment_id in claim.support_assessment_ids:
        assessment = assessment_by_id.get(assessment_id)
        if (
            assessment is None
            or assessment.verdict != "entailed"
            or assessment.confidence < minimum_confidence
        ):
            return False
        candidate = candidate_by_id.get(assessment.candidate_id)
        if candidate is None or candidate.text != claim.claim:
            return False
        if not set(claim.passage_ids) <= set(candidate.passage_ids):
            return False
    for passage_id in claim.passage_ids:
        passage = passage_by_id.get(passage_id)
        if passage is None or passage.evidence_id not in claim.evidence_ids:
            return False
    return True


__all__ = [
    "extract_candidate_claims",
    "deterministic_verify_candidate_claims",
    "claim_has_verified_lineage",
    "narrow_partially_entailed_candidates",
    "source_authority_tier",
    "verified_evidence_claims",
    "verify_candidate_claims",
]

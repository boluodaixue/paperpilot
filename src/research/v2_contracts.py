"""Serializable contracts for the bounded Research Agent V2 architecture.

The V2 supervisor/worker boundary follows the graph shape described by
LangChain's ``deep_research_from_scratch`` project at commit
``93f35e5d2a51590f9542207a9ff66a01901da5bc``.  The implementation remains
PaperPilot-specific so its checkpoint, budget, Evidence, and Vault contracts
stay authoritative.  See ``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, Mapping

from .models import (
    EvidenceItem,
    OutputStatus,
    ResearchStatus,
    TerminationReason,
    ToolAvailabilityAlert,
)


class ResearchArchitecture(str, Enum):
    """Selectable post-confirmation research orchestration architecture."""

    LEGACY = "legacy"
    SUPERVISOR_V2 = "supervisor_v2"


@dataclass(frozen=True)
class SupervisorV2Config:
    """Strict, checkpoint-safe rollout bounds for the V2 graph."""

    enabled: bool = False
    max_initial_workers: int = 4
    max_research_waves: int = 3
    red_review_enabled: bool = True
    max_red_review_rounds: int = 1
    max_citation_repair_rounds: int = 1

    def validate(self) -> None:
        for key in ("enabled", "red_review_enabled"):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"research.supervisor_v2.{key} must be a boolean")

        integer_values = {
            "max_initial_workers": self.max_initial_workers,
            "max_research_waves": self.max_research_waves,
            "max_red_review_rounds": self.max_red_review_rounds,
            "max_citation_repair_rounds": self.max_citation_repair_rounds,
        }
        for key, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"research.supervisor_v2.{key} must be an integer")

        if self.max_initial_workers < 1:
            raise ValueError(
                "research.supervisor_v2.max_initial_workers must be at least 1"
            )
        if not 1 <= self.max_research_waves <= 3:
            raise ValueError(
                "research.supervisor_v2.max_research_waves must be between 1 and 3"
            )
        if not 0 <= self.max_red_review_rounds <= 1:
            raise ValueError(
                "research.supervisor_v2.max_red_review_rounds must be between 0 and 1"
            )
        if not 0 <= self.max_citation_repair_rounds <= 1:
            raise ValueError(
                "research.supervisor_v2.max_citation_repair_rounds must be between 0 and 1"
            )


@dataclass(frozen=True)
class ResearchArchitectureSettings:
    """Fully parsed architecture selection and its bounded V2 settings."""

    architecture: ResearchArchitecture = ResearchArchitecture.LEGACY
    supervisor_v2: SupervisorV2Config = SupervisorV2Config()


class ChallengeDecision(str, Enum):
    """Lead Researcher disposition for one Red challenge."""

    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


@dataclass(frozen=True)
class ChallengeAdjudication:
    """Structured Lead disposition for exactly one Red challenge."""

    challenge_id: str
    decision: ChallengeDecision
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"stable ID payload contains unsupported value: {type(value).__name__}")


def stable_content_id(prefix: str, payload: Any) -> str:
    """Return a short stable SHA-256 ID over canonical JSON content."""
    clean_prefix = str(prefix).strip().lower().replace("_", "-")
    if not clean_prefix or not clean_prefix.replace("-", "").isalnum():
        raise ValueError("stable ID prefix must be a non-empty identifier")
    encoded = json.dumps(
        _canonical_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{clean_prefix}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _clean_tuple(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


@dataclass(frozen=True)
class CoreQuestion:
    """One lightweight must-answer question, not a Cartesian coverage cell."""

    question_id: str
    description: str
    required: bool = True
    priority: str = "high"
    origin: str = "brief_direction"
    verification: str = "source-locatable evidence"

    @classmethod
    def create(
        cls,
        description: str,
        required: bool = True,
        priority: str = "high",
        origin: str = "brief_direction",
        verification: str = "source-locatable evidence",
    ) -> "CoreQuestion":
        description = str(description).strip()
        if not description:
            raise ValueError("CoreQuestion description cannot be empty")
        payload = {
            "description": description,
            "required": bool(required),
            "priority": str(priority).strip() or "medium",
            "origin": str(origin).strip() or "model",
            "verification": str(verification).strip() or "source-locatable evidence",
        }
        return cls(stable_content_id("question", payload), **payload)


@dataclass(frozen=True)
class ResearchPlan:
    """Stable, serializable plan produced before external research begins."""

    plan_id: str
    brief_revision: int
    core_questions: tuple[CoreQuestion, ...]
    report_outline: tuple[str, ...] = ()
    source_guidance: tuple[str, ...] = ()
    work_hints: tuple[str, ...] = ()
    fallback_reason: str | None = None

    @classmethod
    def create(
        cls,
        brief_revision: int,
        core_questions: tuple[CoreQuestion, ...],
        report_outline: tuple[str, ...] = (),
        source_guidance: tuple[str, ...] = (),
        work_hints: tuple[str, ...] = (),
        fallback_reason: str | None = None,
    ) -> "ResearchPlan":
        questions = tuple(core_questions)
        if not questions:
            raise ValueError("ResearchPlan requires at least one CoreQuestion")
        question_ids = [item.question_id for item in questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("ResearchPlan CoreQuestion IDs must be unique")
        payload = {
            "brief_revision": int(brief_revision),
            "core_questions": questions,
            "report_outline": _clean_tuple(report_outline),
            "source_guidance": _clean_tuple(source_guidance),
            "work_hints": _clean_tuple(work_hints),
            "fallback_reason": (
                str(fallback_reason).strip() if fallback_reason else None
            ),
        }
        return cls(stable_content_id("plan", payload), **payload)


@dataclass(frozen=True)
class WorkPacket:
    """Bounded one-wave assignment for a non-recursive Blue Worker."""

    packet_id: str
    objective: str
    question_ids: tuple[str, ...]
    expected_output: str
    source_guidance: tuple[str, ...]
    max_tool_calls: int
    token_budget: int
    deadline_at: float
    wave: str = "initial"

    @classmethod
    def create(
        cls,
        objective: str,
        question_ids: tuple[str, ...],
        expected_output: str,
        source_guidance: tuple[str, ...],
        max_tool_calls: int,
        token_budget: int,
        deadline_at: float,
        wave: str = "initial",
    ) -> "WorkPacket":
        payload = {
            "objective": str(objective).strip(),
            "question_ids": _clean_tuple(question_ids),
            "expected_output": str(expected_output).strip(),
            "source_guidance": _clean_tuple(source_guidance),
            "max_tool_calls": int(max_tool_calls),
            "token_budget": int(token_budget),
            "deadline_at": float(deadline_at),
            "wave": str(wave).strip().lower(),
        }
        if not payload["objective"] or not payload["question_ids"]:
            raise ValueError("WorkPacket requires an objective and question IDs")
        if payload["wave"] not in {"initial", "supplemental"}:
            raise ValueError("WorkPacket wave must be initial or supplemental")
        if payload["max_tool_calls"] < 0 or payload["token_budget"] < 0:
            raise ValueError("WorkPacket budgets cannot be negative")
        return cls(stable_content_id("packet", payload), **payload)


@dataclass(frozen=True)
class EvidenceClaim:
    """A report-usable claim mapped to one or more existing Evidence IDs."""

    claim_id: str
    claim: str
    question_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    source_ref: str
    locator: str
    excerpt: str
    limitations: str = ""
    confidence: str = "medium"
    comparability_notes: str = ""

    @classmethod
    def create(
        cls,
        claim: str,
        question_ids: tuple[str, ...],
        evidence_ids: tuple[str, ...],
        source_ref: str,
        locator: str,
        excerpt: str,
        limitations: str = "",
        confidence: str = "medium",
        comparability_notes: str = "",
    ) -> "EvidenceClaim":
        payload = {
            "claim": str(claim).strip(),
            "question_ids": _clean_tuple(question_ids),
            "evidence_ids": _clean_tuple(evidence_ids),
            "source_ref": str(source_ref).strip(),
            "locator": str(locator).strip(),
            "excerpt": str(excerpt).strip(),
            "limitations": str(limitations).strip(),
            "confidence": str(confidence).strip().lower() or "medium",
            "comparability_notes": str(comparability_notes).strip(),
        }
        if not all(
            (
                payload["claim"],
                payload["question_ids"],
                payload["evidence_ids"],
                payload["source_ref"],
                payload["locator"],
                payload["excerpt"],
            )
        ):
            raise ValueError("EvidenceClaim requires claim, IDs, source, locator, and excerpt")
        return cls(stable_content_id("claim", payload), **payload)


@dataclass(frozen=True)
class ResearchChallenge:
    """One allowlisted Red research challenge."""

    challenge_id: str
    category: str
    target_question_ids: tuple[str, ...]
    target_claim_ids: tuple[str, ...]
    reason: str
    severity: str
    requested_evidence: str = ""
    suggested_query: str = ""
    status: str = "pending"
    resolution_evidence_ids: tuple[str, ...] = ()
    resolution_reason: str = ""

    @classmethod
    def create(
        cls,
        category: str,
        target_question_ids: tuple[str, ...],
        target_claim_ids: tuple[str, ...],
        reason: str,
        severity: str,
        requested_evidence: str = "",
        suggested_query: str = "",
        status: str = "pending",
    ) -> "ResearchChallenge":
        payload = {
            "category": str(category).strip().lower(),
            "target_question_ids": _clean_tuple(target_question_ids),
            "target_claim_ids": _clean_tuple(target_claim_ids),
            "reason": str(reason).strip(),
            "severity": str(severity).strip().lower(),
            "requested_evidence": str(requested_evidence).strip(),
            "suggested_query": str(suggested_query).strip(),
            "status": str(status).strip().lower() or "pending",
        }
        if payload["category"] not in {
            "missing_question",
            "unsupported_claim",
            "weak_source",
            "conflict",
            "non_comparable",
            "uncertainty",
        }:
            raise ValueError("unknown ResearchChallenge category")
        if not payload["reason"]:
            raise ValueError("ResearchChallenge reason cannot be empty")
        return cls(stable_content_id("challenge", payload), **payload)


@dataclass(frozen=True)
class CitationIssue:
    """One deterministic or semantic citation-audit issue."""

    issue_id: str
    claim_text: str
    section: str
    evidence_ids: tuple[str, ...]
    category: str
    severity: str
    repair_action: str
    status: str = "pending"

    @classmethod
    def create(
        cls,
        claim_text: str,
        section: str,
        evidence_ids: tuple[str, ...],
        category: str,
        severity: str,
        repair_action: str,
        status: str = "pending",
    ) -> "CitationIssue":
        payload = {
            "claim_text": str(claim_text).strip(),
            "section": str(section).strip(),
            "evidence_ids": _clean_tuple(evidence_ids),
            "category": str(category).strip().lower(),
            "severity": str(severity).strip().lower(),
            "repair_action": str(repair_action).strip().lower(),
            "status": str(status).strip().lower() or "pending",
        }
        if payload["category"] not in {
            "missing",
            "invalid",
            "overclaim",
            "conflict",
            "locator",
        }:
            raise ValueError("unknown CitationIssue category")
        if not payload["claim_text"] or not payload["repair_action"]:
            raise ValueError("CitationIssue requires claim text and repair action")
        return cls(stable_content_id("citation-issue", payload), **payload)


@dataclass(frozen=True)
class CitationAuditOutcome:
    """Checkpointed result of deterministic and semantic citation checks."""

    status: str
    issues: tuple[CitationIssue, ...] = ()
    repaired_markdown: str | None = None
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportDraft:
    """Evidence-only Lead draft before deterministic citation rendering."""

    plan_id: str
    markdown: str
    status: str
    evidence_ids: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    output_status: OutputStatus = OutputStatus.VALID


@dataclass(frozen=True)
class BlueWorkerUsage:
    """Bounded usage returned to the Lead without importing message history."""

    iterations: int = 0
    tool_calls: int = 0
    estimated_tokens: int = 0
    retries: int = 0
    source_candidates: int = 0
    sources_opened: int = 0
    duplicate_sources: int = 0
    acquisition_calls: int = 0


@dataclass(frozen=True)
class BlueWorkerResult:
    """Only result shape returned by a one-layer Blue Worker."""

    packet_id: str
    status: ResearchStatus
    summary: str
    claims: tuple[EvidenceClaim, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    unresolved: tuple[str, ...] = ()
    alerts: tuple[ToolAvailabilityAlert, ...] = ()
    usage: BlueWorkerUsage = BlueWorkerUsage()
    termination_reason: TerminationReason | None = None
    output_status: OutputStatus = OutputStatus.VALID


@dataclass(frozen=True)
class ConductResearch:
    """Structured Supervisor command to execute one bounded packet wave."""

    packets: tuple[WorkPacket, ...]
    wave: str


@dataclass(frozen=True)
class ResearchComplete:
    """Structured Supervisor command to leave research for review/drafting."""

    reason: str
    termination_reason: TerminationReason | None = None


@dataclass(frozen=True)
class SupervisorOutcome:
    """Deterministically merged result of one bounded Supervisor run."""

    plan_id: str
    worker_results: tuple[BlueWorkerResult, ...]
    assigned_question_ids: tuple[str, ...]
    resolved_question_ids: tuple[str, ...]
    unresolved_question_ids: tuple[str, ...]
    wave_count: int
    finalization_token_reserve: int
    termination_reason: TerminationReason | None = None


@dataclass(frozen=True)
class ResearchChallengeLoopOutcome:
    """Checkpoint-safe output of the full Red/adjudication/supplement loop."""

    supervisor_outcome: SupervisorOutcome
    challenges: tuple[ResearchChallenge, ...] = ()
    adjudications: tuple[ChallengeAdjudication, ...] = ()
    quality_alerts: tuple[ToolAvailabilityAlert, ...] = ()
    supplemental_question_ids: tuple[str, ...] = ()
    supplemental_packet_ids: tuple[str, ...] = ()

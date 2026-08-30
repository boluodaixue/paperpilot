"""Contracts and deterministic validation for in-graph research sufficiency."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .models import (
    CriticalGap,
    EvidenceItem,
    NextResearchAction,
    OutputStatus,
    RequirementCoverage,
    RequirementStatus,
    ResearchDecision,
    ResearchRequirement,
    ResearchTask,
    StrategyAttempt,
    TerminationReason,
)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_EXPECTED_VALUES = {"high", "medium", "low"}
_IMPACTS = {"high", "medium", "low"}


class AssessmentValidationError(ValueError):
    """Raised when a model assessment cannot safely drive graph routing."""


@dataclass(frozen=True)
class ResearchAssessment:
    """Validated semantic assessment used by the deterministic router."""

    decision: ResearchDecision
    coverage: tuple[RequirementCoverage, ...]
    critical_gaps: tuple[CriticalGap, ...] = ()
    next_actions: tuple[NextResearchAction, ...] = ()
    termination_reason: TerminationReason | None = None
    replan_reason: str | None = None
    exhaustion_reason: str | None = None
    output_status: OutputStatus = OutputStatus.VALID


def _clean_sequence(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def build_research_requirements(task: ResearchTask) -> tuple[ResearchRequirement, ...]:
    """Create stable R1..Rn requirements from the confirmed Brief task context."""
    context = task.context if isinstance(task.context, dict) else {}
    explicit = context.get("research_requirements")
    descriptions: list[str] = []
    if isinstance(explicit, (list, tuple)):
        for item in explicit:
            if isinstance(item, dict):
                description = str(item.get("description") or "").strip()
            else:
                description = str(item).strip()
            if description:
                descriptions.append(description)
    if not descriptions:
        # Directions are the confirmed work items. Memory gaps are appended when
        # they add a distinct necessary requirement; scope remains a boundary.
        descriptions.extend(_clean_sequence(context.get("directions")))
        descriptions.extend(_clean_sequence(context.get("research_gaps")))
    if not descriptions:
        descriptions.append(task.objective.strip())
    unique = list(dict.fromkeys(descriptions))
    return tuple(
        ResearchRequirement(f"R{index}", description)
        for index, description in enumerate(unique, 1)
    )


def initial_coverage(
    requirements: Iterable[ResearchRequirement],
) -> tuple[RequirementCoverage, ...]:
    return tuple(
        RequirementCoverage(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.UNSUPPORTED,
            remaining_gap=requirement.description,
        )
        for requirement in requirements
    )


def parse_json_object(content: str) -> dict[str, Any]:
    candidate = (content or "").strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AssessmentValidationError("response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AssessmentValidationError("response must be a JSON object")
    return payload


def _enum_value(enum_type: Any, raw: Any, field: str) -> Any:
    value = str(raw or "").strip().lower().replace(" ", "_")
    if value == "stop":
        value = "stop_research"
    try:
        return enum_type(value)
    except ValueError as exc:
        raise AssessmentValidationError(f"invalid {field}: {raw!r}") from exc


def _parse_coverage(
    raw: Any,
    *,
    requirements: tuple[ResearchRequirement, ...],
    evidence_ids: set[str],
    require_evidence: bool,
) -> tuple[RequirementCoverage, ...]:
    if not isinstance(raw, list):
        raise AssessmentValidationError("coverage must be an array")
    expected_ids = [item.requirement_id for item in requirements if item.required]
    parsed: list[RequirementCoverage] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise AssessmentValidationError("coverage entries must be objects")
        requirement_id = str(item.get("requirement_id") or "").strip()
        if requirement_id not in expected_ids or requirement_id in seen:
            raise AssessmentValidationError(
                f"coverage contains an unknown or duplicate requirement: {requirement_id!r}"
            )
        seen.add(requirement_id)
        status = _enum_value(RequirementStatus, item.get("status"), "coverage status")
        cited = tuple(dict.fromkeys(_clean_sequence(item.get("evidence_ids"))))
        missing = [evidence_id for evidence_id in cited if evidence_id not in evidence_ids]
        if missing:
            raise AssessmentValidationError(
                f"coverage references unknown Evidence IDs: {', '.join(missing)}"
            )
        if status == RequirementStatus.SUPPORTED and require_evidence and not cited:
            raise AssessmentValidationError(
                f"supported requirement {requirement_id} must cite existing evidence"
            )
        parsed.append(
            RequirementCoverage(
                requirement_id=requirement_id,
                status=status,
                evidence_ids=cited,
                rationale=str(item.get("rationale") or "").strip(),
                remaining_gap=(
                    str(item.get("remaining_gap")).strip()
                    if item.get("remaining_gap") is not None
                    else None
                ),
            )
        )
    if seen != set(expected_ids):
        missing = [item for item in expected_ids if item not in seen]
        raise AssessmentValidationError(
            f"coverage is missing required requirements: {', '.join(missing)}"
        )
    by_id = {item.requirement_id: item for item in parsed}
    return tuple(by_id[item] for item in expected_ids)


def _parse_gaps(raw: Any, requirement_ids: set[str]) -> tuple[CriticalGap, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AssessmentValidationError("critical_gaps must be an array")
    gaps: list[CriticalGap] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AssessmentValidationError("critical gap entries must be objects")
        requirement_id = str(item.get("requirement_id") or "").strip()
        reason = str(item.get("reason") or "").strip()
        impact = str(item.get("impact") or "high").strip().lower()
        if requirement_id not in requirement_ids or not reason or impact not in _IMPACTS:
            raise AssessmentValidationError("critical gap is not requirement-scoped")
        gaps.append(CriticalGap(requirement_id, reason, impact))
    return tuple(gaps)


def _parse_actions(raw: Any, requirement_ids: set[str]) -> tuple[NextResearchAction, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AssessmentValidationError("next_actions must be an array")
    actions: list[NextResearchAction] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AssessmentValidationError("next action entries must be objects")
        requirement_id = str(item.get("requirement_id") or "").strip()
        strategy = str(item.get("strategy") or "").strip().lower()
        query = str(item.get("query") or "").strip()
        expected_value = str(item.get("expected_value") or "").strip().lower()
        improvement = str(item.get("expected_improvement") or "").strip()
        if (
            requirement_id not in requirement_ids
            or not strategy
            or not query
            or expected_value not in _EXPECTED_VALUES
            or not improvement
        ):
            raise AssessmentValidationError("next action is not executable")
        actions.append(
            NextResearchAction(
                requirement_id,
                strategy,
                query,
                expected_value,
                improvement,
            )
        )
    return tuple(actions)


def parse_research_assessment(
    content: str,
    *,
    requirements: tuple[ResearchRequirement, ...],
    evidence: Iterable[EvidenceItem],
    attempts: Iterable[StrategyAttempt],
    require_evidence: bool = True,
    output_status: OutputStatus = OutputStatus.VALID,
) -> ResearchAssessment:
    """Parse and deterministically validate one semantic assessment response."""
    payload = parse_json_object(content)
    decision = _enum_value(ResearchDecision, payload.get("decision"), "decision")
    evidence_ids = {item.evidence_id for item in evidence}
    requirement_ids = {
        item.requirement_id for item in requirements if item.required
    }
    coverage = _parse_coverage(
        payload.get("coverage"),
        requirements=requirements,
        evidence_ids=evidence_ids,
        require_evidence=require_evidence,
    )
    gaps = _parse_gaps(payload.get("critical_gaps", []), requirement_ids)
    actions = _parse_actions(payload.get("next_actions", []), requirement_ids)
    raw_reason = payload.get("termination_reason")
    termination_reason = (
        None
        if raw_reason is None or str(raw_reason).strip().lower() in {"", "null", "none"}
        else _enum_value(TerminationReason, raw_reason, "termination_reason")
    )
    replan_reason = str(payload.get("replan_reason") or "").strip() or None
    exhaustion_reason = str(payload.get("exhaustion_reason") or "").strip() or None

    gap_ids = {item.requirement_id for item in gaps}
    coverage_by_id = {item.requirement_id: item for item in coverage}
    invalid_gap_ids = [
        item.requirement_id
        for item in gaps
        if coverage_by_id[item.requirement_id].status == RequirementStatus.SUPPORTED
    ]
    if invalid_gap_ids:
        raise AssessmentValidationError(
            "critical gaps cannot target supported requirements: "
            + ", ".join(invalid_gap_ids)
        )
    actionable = [
        item
        for item in actions
        if item.requirement_id in gap_ids and item.expected_value in {"high", "medium"}
    ]
    if decision in {ResearchDecision.CONTINUE, ResearchDecision.REPLAN}:
        if termination_reason is not None:
            raise AssessmentValidationError("continue/replan cannot terminate research")
        if not gaps or not actionable:
            raise AssessmentValidationError(
                "continue/replan requires an important gap and an executable action"
            )
        if not any(item.impact in {"high", "medium"} for item in gaps):
            raise AssessmentValidationError(
                "continue/replan requires a high- or medium-impact gap"
            )
    if decision == ResearchDecision.REPLAN:
        if not replan_reason:
            raise AssessmentValidationError("replan must explain why the old path is low value")
        tried = {
            (item.requirement_id, item.strategy.strip().lower()) for item in attempts
        }
        if not any(
            (item.requirement_id, item.strategy) not in tried for item in actionable
        ):
            raise AssessmentValidationError("replan must provide a materially new strategy")
    if decision == ResearchDecision.STOP_RESEARCH and termination_reason is None:
        raise AssessmentValidationError("stop_research requires a termination_reason")
    if decision != ResearchDecision.STOP_RESEARCH and termination_reason is not None:
        raise AssessmentValidationError("termination_reason requires stop_research")
    if termination_reason == TerminationReason.COVERAGE_COMPLETE:
        incomplete = [
            item.requirement_id
            for item in coverage
            if item.status != RequirementStatus.SUPPORTED
        ]
        if incomplete or gaps or actions:
            raise AssessmentValidationError(
                "coverage_complete cannot retain incomplete coverage, gaps, or next actions"
            )
    if termination_reason == TerminationReason.SATURATED:
        incomplete_ids = {
            item.requirement_id
            for item in coverage
            if item.status != RequirementStatus.SUPPORTED
        }
        low_gap_ids = {item.requirement_id for item in gaps if item.impact == "low"}
        if actions or any(item.impact in {"high", "medium"} for item in gaps):
            raise AssessmentValidationError(
                "saturated may retain only non-actionable low-impact gaps"
            )
        if incomplete_ids and not incomplete_ids.issubset(low_gap_ids):
            raise AssessmentValidationError(
                "saturated must classify every incomplete requirement as low impact"
            )
    if termination_reason == TerminationReason.EVIDENCE_EXHAUSTED:
        if not gaps or actions or not exhaustion_reason:
            raise AssessmentValidationError(
                "evidence_exhausted requires important gaps, no next action, and a reason"
            )
        if not any(item.impact in {"high", "medium"} for item in gaps):
            raise AssessmentValidationError(
                "evidence_exhausted requires an important unresolved gap"
            )
        attempts_by_requirement: dict[str, set[str]] = {}
        for item in attempts:
            if item.outcome == "no_progress":
                attempts_by_requirement.setdefault(item.requirement_id, set()).add(
                    item.strategy.strip().lower()
                )
        if any(
            len(attempts_by_requirement.get(gap.requirement_id, set())) < 2
            for gap in gaps
            if gap.impact in {"high", "medium"}
        ):
            raise AssessmentValidationError(
                "evidence_exhausted requires multiple distinct no-progress strategies per important gap"
            )
    if termination_reason in {
        TerminationReason.BUDGET_FORCED,
        TerminationReason.TOOL_FAILURE,
        TerminationReason.USER_CANCELLED,
    }:
        raise AssessmentValidationError(
            f"{termination_reason.value} can only be emitted by deterministic runtime state"
        )

    return ResearchAssessment(
        decision=decision,
        coverage=coverage,
        critical_gaps=gaps,
        next_actions=actions,
        termination_reason=termination_reason,
        replan_reason=replan_reason,
        exhaustion_reason=exhaustion_reason,
        output_status=output_status,
    )


def hard_termination_reason(stop_reason: str | None) -> TerminationReason | None:
    """Map concrete fuse/error details to the stable public stop taxonomy."""
    if not stop_reason:
        return None
    lowered = stop_reason.lower()
    if lowered == "user_cancelled":
        return TerminationReason.USER_CANCELLED
    if any(
        token in lowered
        for token in ("budget", "max_iterations", "max_tool_calls", "token_budget", "time_budget")
    ):
        return TerminationReason.BUDGET_FORCED
    return TerminationReason.TOOL_FAILURE


def assessment_schema_prompt(
    *,
    task: ResearchTask,
    requirements: tuple[ResearchRequirement, ...],
    coverage: tuple[RequirementCoverage, ...],
    evidence: Iterable[EvidenceItem],
    critical_gaps: tuple[CriticalGap, ...],
    attempts: tuple[StrategyAttempt, ...],
    candidate_final: str,
    recent_tool_failures: tuple[str, ...],
) -> str:
    """Build the bounded same-policy prompt for semantic sufficiency assessment."""
    evidence_payload = [
        {
            "evidence_id": item.evidence_id,
            "finding": item.finding[:1000],
            "source_type": item.source_type,
            "title": item.title,
            "source_ref": item.source_ref,
            "limitations": item.limitations,
        }
        for item in evidence
    ]
    payload = {
        "objective": task.objective,
        "require_evidence": task.require_evidence,
        "expected_output": task.expected_output,
        "constraints": list(task.constraints),
        "requirements": [
            {
                "requirement_id": item.requirement_id,
                "description": item.description,
                "required": item.required,
            }
            for item in requirements
        ],
        "previous_coverage": [
            {
                "requirement_id": item.requirement_id,
                "status": item.status.value,
                "evidence_ids": list(item.evidence_ids),
                "remaining_gap": item.remaining_gap,
            }
            for item in coverage
        ],
        "evidence": evidence_payload,
        "previous_critical_gaps": [item.__dict__ for item in critical_gaps],
        "strategy_attempts": [
            {
                **item.__dict__,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in attempts
        ],
        "recent_tool_failures": list(recent_tool_failures),
        "candidate_final_response": candidate_final[:8000],
    }
    return (
        "ASSESS_RESEARCH_STATE\n"
        "You are the same Research Agent policy, performing a structured in-graph "
        "sufficiency decision. Source count and loop count are observations only. "
        "Decide continue, replan, or stop_research from confirmed requirements, "
        "evidence strength, actionable next-step value, and real constraints. Do not "
        "claim supported without citing listed Evidence IDs. Replan needs a materially "
        "different strategy. Return JSON only with: decision; coverage entries for "
        "every requirement (requirement_id,status,evidence_ids,rationale,remaining_gap); "
        "critical_gaps (requirement_id,reason,impact); next_actions "
        "(requirement_id,strategy,query,expected_value,expected_improvement); "
        "termination_reason; replan_reason; exhaustion_reason. Valid termination "
        "reasons you may return: coverage_complete, saturated, evidence_exhausted. "
        "budget_forced, tool_failure, and user_cancelled are runtime-only reasons; "
        "never infer them.\n\nSTATE:\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def repair_assessment_prompt(content: str, error: str) -> str:
    return (
        "REPAIR_ASSESSMENT_JSON\n"
        "Repair the following assessment into the exact requested JSON schema. "
        "Do not call tools and do not add evidence or change the research objective.\n"
        f"Validation error: {error}\nInvalid response:\n{content[:12000]}"
    )


def control_message(assessment: ResearchAssessment) -> str:
    action_payload = [item.__dict__ for item in assessment.next_actions]
    gap_payload = [item.__dict__ for item in assessment.critical_gaps]
    return (
        "RESEARCH_STATE_DECISION\n"
        f"Decision: {assessment.decision.value}.\n"
        f"Critical gaps: {json.dumps(gap_payload, ensure_ascii=False)}\n"
        f"Next actions: {json.dumps(action_payload, ensure_ascii=False)}\n"
        "Keep tools available and work only on these unresolved requirements. "
        "Do not silently expand the confirmed objective."
    )


def finalization_prompt(assessment: ResearchAssessment) -> str:
    return (
        "FINAL_SYNTHESIS\n"
        f"Research stopped because {assessment.termination_reason.value if assessment.termination_reason else 'unknown'}. "
        "Return one JSON object with status, summary, findings, and unresolved. "
        "Use only collected evidence, disclose remaining gaps, and do not call tools."
    )


def repair_final_prompt(content: str, error: str) -> str:
    return (
        "REPAIR_FINAL_JSON\n"
        "Repair only the structure of this final response. Return JSON with status "
        "(completed|partial|failed), summary, findings array, and unresolved array. "
        "Do not add claims or evidence and do not call tools.\n"
        f"Validation error: {error}\nInvalid response:\n{content[:12000]}"
    )

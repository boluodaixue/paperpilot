"""Contracts and deterministic validation for in-graph research sufficiency."""

from __future__ import annotations

import hashlib
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
    ResearchResult,
    ResearchTask,
    StrategyAttempt,
    TerminationReason,
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_EXPECTED_VALUES = {"high", "medium", "low"}
_IMPACTS = {"high", "medium", "low"}
_STRATEGIES = {
    "official_database",
    "primary_document",
    "query_rewrite",
    "paper_search",
    "other",
}
_ASSESSMENT_PROJECTION_CHAR_BUDGET = 30000


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


def atomic_requirement_descriptions(values: Iterable[str]) -> list[str]:
    """Split only explicit list/newline/semicolon boundaries, preserving meaning."""
    atomic: list[str] = []
    for value in values:
        for part in re.split(r"[;；\r\n]+", str(value)):
            cleaned = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)、])\s*", "", part).strip()
            if cleaned:
                atomic.append(cleaned)
    return list(dict.fromkeys(atomic))


def build_research_requirements(task: ResearchTask) -> tuple[ResearchRequirement, ...]:
    """Create stable R1..Rn requirements from the confirmed Brief task context."""
    context = task.context if isinstance(task.context, dict) else {}
    explicit = context.get("research_requirements")
    explicit_requirements: list[tuple[str | None, str, bool, bool]] = []
    if isinstance(explicit, (list, tuple)):
        for item in explicit:
            if isinstance(item, dict):
                description = str(item.get("description") or "").strip()
                requirement_id = str(item.get("requirement_id") or "").strip() or None
                required = bool(item.get("required", True))
                requires_external_evidence = bool(
                    item.get("requires_external_evidence", True)
                )
            else:
                description = str(item).strip()
                requirement_id = None
                required = True
                requires_external_evidence = True
            parts = atomic_requirement_descriptions([description])
            for index, part in enumerate(parts):
                explicit_requirements.append((
                    requirement_id if index == 0 else None,
                    part,
                    required,
                    requires_external_evidence,
                ))
    if explicit_requirements:
        requirements: list[ResearchRequirement] = []
        used_ids: set[str] = set()
        next_index = 1
        for requirement_id, description, required, requires_external_evidence in explicit_requirements:
            while f"R{next_index}" in used_ids:
                next_index += 1
            if not requirement_id or requirement_id in used_ids:
                requirement_id = f"R{next_index}"
                next_index += 1
            used_ids.add(requirement_id)
            requirements.append(ResearchRequirement(
                requirement_id,
                description,
                required,
                requires_external_evidence,
            ))
        return tuple(requirements)

    descriptions: list[str] = []
    if not explicit_requirements:
        # Directions are the confirmed work items. Memory gaps are appended when
        # they add a distinct necessary requirement; scope remains a boundary.
        descriptions.extend(_clean_sequence(context.get("directions")))
        descriptions.extend(_clean_sequence(context.get("research_gaps")))
        descriptions = atomic_requirement_descriptions(descriptions)
        default_output = "Evidence-backed findings and a concise summary."
        expected_output = task.expected_output.strip()
        deliverable = (
            f"Deliverable: {expected_output}" if expected_output and expected_output != default_output else None
        )
        if deliverable:
            descriptions.append(deliverable)
    if not descriptions:
        descriptions.append(task.objective.strip())
    unique = list(dict.fromkeys(descriptions))
    return tuple(
        ResearchRequirement(
            f"R{index}",
            description,
            required=description != deliverable,
        )
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
        if requirement.required and requirement.requires_external_evidence
    )


def merge_child_coverage_evidence(
    parent_coverage: Iterable[RequirementCoverage],
    child_results: Iterable[ResearchResult],
) -> tuple[RequirementCoverage, ...]:
    """Attach child support references without inheriting child status."""
    parent_coverage = tuple(parent_coverage)
    valid_parent_ids = {item.requirement_id for item in parent_coverage}
    evidence_by_requirement: dict[str, list[str]] = {
        item.requirement_id: list(item.evidence_ids) for item in parent_coverage
    }
    for result in child_results:
        child_evidence_ids = {item.evidence_id for item in result.evidence}
        for item in result.coverage:
            if item.requirement_id not in valid_parent_ids:
                continue
            ranked = evidence_by_requirement[item.requirement_id]
            for evidence_id in item.evidence_ids:
                if evidence_id in child_evidence_ids and evidence_id not in ranked:
                    ranked.append(evidence_id)
    return tuple(
        RequirementCoverage(
            requirement_id=item.requirement_id,
            status=item.status,
            evidence_ids=tuple(evidence_by_requirement[item.requirement_id]),
            rationale=item.rationale,
            remaining_gap=item.remaining_gap,
        )
        for item in parent_coverage
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
    evidence_by_id: dict[str, EvidenceItem],
    require_evidence: bool,
) -> tuple[RequirementCoverage, ...]:
    if not isinstance(raw, list):
        raise AssessmentValidationError("coverage must be an array")
    expected_ids = [
        item.requirement_id
        for item in requirements
        if item.required and item.requires_external_evidence
    ]
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
        missing = [evidence_id for evidence_id in cited if evidence_id not in evidence_by_id]
        if missing:
            raise AssessmentValidationError(f"coverage references unknown Evidence IDs: {', '.join(missing)}")
        cross_requirement = [
            evidence_id
            for evidence_id in cited
            if evidence_by_id[evidence_id].requirement_id
            and evidence_by_id[evidence_id].requirement_id != requirement_id
        ]
        if cross_requirement:
            raise AssessmentValidationError(
                "coverage cites Evidence IDs bound to another requirement: " + ", ".join(cross_requirement)
            )
        if status == RequirementStatus.SUPPORTED and require_evidence and not cited:
            raise AssessmentValidationError(f"supported requirement {requirement_id} must cite existing evidence")
        parsed.append(
            RequirementCoverage(
                requirement_id=requirement_id,
                status=status,
                evidence_ids=cited,
                rationale=str(item.get("rationale") or "").strip(),
                remaining_gap=(
                    str(item.get("remaining_gap")).strip() if item.get("remaining_gap") is not None else None
                ),
            )
        )
    if seen != set(expected_ids):
        missing = [item for item in expected_ids if item not in seen]
        raise AssessmentValidationError(f"coverage is missing required requirements: {', '.join(missing)}")
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
        if strategy not in _STRATEGIES:
            raise AssessmentValidationError("next action strategy is not canonical")
        if (
            requirement_id not in requirement_ids
            or not query
            or expected_value not in _EXPECTED_VALUES
            or not improvement
        ):
            raise AssessmentValidationError("next action is not executable")
        action = NextResearchAction(
            requirement_id,
            strategy,
            query,
            expected_value,
            improvement,
        )
        actions.append(
            NextResearchAction(
                action.requirement_id,
                action.strategy,
                action.query,
                action.expected_value,
                action.expected_improvement,
                stable_action_id(action),
            )
        )
    return tuple(actions)


def _normalized_action_key(
    requirement_id: str,
    strategy: str,
    query: str,
) -> tuple[str, str, str]:
    return (
        requirement_id.strip(),
        strategy.strip().lower(),
        " ".join(query.lower().split()),
    )


def stable_action_id(action: NextResearchAction) -> str:
    """Return the deterministic runtime identity for one scoped action."""
    if action.action_id.strip():
        return action.action_id.strip()
    encoded = json.dumps(
        {
            "requirement_id": action.requirement_id.strip(),
            "strategy": action.strategy.strip().lower(),
            "query": " ".join(action.query.lower().split()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "action-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def unattempted_actions(
    actions: Iterable[NextResearchAction],
    attempts: Iterable[StrategyAttempt],
) -> tuple[NextResearchAction, ...]:
    """Return actions whose requirement, strategy, and query were not executed."""
    attempted = {_normalized_action_key(item.requirement_id, item.strategy, item.query) for item in attempts}
    return tuple(
        item
        for item in actions
        if _normalized_action_key(
            item.requirement_id,
            item.strategy,
            item.query,
        )
        not in attempted
    )


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
    attempts = tuple(attempts)
    payload = parse_json_object(content)
    decision = _enum_value(ResearchDecision, payload.get("decision"), "decision")
    evidence_by_id = {item.evidence_id: item for item in evidence}
    requirement_ids = {
        item.requirement_id
        for item in requirements
        if item.required and item.requires_external_evidence
    }
    coverage = _parse_coverage(
        payload.get("coverage"),
        requirements=requirements,
        evidence_by_id=evidence_by_id,
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
            "critical gaps cannot target supported requirements: " + ", ".join(invalid_gap_ids)
        )
    actionable = [
        item for item in actions if item.requirement_id in gap_ids and item.expected_value in {"high", "medium"}
    ]
    if decision in {ResearchDecision.CONTINUE, ResearchDecision.REPLAN}:
        if termination_reason is not None:
            raise AssessmentValidationError("continue/replan cannot terminate research")
        if not gaps or not actionable:
            raise AssessmentValidationError("continue/replan requires an important gap and an executable action")
        if not any(item.impact in {"high", "medium"} for item in gaps):
            raise AssessmentValidationError("continue/replan requires a high- or medium-impact gap")
        if len(unattempted_actions(actionable, attempts)) != len(actionable):
            raise AssessmentValidationError("continue/replan next actions must not repeat an executed action")
        repeated_no_progress: dict[tuple[str, str], int] = {}
        for attempt in attempts:
            if attempt.outcome != "no_progress":
                continue
            key = (attempt.requirement_id, attempt.strategy.strip().lower())
            repeated_no_progress[key] = repeated_no_progress.get(key, 0) + 1
        saturated_families = [
            action
            for action in actionable
            if repeated_no_progress.get(
                (action.requirement_id, action.strategy.strip().lower()),
                0,
            )
            >= 2
        ]
        if saturated_families:
            raise AssessmentValidationError("continue/replan must change a strategy family after repeated no progress")
    if decision == ResearchDecision.REPLAN:
        if not replan_reason:
            raise AssessmentValidationError("replan must explain why the old path is low value")
        tried = {(item.requirement_id, item.strategy.strip().lower()) for item in attempts}
        if not any((item.requirement_id, item.strategy) not in tried for item in actionable):
            raise AssessmentValidationError("replan must provide a materially new strategy")
    if decision == ResearchDecision.STOP_RESEARCH and termination_reason is None:
        raise AssessmentValidationError("stop_research requires a termination_reason")
    if decision != ResearchDecision.STOP_RESEARCH and termination_reason is not None:
        raise AssessmentValidationError("termination_reason requires stop_research")
    if termination_reason == TerminationReason.COVERAGE_COMPLETE:
        incomplete = [item.requirement_id for item in coverage if item.status != RequirementStatus.SUPPORTED]
        if incomplete or gaps or actions:
            raise AssessmentValidationError(
                "coverage_complete cannot retain incomplete coverage, gaps, or next actions"
            )
    if termination_reason == TerminationReason.SATURATED:
        incomplete_ids = {item.requirement_id for item in coverage if item.status != RequirementStatus.SUPPORTED}
        low_gap_ids = {item.requirement_id for item in gaps if item.impact == "low"}
        if actions or any(item.impact in {"high", "medium"} for item in gaps):
            raise AssessmentValidationError("saturated may retain only non-actionable low-impact gaps")
        if incomplete_ids and not incomplete_ids.issubset(low_gap_ids):
            raise AssessmentValidationError("saturated must classify every incomplete requirement as low impact")
    if termination_reason == TerminationReason.EVIDENCE_EXHAUSTED:
        if not gaps or actions or not exhaustion_reason:
            raise AssessmentValidationError("evidence_exhausted requires important gaps, no next action, and a reason")
        if not any(item.impact in {"high", "medium"} for item in gaps):
            raise AssessmentValidationError("evidence_exhausted requires an important unresolved gap")
        attempts_by_requirement: dict[str, set[str]] = {}
        for item in attempts:
            if item.outcome == "no_progress":
                attempts_by_requirement.setdefault(item.requirement_id, set()).add(item.strategy.strip().lower())
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
    if any(token in lowered for token in ("budget", "max_iterations", "max_tool_calls", "token_budget", "time_budget")):
        return TerminationReason.BUDGET_FORCED
    return TerminationReason.TOOL_FAILURE


def aggregate_strategy_attempts(
    attempts: Iterable[StrategyAttempt],
) -> tuple[dict[str, Any], ...]:
    """Project the append-only attempt ledger into requirement/strategy outcomes."""
    aggregates: dict[tuple[str, str], dict[str, Any]] = {}
    query_sets: dict[tuple[str, str], set[str]] = {}
    evidence_sets: dict[tuple[str, str], set[str]] = {}
    for item in attempts:
        key = (item.requirement_id, item.strategy.strip().lower())
        if key not in aggregates:
            aggregates[key] = {
                "requirement_id": item.requirement_id,
                "strategy": item.strategy.strip().lower(),
                "attempt_count": 0,
                "evidence_found_count": 0,
                "no_progress_count": 0,
                "last_query": "",
                "last_outcome": "",
            }
            query_sets[key] = set()
            evidence_sets[key] = set()
        aggregate = aggregates[key]
        aggregate["attempt_count"] += 1
        if item.outcome == "evidence_found":
            aggregate["evidence_found_count"] += 1
        elif item.outcome == "no_progress":
            aggregate["no_progress_count"] += 1
        aggregate["last_query"] = item.query[:600]
        aggregate["last_outcome"] = item.outcome
        query_sets[key].add(" ".join(item.query.lower().split()))
        evidence_sets[key].update(item.evidence_ids)
    projected: list[dict[str, Any]] = []
    for key, aggregate in aggregates.items():
        projected.append(
            {
                **aggregate,
                "distinct_query_count": len(query_sets[key]),
                "evidence_count": len(evidence_sets[key]),
            }
        )
    return tuple(projected)


def _child_result_projection(
    child_results: Iterable[ResearchResult],
) -> list[dict[str, Any]]:
    return [
        {
            "task_id": result.task_id,
            "status": result.status.value,
            "termination_reason": (result.termination_reason.value if result.termination_reason is not None else None),
            "summary": result.summary[:800],
            "research_memo": result.research_memo[:3500],
            "coverage": [
                {
                    "requirement_id": item.requirement_id,
                    "status": item.status.value,
                    "evidence_ids": list(item.evidence_ids),
                    "remaining_gap": item.remaining_gap,
                }
                for item in result.coverage
            ],
            "critical_gaps": [item.__dict__ for item in result.critical_gaps],
        }
        for result in child_results
    ]


def build_assessment_projection(
    *,
    task: ResearchTask,
    requirements: tuple[ResearchRequirement, ...],
    coverage: tuple[RequirementCoverage, ...],
    evidence: Iterable[EvidenceItem],
    critical_gaps: tuple[CriticalGap, ...],
    attempts: tuple[StrategyAttempt, ...],
    child_results: Iterable[ResearchResult],
    candidate_final: str,
    recent_tool_failures: tuple[str, ...],
    recent_tool_outcomes: tuple[dict[str, str], ...],
    focus_evidence_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a bounded semantic projection while retaining raw state in checkpoint."""
    evidence = tuple(evidence)
    child_results = tuple(child_results)
    focus_ids = tuple(dict.fromkeys(focus_evidence_ids))
    candidate_ids: set[str] = {evidence_id for item in coverage for evidence_id in item.evidence_ids}
    # Coverage can temporarily be unsupported while a newly gathered item is
    # awaiting assessment. Retain a small, bounded tail per requirement so a
    # later assessment can close multiple requirements without replaying the
    # full evidence history.
    recent_by_requirement: dict[str, list[str]] = {}
    for item in evidence:
        if item.requirement_id:
            recent_by_requirement.setdefault(item.requirement_id, []).append(item.evidence_id)
    for evidence_ids in recent_by_requirement.values():
        candidate_ids.update(evidence_ids[-3:])
    candidate_ids.update(
        evidence_id for item in attempts if item.outcome == "evidence_found" for evidence_id in item.evidence_ids
    )
    candidate_ids.update(
        evidence_id for result in child_results for item in result.coverage for evidence_id in item.evidence_ids
    )
    classified_ids = {evidence_id for item in attempts for evidence_id in item.evidence_ids}
    classified_ids.update(evidence_id for item in coverage for evidence_id in item.evidence_ids)
    candidate_ids.update(item.evidence_id for item in evidence if item.evidence_id not in classified_ids)

    source_type_counts: dict[str, int] = {}
    for item in evidence:
        source_type_counts[item.source_type] = source_type_counts.get(item.source_type, 0) + 1
    payload: dict[str, Any] = {
        "objective": task.objective,
        "require_evidence": task.require_evidence,
        "expected_output": task.expected_output,
        "constraints": list(task.constraints),
        "requirements": [
            item.__dict__
            for item in requirements
            if item.required and item.requires_external_evidence
        ],
        "synthesis_requirements": [
            item.__dict__
            for item in requirements
            if item.required and not item.requires_external_evidence
        ],
        "previous_coverage": [
            {
                "requirement_id": item.requirement_id,
                "status": item.status.value,
                "evidence_ids": list(item.evidence_ids),
                "rationale": item.rationale,
                "remaining_gap": item.remaining_gap,
            }
            for item in coverage
        ],
        "evidence_inventory": {
            "total_count": len(evidence),
            "candidate_count": len(candidate_ids),
            "focus_count": len(focus_ids),
            "included_focus_count": 0,
            "included_count": 0,
            "omitted_candidate_count": 0,
            "source_type_counts": source_type_counts,
        },
        "evidence": [],
        "previous_critical_gaps": [item.__dict__ for item in critical_gaps],
        "strategy_attempt_summary": list(aggregate_strategy_attempts(attempts)),
        "child_result_summary": _child_result_projection(child_results),
        "recent_tool_failures": list(recent_tool_failures),
        "recent_tool_outcomes": list(recent_tool_outcomes),
        "candidate_final_response": candidate_final[:2000],
    }
    by_id = {item.evidence_id: item for item in evidence}
    focused = [by_id[item] for item in focus_ids if item in by_id]
    focused_id_set = {item.evidence_id for item in focused}
    candidate_evidence = [
        *focused,
        *(item for item in evidence if item.evidence_id in candidate_ids and item.evidence_id not in focused_id_set),
    ]
    for item in candidate_evidence:
        compact = {
            "evidence_id": item.evidence_id,
            "finding": item.finding[:320],
            "source_type": item.source_type,
            "title": item.title[:180],
            "source_ref": item.source_ref[:300],
            "limitations": item.limitations[:180],
            "requirement_id": item.requirement_id,
            "action_id": item.action_id,
            "artifact_id": item.artifact_id,
        }
        payload["evidence"].append(compact)
        if len(json.dumps(payload, ensure_ascii=False, default=str)) > (_ASSESSMENT_PROJECTION_CHAR_BUDGET):
            payload["evidence"].pop()
            break
    included = len(payload["evidence"])
    payload["evidence_inventory"]["included_count"] = included
    payload["evidence_inventory"]["included_focus_count"] = sum(
        item["evidence_id"] in focused_id_set for item in payload["evidence"]
    )
    payload["evidence_inventory"]["omitted_candidate_count"] = max(
        0,
        len(candidate_evidence) - included,
    )
    return payload


def assessment_schema_prompt(
    *,
    task: ResearchTask,
    requirements: tuple[ResearchRequirement, ...],
    coverage: tuple[RequirementCoverage, ...],
    evidence: Iterable[EvidenceItem],
    critical_gaps: tuple[CriticalGap, ...],
    attempts: tuple[StrategyAttempt, ...],
    child_results: Iterable[ResearchResult],
    candidate_final: str,
    recent_tool_failures: tuple[str, ...],
    recent_tool_outcomes: tuple[dict[str, str], ...],
    focus_evidence_ids: tuple[str, ...] = (),
) -> str:
    """Build the bounded same-policy prompt for semantic sufficiency assessment."""
    payload = build_assessment_projection(
        task=task,
        requirements=requirements,
        coverage=coverage,
        evidence=evidence,
        critical_gaps=critical_gaps,
        attempts=attempts,
        child_results=child_results,
        candidate_final=candidate_final,
        recent_tool_failures=recent_tool_failures,
        recent_tool_outcomes=recent_tool_outcomes,
        focus_evidence_ids=focus_evidence_ids,
    )
    return (
        "ASSESS_RESEARCH_STATE\n"
        "You are the same Research Agent policy, performing a structured in-graph "
        "sufficiency decision. Source count and loop count are observations only. "
        "Decide continue, replan, or stop_research from confirmed requirements, "
        "evidence strength, actionable next-step value, and real constraints. Do not "
        "claim supported without citing listed Evidence IDs. Replan needs a materially "
        "different strategy. Evidence with a non-empty requirement_id is scoped to "
        "that requirement and MUST NOT be cited by any other requirement. If useful "
        "evidence is scoped elsewhere, leave the target requirement incomplete and "
        "schedule a requirement-scoped next action instead of reusing the Evidence ID. "
        "A Continue action must not repeat an action already shown "
        "in the strategy-attempt summary; repeated queries are not new research value. "
        "When important gaps have multiple no-progress strategy families and no "
        "materially different executable path remains, choose evidence_exhausted. "
        "Return JSON only with: decision; coverage entries for "
        "every requirement (requirement_id,status,evidence_ids,rationale,remaining_gap); "
        "Coverage evidence_ids only justify the coverage decision; they are not a "
        "final-report whitelist or ranking. Select and explain report-worthy material "
        "later in the scoped Child research memo or Root report. "
        "critical_gaps (requirement_id,reason,impact where impact is exactly high, "
        "medium, or low); next_actions (requirement_id,strategy,query,expected_value,"
        "expected_improvement). "
        "strategy MUST be exactly one of official_database, primary_document, "
        "query_rewrite, paper_search, or other; put detailed plans in query or "
        "expected_improvement, never in strategy. "
        "expected_value is a priority label and MUST be exactly high, medium, or low; "
        "put the prose description of the expected result only in "
        "expected_improvement. Return at most one current best next_action per "
        "requirement and order requirements from highest to lowest execution priority; "
        "the runtime activates one action at a time so every real tool call has an "
        "unambiguous requirement and strategy. "
        "termination_reason; replan_reason; exhaustion_reason. Valid termination "
        "reasons you may return: coverage_complete, saturated, evidence_exhausted. "
        "budget_forced, tool_failure, and user_cancelled are runtime-only reasons; "
        "never infer them.\n\nSTATE:\n" + json.dumps(payload, ensure_ascii=False, default=str)
    )


def repair_assessment_prompt(
    content: str,
    error: str,
    *,
    evidence_bindings: dict[str, str] | None = None,
) -> str:
    binding_lines = [
        f"- {evidence_id}: {requirement_id or 'unbound'}"
        for evidence_id, requirement_id in sorted((evidence_bindings or {}).items())
    ]
    binding_guidance = (
        "\nAllowed Evidence-ID bindings (an ID bound to Rn may be cited only by Rn; "
        "unbound IDs may be cited wherever semantically supported):\n"
        + "\n".join(binding_lines)
        if binding_lines
        else ""
    )
    return (
        "REPAIR_ASSESSMENT_JSON\n"
        "Repair the following assessment into the exact requested JSON schema. "
        "Do not call tools and do not add evidence or change the research objective.\n"
        'Required shape: {"decision":"continue|replan|stop_research",'
        '"coverage":[{"requirement_id":"R1","status":'
        '"supported|weak|conflicted|unsupported","evidence_ids":[],'
        '"rationale":"...","remaining_gap":"...|null"}],'
        '"critical_gaps":[{"requirement_id":"R1","reason":"...",'
        '"impact":"high|medium|low"}],"next_actions":[{'
        '"requirement_id":"R1","strategy":"official_database|primary_document|'
        'query_rewrite|paper_search|other",'
        '"query":"...","expected_value":"high|medium|low",'
        '"expected_improvement":"..."}],"termination_reason":null,'
        '"replan_reason":null,"exhaustion_reason":null}. '
        "The vertical-bar values above are alternatives, never literal output. "
        "strategy is only the strategy-family enum; move detailed strategy prose "
        "into query or expected_improvement. expected_value is only the priority "
        "enum; move any descriptive text from "
        "expected_value into expected_improvement. Remove every cross-requirement "
        "Evidence ID; if that leaves a supported item without evidence, downgrade it "
        "and provide a requirement-scoped gap and next action.\n"
        f"{binding_guidance}\n"
        f"Validation error: {error}\nInvalid response:\n{content[:12000]}"
    )


def reconcile_strategy_attempt_outcomes(
    attempts: Iterable[StrategyAttempt],
    coverage: Iterable[RequirementCoverage],
) -> tuple[StrategyAttempt, ...]:
    """Count tool output as progress only when validated coverage cites it."""
    cited_by_requirement = {item.requirement_id: set(item.evidence_ids) for item in coverage}
    reconciled: list[StrategyAttempt] = []
    for attempt in attempts:
        cited = cited_by_requirement.get(attempt.requirement_id, set())
        made_progress = bool(cited.intersection(attempt.evidence_ids))
        reconciled.append(
            StrategyAttempt(
                requirement_id=attempt.requirement_id,
                strategy=attempt.strategy,
                query=attempt.query,
                outcome="evidence_found" if made_progress else "no_progress",
                evidence_ids=attempt.evidence_ids,
                action_id=attempt.action_id,
                artifact_ids=attempt.artifact_ids,
            )
        )
    return tuple(reconciled)


def active_next_actions(
    actions: Iterable[NextResearchAction],
) -> tuple[NextResearchAction, ...]:
    """Activate one prioritized action so real tool calls remain traceable."""
    return tuple(actions)[:1]


def merge_next_action_queue(
    previous_actions: Iterable[NextResearchAction],
    assessment: ResearchAssessment,
    *,
    active_consumed: bool,
) -> tuple[NextResearchAction, ...]:
    """Preserve unexecuted actions while applying a local assessment update."""
    if assessment.decision == ResearchDecision.STOP_RESEARCH:
        return ()

    gap_ids = {item.requirement_id for item in assessment.critical_gaps}
    pending = list(previous_actions)
    if active_consumed and pending:
        pending = pending[1:]
    pending_candidates = [
        action for action in pending if action.requirement_id in gap_ids and action.expected_value in {"high", "medium"}
    ]
    pending = []
    pending_requirements: set[str] = set()
    for action in pending_candidates:
        if action.requirement_id not in pending_requirements:
            pending_requirements.add(action.requirement_id)
            pending.append(action)

    proposed_candidates = [
        action
        for action in assessment.next_actions
        if action.requirement_id in gap_ids and action.expected_value in {"high", "medium"}
    ]
    proposed = []
    proposed_requirements: set[str] = set()
    for action in proposed_candidates:
        if action.requirement_id not in proposed_requirements:
            proposed_requirements.add(action.requirement_id)
            proposed.append(action)
    if assessment.decision == ResearchDecision.REPLAN:
        pending = [action for action in pending if action.requirement_id not in proposed_requirements]

    merged = list(pending)
    scheduled_requirements = {action.requirement_id for action in merged}
    for action in proposed:
        if action.requirement_id not in scheduled_requirements:
            scheduled_requirements.add(action.requirement_id)
            merged.append(action)
    return tuple(merged)


def control_message(
    assessment: ResearchAssessment,
    *,
    scheduled_actions: Iterable[NextResearchAction] | None = None,
) -> str:
    actions = assessment.next_actions if scheduled_actions is None else tuple(scheduled_actions)
    action_payload = [item.__dict__ for item in active_next_actions(actions)]
    gap_payload = [item.__dict__ for item in assessment.critical_gaps]
    return (
        "RESEARCH_STATE_DECISION\n"
        f"Decision: {assessment.decision.value}.\n"
        f"Critical gaps: {json.dumps(gap_payload, ensure_ascii=False)}\n"
        f"Active next action: {json.dumps(action_payload, ensure_ascii=False)}\n"
        f"Queued action count: {max(0, len(actions) - 1)}.\n"
        "Keep tools available and execute only the single active action in this loop. "
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

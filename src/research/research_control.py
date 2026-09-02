"""Explicit control decisions for the homogeneous recursive AgentGraph."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from .fork_policy import parse_fork_candidates
from .models import ForkCandidate, ForkReason, ResearchRequirement


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class ResearchControlAction(str, Enum):
    """One semantic control choice made by the same homogeneous Agent."""

    FORK = "fork"
    LOCAL_RESEARCH = "local_research"
    MERGE = "merge"
    COMPLETE = "complete"


@dataclass(frozen=True)
class HomogeneousForkConfig:
    """Opt-in controls for explicit decisions and refundable child leases."""

    enabled: bool = False
    explicit_control_decision: bool = False
    budget_leases_enabled: bool = False
    # Deprecated compatibility field.  Recursive Fork no longer has a local
    # tool-call prerequisite; a restored non-zero value is intentionally ignored.
    recursive_fork_min_local_tool_calls: int = 0
    reconsider_after_local_rounds: int = 2
    parent_merge_reserve_tokens: int = 50000
    root_final_max_tokens: int = 32768
    root_final_output_token_budget: int = 50000
    initial_child_lease_tokens: int = 60000
    child_topup_tokens: int = 25000
    max_child_lease_tokens: int = 125000

    def validate(self) -> None:
        for key in ("enabled", "explicit_control_decision", "budget_leases_enabled"):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"research.homogeneous_fork.{key} must be a boolean")
        for key in (
            "reconsider_after_local_rounds",
            "recursive_fork_min_local_tool_calls",
            "parent_merge_reserve_tokens",
            "root_final_max_tokens",
            "root_final_output_token_budget",
            "initial_child_lease_tokens",
            "child_topup_tokens",
            "max_child_lease_tokens",
        ):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"research.homogeneous_fork.{key} must be a non-negative integer"
                )
        if self.reconsider_after_local_rounds < 1:
            raise ValueError(
                "research.homogeneous_fork.reconsider_after_local_rounds must be at least 1"
            )
        if self.root_final_max_tokens < 1024:
            raise ValueError(
                "research.homogeneous_fork.root_final_max_tokens must be at least 1024"
            )
        if self.root_final_output_token_budget < self.root_final_max_tokens:
            raise ValueError(
                "root_final_output_token_budget cannot be smaller than root_final_max_tokens"
            )
        if self.enabled and not self.explicit_control_decision:
            raise ValueError(
                "enabled homogeneous Fork requires explicit_control_decision=true"
            )
        if self.budget_leases_enabled and not self.enabled:
            raise ValueError(
                "budget_leases_enabled requires homogeneous Fork enabled=true"
            )
        if self.initial_child_lease_tokens > self.max_child_lease_tokens:
            raise ValueError(
                "initial_child_lease_tokens cannot exceed max_child_lease_tokens"
            )


@dataclass(frozen=True)
class ResearchControlDecision:
    action: ResearchControlAction
    rationale: str
    target_requirement_ids: tuple[str, ...] = ()
    fork_candidates: tuple[ForkCandidate, ...] = ()


def initial_root_requirement_decision(
    requirements: Iterable[ResearchRequirement],
    *,
    max_children: int,
    remaining_total_agent_slots: int | None,
) -> ResearchControlDecision | None:
    """Build the stable one-owner-per-requirement r9b coverage wave."""

    external = tuple(
        item
        for item in requirements
        if item.required and item.requires_external_evidence
    )
    available = max_children
    if remaining_total_agent_slots is not None:
        available = min(available, max(0, int(remaining_total_agent_slots)))
    if len(external) < 2 or available < len(external):
        return None
    candidates = tuple(
        ForkCandidate(
            objective=item.description,
            expected_output=(
                "Opened, source-locatable Evidence and verified findings for "
                f"requirement {item.requirement_id}."
            ),
            requirement_ids=(item.requirement_id,),
            scope_signature=f"requirement:{item.requirement_id}",
            context={
                "coverage_unit": item.requirement_id,
                "initial_root_wave": True,
            },
            reasons=(ForkReason.PARALLEL,),
            estimated_tool_calls=2,
            independent=True,
        )
        for item in external
    )
    return ResearchControlDecision(
        ResearchControlAction.FORK,
        (
            "Initial root coverage wave assigns every independent external-"
            "evidence Requirement to one homogeneous Child."
        ),
        tuple(item.requirement_id for item in external),
        candidates,
    )


def should_scout_before_fork(
    decision: ResearchControlDecision,
    *,
    depth: int,
    has_research_basis: bool,
) -> bool:
    """Require evidence-aware recursive Fork without a tool-count gate."""

    if (
        decision.action is not ResearchControlAction.FORK
        or depth <= 0
        or has_research_basis
    ):
        return False
    return not all(
        ForkReason.DEEP_TOOL_CHAIN in item.reasons
        and item.estimated_tool_calls >= 3
        for item in decision.fork_candidates
    )


def _json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    match = _JSON_FENCE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("research control decision is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("research control decision must be a JSON object")
    return value


def parse_research_control_decision(
    content: str,
    *,
    valid_requirement_ids: Iterable[str],
) -> ResearchControlDecision:
    """Validate one explicit decision without changing existing Fork reasons."""

    payload = _json_object(content)
    allowed_fields = {
        "action",
        "rationale",
        "target_requirement_ids",
        "fork_candidates",
    }
    unknown = set(payload) - allowed_fields
    if unknown:
        raise ValueError(
            "unknown research control fields: " + ", ".join(sorted(unknown))
        )
    try:
        action = ResearchControlAction(str(payload.get("action") or ""))
    except ValueError as exc:
        raise ValueError("research control action is invalid") from exc
    rationale = str(payload.get("rationale") or "").strip()
    if not rationale:
        raise ValueError("research control rationale is required")
    raw_targets = payload.get("target_requirement_ids", [])
    if not isinstance(raw_targets, list):
        raise ValueError("target_requirement_ids must be a list")
    targets = tuple(
        dict.fromkeys(str(item).strip() for item in raw_targets if str(item).strip())
    )
    valid = set(str(item).strip() for item in valid_requirement_ids)
    unknown_targets = set(targets) - valid
    if unknown_targets:
        raise ValueError(
            "research control references unknown requirements: "
            + ", ".join(sorted(unknown_targets))
        )
    raw_candidates = payload.get("fork_candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("fork_candidates must be a list")
    candidates = tuple(parse_fork_candidates({"candidates": raw_candidates}))
    if action is ResearchControlAction.FORK:
        if not candidates:
            raise ValueError("fork control decision requires candidates")
        if any(not item.reasons for item in candidates):
            raise ValueError("every Fork candidate requires an approved reason")
        candidate_targets = {
            requirement_id
            for candidate in candidates
            for requirement_id in candidate.requirement_ids
        }
        if set(targets) != candidate_targets:
            raise ValueError(
                "fork target requirements must exactly match candidate assignments"
            )
    elif candidates:
        raise ValueError("non-fork control decision cannot include Fork candidates")
    return ResearchControlDecision(action, rationale, targets, candidates)


def research_control_prompt(
    *,
    objective: str,
    requirements: Iterable[ResearchRequirement],
    coverage: Iterable[Mapping[str, Any]],
    child_summaries: Iterable[Mapping[str, Any]],
    board_view: Mapping[str, Any] | None,
    depth: int,
    max_fork_depth: int,
    max_children: int,
    local_rounds_since_fork: int,
    reconsider_after_local_rounds: int = 2,
    remaining_total_agent_slots: int | None = None,
    delegable_token_budget: int | None = None,
    fork_evidence_basis: bool = False,
) -> list[dict[str, str]]:
    """Ask the same Agent to explicitly consider Fork before using tools."""

    payload = {
        "objective": objective,
        "requirements": [
            {
                "requirement_id": item.requirement_id,
                "description": item.description,
                "required": item.required,
                "requires_external_evidence": item.requires_external_evidence,
            }
            for item in requirements
        ],
        "coverage": list(coverage),
        "child_results": list(child_summaries),
        "coordination_board": dict(board_view or {}),
        "depth": depth,
        "max_fork_depth": max_fork_depth,
        "remaining_child_slots": max_children,
        "remaining_total_agent_slots": remaining_total_agent_slots,
        "delegable_token_budget": delegable_token_budget,
        "fork_evidence_basis": bool(fork_evidence_basis),
        "local_rounds_since_fork": local_rounds_since_fork,
        "fork_reconsideration_due": (
            local_rounds_since_fork >= reconsider_after_local_rounds
        ),
    }
    return [
        {
            "role": "system",
            "content": (
                "Choose the next control action for this homogeneous Research Agent. "
                "This does not force Fork, but you must explicitly decide before research. "
                "Allowed actions: fork, local_research, merge, complete.\n\n"
                "You own the semantic Fork decision: inspect the plan, your assignment, "
                "sibling assignments, existing queries/sources/Evidence, current gaps, "
                "and queue capacity. Decide whether scopes are meaningfully distinct; "
                "the runtime only enforces exact fingerprints and hard limits. At depth "
                "0, independent required external-evidence Requirements must retain "
                "separate Child ownership when capacity permits; you must not combine "
                "independent external-evidence Requirements into a smaller batch. "
                "Requirements with "
                "requires_external_evidence=false are synthesis work for the Root "
                "and must not be delegated as external evidence research.\n\n"
                "A Requirement is a coverage unit, not the smallest execution unit. "
                "One Requirement may own several semantically distinct Child or "
                "Grandchild Assignments, so 'single requirement', 'focused requirement', "
                "or depth alone is not a sufficient reason to avoid Fork. At depth 1, "
                "parallel or context-isolation Fork requires an observed research basis: "
                "a completed/failed query, opened/failed source, or collected Evidence "
                "owned by this Assignment. Without that basis, scout locally first. A "
                "concrete deep_tool_chain of at least three calls may Fork immediately. "
                "Every Grandchild must stay within one Requirement and name a concrete "
                "clause, jurisdiction, primary document, or tool chain with a precise "
                "non-overlapping scope_signature. Never Fork when remaining "
                "total-agent or delegable-token capacity is insufficient, and never "
                "repeat a sibling's queued, active, or completed scope.\n\n"
                "Fork retains exactly three approved strategies:\n"
                f"- {ForkReason.PARALLEL.value}: at least two independent tasks can run in parallel;\n"
                f"- {ForkReason.CONTEXT_ISOLATION.value}: substantial intermediate material needs isolation;\n"
                f"- {ForkReason.DEEP_TOOL_CHAIN.value}: the child requires at least three tool calls.\n\n"
                "When fork_reconsideration_due is true, reconsider Fork explicitly, "
                "but you may still choose local_research with a concrete reason.\n\n"
                "Return exactly one JSON object with action, rationale, "
                "target_requirement_ids, and fork_candidates. Each Fork candidate uses "
                "objective, expected_output, requirement_ids, scope_signature, context, reasons, "
                "estimated_tool_calls, independent. For non-fork actions return an empty "
                "fork_candidates list. Do not call tools or perform research in this step."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def decision_as_fork_tool_call(
    decision: ResearchControlDecision,
    *,
    call_id: str,
) -> dict[str, Any]:
    if decision.action is not ResearchControlAction.FORK:
        raise ValueError("only a Fork decision can become a fork_research call")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "fork_research",
            "arguments": json.dumps(
                {
                    "candidates": [
                        {
                            "objective": item.objective,
                            "expected_output": item.expected_output,
                            "requirement_ids": list(item.requirement_ids),
                            "scope_signature": item.scope_signature,
                            "context": item.context,
                            "reasons": [reason.value for reason in item.reasons],
                            "estimated_tool_calls": item.estimated_tool_calls,
                            "independent": item.independent,
                        }
                        for item in decision.fork_candidates
                    ]
                },
                ensure_ascii=False,
            ),
        },
    }


__all__ = [
    "HomogeneousForkConfig",
    "ResearchControlAction",
    "ResearchControlDecision",
    "decision_as_fork_tool_call",
    "initial_root_requirement_decision",
    "parse_research_control_decision",
    "research_control_prompt",
    "should_scout_before_fork",
]

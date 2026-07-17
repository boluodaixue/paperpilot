"""N3's small deterministic fork gate; it is not a controller or service."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .models import ExecutionIdentity, ForkCandidate, ForkReason, ResearchTask


FORK_TOOL_NAME = "fork_research"


def fork_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": FORK_TOOL_NAME,
            "description": (
                "Delegate scoped work to homogeneous Research Agents only when tasks can "
                "run independently, context must be isolated, or the expected tool chain "
                "requires at least three calls. Use one control action at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "objective": {"type": "string"},
                                "expected_output": {"type": "string"},
                                "context": {"type": "object"},
                                "reasons": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": [reason.value for reason in ForkReason],
                                    },
                                },
                                "estimated_tool_calls": {
                                    "type": "integer",
                                    "minimum": 0,
                                },
                                "independent": {"type": "boolean"},
                            },
                            "required": ["objective", "expected_output", "reasons"],
                        },
                        "minItems": 1,
                    }
                },
                "required": ["candidates"],
            },
        },
    }


def parse_fork_candidates(arguments: Any) -> list[ForkCandidate]:
    if isinstance(arguments, str):
        try:
            payload = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return []
    elif isinstance(arguments, dict):
        payload = arguments
    else:
        return []
    if not isinstance(payload, dict):
        return []

    candidates: list[ForkCandidate] = []
    for raw in payload.get("candidates", []):
        if not isinstance(raw, dict):
            continue
        reasons: list[ForkReason] = []
        for value in raw.get("reasons", []):
            try:
                reason = ForkReason(str(value))
            except ValueError:
                continue
            if reason not in reasons:
                reasons.append(reason)
        context = raw.get("context", {})
        try:
            estimated_tool_calls = max(0, int(raw.get("estimated_tool_calls") or 0))
        except (TypeError, ValueError):
            estimated_tool_calls = 0
        candidates.append(
            ForkCandidate(
                objective=str(raw.get("objective") or "").strip(),
                expected_output=str(raw.get("expected_output") or "").strip(),
                context=context if isinstance(context, dict) else {},
                reasons=tuple(reasons),
                estimated_tool_calls=estimated_tool_calls,
                independent=bool(raw.get("independent", True)),
            )
        )
    return candidates


def candidate_fingerprint(candidate: ForkCandidate) -> str:
    normalized = {
        "objective": " ".join(candidate.objective.lower().split()),
        "expected_output": " ".join(candidate.expected_output.lower().split()),
        "context": candidate.context,
    }
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_fork_candidates(
    candidates: Iterable[ForkCandidate],
    *,
    parent_task: ResearchTask,
    identity: ExecutionIdentity,
    max_fork_depth: int,
    max_children: int,
    completed_fingerprints: Iterable[str] = (),
    ancestor_objectives: Iterable[str] = (),
) -> tuple[list[ForkCandidate], list[str]]:
    """Return accepted candidates and human-readable deterministic rejections."""
    proposals = list(candidates)
    rejected: list[str] = []
    if identity.depth >= max_fork_depth:
        return [], ["fork depth limit reached"]
    if max_children <= 0:
        return [], ["child budget exhausted"]

    seen = set(completed_fingerprints)
    parent_objective = " ".join(parent_task.objective.lower().split())
    blocked_objectives = {
        " ".join(str(objective).lower().split())
        for objective in ancestor_objectives
        if str(objective).strip()
    }
    blocked_objectives.add(parent_objective)
    parallel_fingerprints = {
        candidate_fingerprint(item)
        for item in proposals
        if item.objective
        and item.expected_output
        and " ".join(item.objective.lower().split()) not in blocked_objectives
        and candidate_fingerprint(item) not in seen
        and ForkReason.PARALLEL in item.reasons
        and item.independent
    }
    parallel_allowed = len(parallel_fingerprints) >= 2
    accepted: list[ForkCandidate] = []

    for candidate in proposals:
        label = candidate.objective or "<empty objective>"
        if not candidate.objective or not candidate.expected_output:
            rejected.append(f"{label}: task scope is incomplete")
            continue
        if " ".join(candidate.objective.lower().split()) in blocked_objectives:
            rejected.append(f"{label}: duplicates an ancestor task")
            continue
        fingerprint = candidate_fingerprint(candidate)
        if fingerprint in seen:
            rejected.append(f"{label}: duplicate task")
            continue

        valid_parallel = (
            ForkReason.PARALLEL in candidate.reasons
            and candidate.independent
            and parallel_allowed
        )
        valid_isolation = ForkReason.CONTEXT_ISOLATION in candidate.reasons
        valid_depth = (
            ForkReason.DEEP_TOOL_CHAIN in candidate.reasons
            and candidate.estimated_tool_calls >= 3
        )
        if not (valid_parallel or valid_isolation or valid_depth):
            rejected.append(f"{label}: no approved fork condition was satisfied")
            continue
        if len(accepted) >= max_children:
            rejected.append(f"{label}: child budget exhausted")
            continue

        accepted.append(candidate)
        seen.add(fingerprint)

    return accepted, rejected

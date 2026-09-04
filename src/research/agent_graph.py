"""The checkpointed LangGraph implementation shared by every Agent level."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ..tools.file_reader import FileReaderError
from ..tools.evidence_acquisition import canonicalize_source_url
from ..tools.notepad import NotepadTool
from ..utils.tracing import trace_block, trace_context
from .context_compaction import (
    ContextCompactionError,
    collapse_verified_working_context,
    context_consolidation_manifest,
    deterministic_context_cleanup,
    semantic_memo_messages,
    validate_semantic_memo,
)
from .agent_scheduler import (
    FairAgentScheduler,
    ScheduledAgent,
    ScheduledAgentCancelled,
)
from .fork_policy import (
    FORK_TOOL_NAME,
    candidate_fingerprint,
    evaluate_fork_candidates,
    fork_tool_schema,
    parse_fork_candidates,
)
from .models import (
    AgentLimits,
    CriticalGap,
    EvidenceItem,
    ExecutionIdentity,
    ForkCandidate,
    NextResearchAction,
    OutputStatus,
    RequirementCoverage,
    RequirementStatus,
    ResearchDecision,
    ResearchRequirement,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
    StrategyAttempt,
    TerminationReason,
)
from .policy import call_policy
from .research_blackboard import ResearchBlackboard, normalize_scope_signature
from .research_control import (
    HomogeneousForkConfig,
    ResearchControlAction,
    ResearchControlDecision,
    decision_as_fork_tool_call,
    initial_root_requirement_decision,
    parse_research_control_decision,
    research_control_prompt,
    should_scout_before_fork,
)
from .research_sufficiency import (
    AssessmentValidationError,
    ResearchAssessment,
    active_next_actions,
    aggregate_strategy_attempts,
    assessment_schema_prompt,
    build_research_requirements,
    control_message,
    finalization_prompt,
    hard_termination_reason,
    initial_coverage,
    merge_child_coverage_evidence,
    merge_next_action_queue,
    parse_json_object,
    parse_research_assessment,
    reconcile_strategy_attempt_outcomes,
    repair_assessment_prompt,
    repair_final_prompt,
    stable_action_id,
    unattempted_actions,
)
from .tool_availability import (
    availability_alert_from_event,
    classify_fallback_backend_alerts,
    classify_tool_availability,
)

_TOOL_ARTIFACT_OFFLOAD_CHARS = 4000
_CROSS_SCOPE_SIGNAL_TOOL_NAME = "signal_cross_scope"
_BLACKBOARD_LEASE_SECONDS = 300.0
_EVIDENCE_MARKER = re.compile(r"\[\[EVIDENCE:([A-Za-z0-9._-]+)\]\]")


__all__ = [
    "ResearchAgentState",
    "build_research_agent_graph",
    "create_research_agent_state",
    "run_research_agent",
]


class ResearchAgentState(TypedDict):
    """Only fields read or written by the homogeneous Research AgentGraph."""

    task: ResearchTask
    identity: ExecutionIdentity
    limits: AgentLimits
    messages: list[dict[str, Any]]
    notepad_entries: list[dict[str, Any]]
    iteration: int
    tool_calls_used: int
    pending_tool_calls: list[dict[str, Any]]
    pending_fork_calls: list[dict[str, Any]]
    pending_stop_reason: str | None
    completed_fork_fingerprints: list[str]
    child_thread_ids: list[str]
    child_results: list[ResearchResult]
    observed_evidence: list[EvidenceItem]
    deadline_at: float
    subtree_thread_budget: int
    subtree_tool_budget: int
    subtree_token_budget: int
    subtree_retry_budget: int
    total_threads_used: int
    total_tool_calls_used: int
    estimated_tokens_used: int
    retries_used: int
    source_candidate_count: int
    source_open_count: int
    duplicate_source_count: int
    acquisition_call_count: int
    execution_events: list[dict[str, Any]]
    compression_manifests: list[dict[str, Any]]
    semantic_memos: list[dict[str, Any]]
    lineage_objectives: list[str]
    draft: dict[str, Any] | None
    draft_raw: str
    last_content: str
    last_assessed_evidence_count: int
    last_assessed_strategy_attempt_count: int
    research_requirements: list[ResearchRequirement]
    coverage: list[RequirementCoverage]
    critical_gaps: list[CriticalGap]
    next_actions: list[NextResearchAction]
    strategy_attempts: list[StrategyAttempt]
    assessment_decision: ResearchDecision | None
    assessment_output_status: OutputStatus
    assessment_error: str | None
    termination_reason: TerminationReason | None
    finalization_requested: bool
    output_status: OutputStatus
    stop_reason: str | None
    result: ResearchResult | None
    control_action: str | None
    control_rationale: str | None
    control_target_requirement_ids: list[str]
    control_decision_error: str | None
    control_repair_applied: bool
    local_rounds_since_fork: int
    own_assignment_id: str
    parent_assignment_id: str


def _owned_requirement_ids(state: ResearchAgentState) -> tuple[str, ...]:
    context = state["task"].context if isinstance(state["task"].context, dict) else {}
    inherited = context.get("parent_requirement_ids")
    if isinstance(inherited, (list, tuple)):
        values = tuple(dict.fromkeys(str(item).strip() for item in inherited if str(item).strip()))
        if values:
            return values
    return tuple(
        item.requirement_id
        for item in state.get("research_requirements", [])
        if item.required
    )


def _root_assignment_id(identity: ExecutionIdentity) -> str:
    digest = hashlib.sha256(identity.root_thread_id.encode("utf-8")).hexdigest()[:16]
    return f"assignment-root-{digest}"


def _own_assignment_id(state: ResearchAgentState) -> str:
    value = str(state.get("own_assignment_id") or "").strip()
    if value:
        return value
    context = state["task"].context if isinstance(state["task"].context, dict) else {}
    return str(context.get("own_assignment_id") or "").strip() or _root_assignment_id(
        state["identity"]
    )


def _parent_assignment_id(state: ResearchAgentState) -> str:
    value = str(state.get("parent_assignment_id") or "").strip()
    if value:
        return value
    context = state["task"].context if isinstance(state["task"].context, dict) else {}
    return str(context.get("parent_assignment_id") or "").strip()


def _has_owned_research_basis(
    state: ResearchAgentState,
    board_view: dict[str, Any] | None,
) -> bool:
    """Return whether this Assignment has observed a concrete research signal."""

    if state.get("observed_evidence"):
        return True
    if not board_view:
        return int(state.get("tool_calls_used", 0)) > 0
    thread_id = state["identity"].thread_id
    assignment_id = _own_assignment_id(state)
    for key in ("recent_queries", "recent_sources", "recent_evidence"):
        for item in board_view.get(key, []):
            if not isinstance(item, dict):
                continue
            if (
                item.get("owner_thread_id") == thread_id
                or item.get("assignment_id") == assignment_id
            ):
                return True
    return False


def _candidate_scope_signature(candidate: ForkCandidate) -> str:
    return normalize_scope_signature(candidate.scope_signature, candidate.objective)


def _candidate_assignment_id(
    parent_assignment_id: str,
    candidate: ForkCandidate,
) -> str:
    payload = json.dumps(
        {
            "parent_assignment_id": parent_assignment_id,
            "requirement_ids": sorted(candidate.requirement_ids),
            "objective": normalize_scope_signature(candidate.objective),
            "scope_signature": _candidate_scope_signature(candidate),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "assignment-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _cross_scope_signal_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _CROSS_SCOPE_SIGNAL_TOOL_NAME,
            "description": (
                "Publish already-collected Evidence that is useful to a sibling requirement. "
                "Do not start new research outside your assignment; signal the Evidence ID "
                "and target requirement so its owner can reuse it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "target_requirement_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["evidence_id", "target_requirement_id"],
            },
        },
    }


_FINALIZATION_OUTPUT_TOKEN_RESERVE = 12000
_ASSESSMENT_OUTPUT_TOKEN_RESERVE = 12000
_ASSESSMENT_INPUT_TOKEN_RESERVE = 10000
_ROOT_FINALIZATION_TOKEN_MINIMUM = 40000
_CHILD_FINALIZATION_TOKEN_MINIMUM = 8000
_FINALIZATION_TOKEN_RESERVE_PERCENT = 15
_FINALIZATION_COMPLEXITY_TOKEN_CAP = 20000
_FINALIZATION_TIME_RESERVE_FRACTION = 0.1
_MIN_FORK_RESEARCH_TOOL_CALLS = 1
_MIN_FORK_RESEARCH_TOKEN_BUDGET = (
    _ASSESSMENT_INPUT_TOKEN_RESERVE
    + _ASSESSMENT_OUTPUT_TOKEN_RESERVE
    + _FINALIZATION_OUTPUT_TOKEN_RESERVE
)


def _event(
    kind: str,
    identity: ExecutionIdentity,
    **details: Any,
) -> dict[str, Any]:
    return {
        "kind": kind,
        **_identity_metadata(identity),
        **details,
    }


def _remaining_seconds(state: ResearchAgentState) -> float:
    return state["deadline_at"] - time.time()


def _remaining_finalization_seconds(state: ResearchAgentState) -> float:
    """Give only the Root an additional post-research synthesis window."""
    grace = (
        state["limits"].root_finalization_grace_seconds
        if state["identity"].depth == 0
        else 0.0
    )
    return _remaining_seconds(state) + grace


def _remaining_research_seconds(state: ResearchAgentState) -> float:
    """Let Root use the full research budget while descendants return earlier."""
    per_level_reserve = state["limits"].max_elapsed_seconds * _FINALIZATION_TIME_RESERVE_FRACTION
    # Preserve the legacy in-budget Root reserve when no separate grace window
    # is configured.  An explicit grace window makes the research deadline a
    # true research-only budget instead of silently shortening it.
    root_reserve = (
        0.0
        if state["limits"].root_finalization_grace_seconds > 0
        else per_level_reserve
    )
    reserve = root_reserve + per_level_reserve * state["identity"].depth
    return _remaining_seconds(state) - reserve


def _child_research_seconds_after_fork(state: ResearchAgentState) -> float:
    """Return research time a newly dispatched child would actually receive."""

    per_level_reserve = (
        state["limits"].max_elapsed_seconds
        * _FINALIZATION_TIME_RESERVE_FRACTION
    )
    root_reserve = (
        0.0
        if state["limits"].root_finalization_grace_seconds > 0
        else per_level_reserve
    )
    child_depth = state["identity"].depth + 1
    return _remaining_seconds(state) - root_reserve - per_level_reserve * child_depth


def _minimum_fork_token_budget(fork_settings: HomogeneousForkConfig) -> int:
    """Keep enough tokens for control, acquisition/reading, and a scoped memo."""

    if not fork_settings.budget_leases_enabled:
        return _MIN_FORK_RESEARCH_TOKEN_BUDGET
    return max(_MIN_FORK_RESEARCH_TOKEN_BUDGET, fork_settings.initial_child_lease_tokens)


def _fork_resource_allocations(
    state: ResearchAgentState,
    candidates: list[ForkCandidate],
    *,
    fork_settings: HomogeneousForkConfig,
    check_token_budget: bool,
) -> tuple[list[ForkCandidate], list[int], list[int], list[ForkCandidate]]:
    """Select the largest stable prefix able to complete one research loop.

    The normal equal-split allocation is preserved whenever it is sufficient.
    When it is not, lower-priority candidates stay with the current Agent rather
    than becoming zero-output child Assignments.
    """

    if not candidates:
        return [], [], [], []
    minimum_seconds = (
        state["limits"].max_elapsed_seconds * _FINALIZATION_TIME_RESERVE_FRACTION
    )
    if _child_research_seconds_after_fork(state) < minimum_seconds:
        return [], [], [], list(candidates)

    delegable_tokens = _delegable_token_budget(state)
    minimum_tokens = _minimum_fork_token_budget(fork_settings)
    for count in range(len(candidates), 0, -1):
        selected = candidates[:count]
        tool_budgets = _split_budget(
            _delegable_tool_budget(state, count),
            count,
        )
        if any(
            allocated
            < max(
                _MIN_FORK_RESEARCH_TOOL_CALLS,
                candidate.estimated_tool_calls,
            )
            for candidate, allocated in zip(selected, tool_budgets)
        ):
            continue
        token_budgets = (
            _split_budget(delegable_tokens, count)
            if check_token_budget
            else [minimum_tokens] * count
        )
        if check_token_budget and any(
            allocated < minimum_tokens for allocated in token_budgets
        ):
            continue
        return (
            selected,
            tool_budgets,
            token_budgets,
            candidates[count:],
        )
    return [], [], [], list(candidates)


def _fundable_child_count(
    state: ResearchAgentState,
    max_children: int,
    *,
    fork_settings: HomogeneousForkConfig,
    available_lease_tokens: int | None = None,
) -> int:
    """Return how many minimally complete children can be funded right now."""

    if max_children <= 0:
        return 0
    minimum_seconds = (
        state["limits"].max_elapsed_seconds * _FINALIZATION_TIME_RESERVE_FRACTION
    )
    if _child_research_seconds_after_fork(state) < minimum_seconds:
        return 0
    token_pool = (
        max(0, int(available_lease_tokens or 0))
        if fork_settings.budget_leases_enabled
        else _delegable_token_budget(state)
    )
    token_capacity = token_pool // _minimum_fork_token_budget(fork_settings)
    for count in range(min(max_children, token_capacity), 0, -1):
        tool_budgets = _split_budget(
            _delegable_tool_budget(state, count),
            count,
        )
        if all(item >= _MIN_FORK_RESEARCH_TOOL_CALLS for item in tool_budgets):
            return count
    return 0


def _remaining_child_slots(state: ResearchAgentState) -> int:
    """Count only real direct Child results against this Agent's limit."""

    return max(
        0,
        state["limits"].effective_max_children_per_agent
        - len(state.get("child_results", [])),
    )


def _finalization_token_reserve(state: ResearchAgentState) -> int:
    """Hard token reserve that ordinary research cannot consume.

    Root synthesis needs enough room to read a compact evidence matrix and
    produce the user-facing report. Child agents keep a smaller independent
    reserve so they can still return a useful memo to the parent.
    """
    subtree_budget = max(0, state["subtree_token_budget"])
    minimum = (
        _ROOT_FINALIZATION_TOKEN_MINIMUM
        if state["identity"].depth == 0
        else _CHILD_FINALIZATION_TOKEN_MINIMUM
    )
    base = max(
        minimum,
        subtree_budget * _FINALIZATION_TOKEN_RESERVE_PERCENT // 100,
    )
    complexity = min(
        _FINALIZATION_COMPLEXITY_TOKEN_CAP,
        150 * len(state.get("observed_evidence", []))
        + 100 * len(state.get("strategy_attempts", []))
        + 500 * len(state.get("child_results", [])),
    )
    return min(subtree_budget, base + complexity)


def _delegable_token_budget(state: ResearchAgentState) -> int:
    """Retain enough subtree tokens for parent assessment and final synthesis."""
    remaining = max(
        0,
        state["subtree_token_budget"] - state["estimated_tokens_used"],
    )
    fixed_reserve = (
        _ASSESSMENT_INPUT_TOKEN_RESERVE + _ASSESSMENT_OUTPUT_TOKEN_RESERVE + _FINALIZATION_OUTPUT_TOKEN_RESERVE
    )
    state_sensitive_reserve = (
        4000
        + 450 * len(state.get("observed_evidence", []))
        + 250 * len(state.get("strategy_attempts", []))
        + 750 * len(state.get("child_results", []))
        + _ASSESSMENT_OUTPUT_TOKEN_RESERVE
        + _FINALIZATION_OUTPUT_TOKEN_RESERVE
    )
    desired_reserve = max(
        fixed_reserve,
        _finalization_token_reserve(state),
        min(60000, state_sensitive_reserve),
    )
    # This is a floor against the original subtree allocation, not a fraction
    # of the currently remaining tokens. Otherwise several sequential fork
    # batches can repeatedly delegate half of the previous "reserve" until the
    # parent has no capacity left to assess and synthesize child results.
    return max(0, remaining - desired_reserve)


def _delegable_tool_budget(
    state: ResearchAgentState,
    child_count: int,
) -> int:
    """Give the parent one fair budget share for post-fork gap resolution."""
    remaining = max(
        0,
        state["subtree_tool_budget"] - state["total_tool_calls_used"],
    )
    if child_count <= 0:
        return 0
    return (remaining * child_count) // (child_count + 1)


def _estimate_tokens(messages: list[dict[str, Any]], response: dict[str, Any]) -> int:
    usage = response.get("usage")
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if isinstance(usage, dict):
        raw_total = usage.get("total_tokens")
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            return total
    input_chars = sum(len(str(message.get("content") or "")) for message in messages)
    output_chars = len(str(response.get("content") or ""))
    output_chars += len(json.dumps(response.get("tool_calls") or [], default=str))
    return max(1, (input_chars + output_chars + 3) // 4)


def _completion_tokens(response: dict[str, Any]) -> int:
    usage = response.get("usage")
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if isinstance(usage, dict):
        raw_completion = usage.get("completion_tokens")
        try:
            completion = int(raw_completion)
        except (TypeError, ValueError):
            completion = 0
        if completion > 0:
            return completion
    content = str(response.get("content") or "")
    return max(1, (len(content) + 1) // 2)


def _identity_metadata(identity: ExecutionIdentity) -> dict[str, Any]:
    return {
        "thread_id": identity.thread_id,
        "parent_thread_id": identity.parent_thread_id,
        "root_thread_id": identity.root_thread_id,
        "depth": identity.depth,
    }


def _validate_invocation(
    state: ResearchAgentState,
    config: RunnableConfig,
) -> None:
    task = state["task"]
    identity = state["identity"]
    limits = state["limits"]
    task.validate()
    identity.validate()
    limits.validate()
    checkpoint_thread_id = config.get("configurable", {}).get("thread_id")
    if checkpoint_thread_id != identity.thread_id:
        raise ValueError("LangGraph configurable.thread_id must match identity.thread_id")


@contextmanager
def _node_trace(name: str, state: ResearchAgentState):
    identity = state["identity"]
    metadata = _identity_metadata(identity)
    with trace_context(
        session_id=identity.root_thread_id,
        trace_name="paperpilot.research.agent",
        tags=["paperpilot", "research-agent", f"depth-{identity.depth}"],
        metadata=metadata,
    ):
        with trace_block(
            f"research_agent.{name}",
            run_type="agent" if name == "think_and_plan" else "chain",
            inputs=metadata,
            tags=["paperpilot", "research-agent", name],
        ) as observation:
            yield observation


def _system_prompt(identity: ExecutionIdentity, limits: AgentLimits) -> str:
    fork_instruction = (
        "You may call fork_research when at least one approved fork condition is "
        "satisfied and the child tasks are explicitly scoped."
        if identity.depth < limits.max_fork_depth and limits.effective_max_children_per_agent > 0
        else "The fork depth or child budget is closed; continue locally and do not fork."
    )
    final_contract = (
        "When final synthesis is requested, return only the complete Markdown "
        "report, not JSON and not a fenced code block. Synthesize the Child "
        "research memos, preserve important gaps, and cite only known sources "
        "with [[EVIDENCE:evidence-id]] markers. Copy every complete Evidence ID "
        "exactly as supplied, including the evidence- prefix; never shorten it "
        "to a bare hash. Do not write a References section; the runtime renders it."
        if identity.depth == 0
        else
        "When the task is complete, stop calling tools and return one JSON object:\n"
        "{\n"
        '  "status": "completed" | "partial" | "failed",\n'
        '  "summary": "concise synthesis",\n'
        '  "findings": ["atomic finding"],\n'
        '  "unresolved": ["remaining uncertainty"],\n'
        '  "research_memo": "Child-only Markdown memo",\n'
        '  "report_markdown": ""\n'
        "}\n"
        "Write a concise report only about your assigned direction in "
        "research_memo. Select the material you judge useful and cite it with "
        "[[EVIDENCE:evidence-id]] markers. Copy every complete Evidence ID exactly "
        "as supplied, including the evidence- prefix; never shorten it to a bare "
        "hash. Do not attempt the global report or wrap the final JSON in commentary."
    )
    return f"""You are a PaperPilot Research Agent at depth {identity.depth}.
Every Research Agent uses this same research loop. {fork_instruction}

Approved fork conditions are: two or more independent tasks can run in
parallel; a task needs context isolation because it will produce substantial
intermediate material; or a task needs at least three tool calls. Use only one
control action at a time. Planning, local research, child-result gathering, and
summarization are all your own responsibilities.

Work on only the scoped task. Use tools when evidence is required. Search
snippets are leads, not complete proof; preserve source identifiers and
locators. Never invent a source.
When an artifact receipt is present, you may recover its bounded original
content with file_reader root "artifact" and path "<artifact_id>.json".

{final_contract}
After tool or child results, the same policy performs a checkpointed structured
research-state assessment. Continue and Replan instructions keep tools available
and identify requirement-scoped gaps. Stop Research alone enters final synthesis.
There is no separate Planner, Manager, or Summarizer Agent.
"""


def _task_prompt(task: ResearchTask) -> str:
    payload = {
        "task_id": task.task_id,
        "objective": task.objective,
        "context": task.context,
        "expected_output": task.expected_output,
        "constraints": list(task.constraints),
        "require_evidence": task.require_evidence,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _tool_schemas(tools: Iterable[Any]) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for tool in tools:
        availability = getattr(tool, "is_available", None)
        if callable(availability) and availability() is False:
            continue
        factory = getattr(tool, "get_openai_tool_schema", None)
        if not callable(factory):
            raise TypeError(f"tool {tool!r} does not provide get_openai_tool_schema()")
        schema = factory()
        if not isinstance(schema, dict):
            raise TypeError(f"tool {tool!r} returned a non-dict schema")
        schemas.append(schema)
    return schemas


def _build_tool_map(tools: Iterable[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tool in tools:
        name = str(getattr(tool, "name", "")).strip()
        if not name:
            raise TypeError(f"tool {tool!r} does not expose a non-empty name")
        if name in result:
            raise ValueError(f"duplicate tool name: {name}")
        if not callable(getattr(tool, "execute", None)):
            raise TypeError(f"tool {name!r} does not provide execute()")
        result[name] = tool
    return result


def _tool_accepts_empty_arguments(tool: Any) -> bool:
    if tool is None:
        return False
    try:
        schema = tool.get_openai_tool_schema()
        parameters = schema.get("function", {}).get("parameters", {})
    except (AttributeError, TypeError):
        return False
    return isinstance(parameters, dict) and not parameters.get("required")


def _normalize_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_calls or []):
        if isinstance(raw, dict):
            data = raw
        elif hasattr(raw, "model_dump"):
            data = raw.model_dump()
        else:
            continue
        function = data.get("function", {})
        if not isinstance(function, dict) and hasattr(function, "model_dump"):
            function = function.model_dump()
        if not isinstance(function, dict):
            continue
        calls.append(
            {
                "id": str(data.get("id") or f"tool-call-{index}"),
                "type": "function",
                "function": {
                    "name": str(function.get("name", "")),
                    "arguments": function.get("arguments", "{}"),
                },
            }
        )
    return calls


def _action_for_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    actions: Iterable[NextResearchAction],
) -> NextResearchAction | None:
    """Match a proposed action only after a real research-tool call is issued."""
    if tool_name in {"", "notepad", FORK_TOOL_NAME}:
        return None
    candidates = tuple(actions)
    if not candidates:
        return None
    argument_text = " ".join(
        str(value).strip().lower()
        for value in arguments.values()
        if isinstance(value, (str, int, float)) and str(value).strip()
    )
    exact = [
        action
        for action in candidates
        if action.query.strip().lower()
        and (action.query.strip().lower() in argument_text or argument_text in action.query.strip().lower())
    ]
    if len(exact) == 1:
        return exact[0]
    # The control message instructs the next loop to work only on its pending
    # actions. A single pending action can therefore be attributed to a real
    # research-tool call even when the policy rewrites the concrete query.
    if len(candidates) == 1 and argument_text:
        return candidates[0]
    return None


def _action_id(action: NextResearchAction) -> str:
    """Expose the stable action identity used by tool/evidence lineage."""
    return stable_action_id(action)


def _identified_action(action: NextResearchAction) -> NextResearchAction:
    if action.action_id:
        return action
    return NextResearchAction(
        action.requirement_id,
        action.strategy,
        action.query,
        action.expected_value,
        action.expected_improvement,
        _action_id(action),
    )


def _initial_research_actions(
    requirements: Iterable[ResearchRequirement],
) -> tuple[NextResearchAction, ...]:
    """Give the first real tool call an explicit requirement/action lineage."""
    return tuple(
        _identified_action(
            NextResearchAction(
                requirement.requirement_id,
                "other",
                requirement.description,
                "high",
                "Establish the first source-locatable evidence for this requirement.",
            )
        )
        for requirement in requirements
        if requirement.required and requirement.requires_external_evidence
    )


def _tool_artifact_id(
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
) -> str:
    """Identify the exact raw tool result before L1 durable offload is applied."""
    encoded = json.dumps(
        {
            "tool": tool_name,
            "arguments": arguments,
            "result": result,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return "artifact-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _semantic_tool_error(tool_name: str, result: Any) -> str | None:
    """Recognize error payloads returned without a Python exception."""
    if isinstance(result, dict):
        raw_error = result.get("error")
        if raw_error is not None and str(raw_error).strip():
            return str(raw_error).strip()
        status = str(result.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            return str(result.get("message") or status).strip()
    text = str(result or "").strip()
    if tool_name == "browser" and re.match(
        r"^\[Browser (?:Error|Warning)\]",
        text,
        re.IGNORECASE,
    ):
        return text[:1000]
    return None


def _is_relevant_evidence(
    action: NextResearchAction,
    *,
    finding: Any,
    title: Any,
    source_ref: Any,
) -> bool:
    """Gate evidence by controlled action scope and source locatability.

    Search and reader tools already execute the action-bound query. Repeating a
    lexical-overlap test here creates false negatives for synonyms and numerical
    results, so relevance is inherited only from a successfully bound action;
    this gate independently requires a finding and a locatable source.
    """
    return bool(action.requirement_id.strip() and str(source_ref or "").strip() and str(finding or title or "").strip())


def _parse_final_draft(content: str) -> dict[str, Any]:
    payload = parse_json_object(content)
    status = str(payload.get("status") or "").strip().lower()
    if status not in {item.value for item in ResearchStatus}:
        raise AssessmentValidationError("final status is invalid")
    summary = payload.get("summary")
    if not isinstance(summary, str):
        raise AssessmentValidationError("final summary must be a string")
    for field in ("findings", "unresolved"):
        value = payload.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise AssessmentValidationError(f"final {field} must be an array of strings")
    for field in ("research_memo", "report_markdown"):
        value = payload.get(field, "")
        if not isinstance(value, str):
            raise AssessmentValidationError(f"final {field} must be a string")
        payload[field] = value.strip()
    return payload


def _try_parse_final_draft(content: str) -> dict[str, Any] | None:
    try:
        return _parse_final_draft(content)
    except AssessmentValidationError:
        return None


def _root_markdown_draft(
    state: ResearchAgentState,
    content: str,
) -> dict[str, Any] | None:
    """Accept Root Markdown directly while preserving legacy JSON test policies."""
    parsed = _try_parse_final_draft(content)
    if parsed is not None:
        return parsed
    body = str(content or "").strip()
    if not body or body.startswith("Error:"):
        return None
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", body, re.IGNORECASE | re.DOTALL)
    if fenced is not None:
        body = fenced.group(1).strip()
    summary = next(
        (
            block.strip()[:1200]
            for block in re.split(r"\n\s*\n", body)
            if block.strip() and not block.lstrip().startswith(("#", "|", "- ", "* "))
        ),
        "Root completed the final Markdown synthesis.",
    )
    unresolved = [
        item.reason
        for item in state.get("critical_gaps", [])
        if item.reason
    ]
    status = (
        ResearchStatus.PARTIAL.value
        if state.get("stop_reason") or unresolved
        else ResearchStatus.COMPLETED.value
    )
    return {
        "status": status,
        "summary": summary,
        "findings": [],
        "unresolved": unresolved,
        "research_memo": "",
        "report_markdown": body,
    }


def _bounded_finalization_messages(
    state: ResearchAgentState,
) -> list[dict[str, Any]]:
    """Build a compact same-policy snapshot instead of replaying long history."""
    inventory = _deduplicate_evidence(state["observed_evidence"])
    by_id = {item.evidence_id: item for item in inventory}
    preferred_ids: list[str] = []
    for child in state.get("child_results", []):
        preferred_ids.extend(_EVIDENCE_MARKER.findall(child.research_memo))
    for coverage in state.get("coverage", []):
        preferred_ids.extend(coverage.evidence_ids)
    ordered_ids = tuple(dict.fromkeys((
        *(item for item in preferred_ids if item in by_id),
        *(item.evidence_id for item in inventory),
    )))
    evidence: list[EvidenceItem] = []
    evidence_chars = 0
    evidence_char_budget = 6000 if state["identity"].depth == 0 else 14000
    for evidence_id in ordered_ids:
        item = by_id[evidence_id]
        estimated = min(500, len(item.finding)) + min(300, len(item.title)) + 180
        if evidence and evidence_chars + estimated > evidence_char_budget:
            break
        evidence.append(item)
        evidence_chars += estimated
    child_results = tuple(state.get("child_results", []))
    memo_limit = max(800, 16000 // max(1, len(child_results)))
    child_memos = [
        {
            "task_id": item.task_id,
            "status": item.status.value,
            "summary": item.summary[:800],
            "research_memo": item.research_memo[:memo_limit],
            "unresolved": list(item.unresolved[:6]),
        }
        for item in child_results
    ]
    payload = {
        "objective": state["task"].objective,
        "expected_output": state["task"].expected_output,
        "constraints": list(state["task"].constraints),
        "termination_reason": (
            state["termination_reason"].value
            if state.get("termination_reason") is not None
            else (
                hard_termination_reason(state.get("stop_reason")).value
                if hard_termination_reason(state.get("stop_reason")) is not None
                else None
            )
        ),
        "stop_detail": state.get("stop_reason"),
        "requirements": [asdict(item) for item in state["research_requirements"]],
        "coverage": [
            {
                **asdict(item),
                "status": item.status.value,
            }
            for item in state["coverage"]
        ],
        "critical_gaps": [asdict(item) for item in state["critical_gaps"]],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "finding": item.finding[:500],
                "title": item.title[:300],
                "source_ref": item.source_ref,
                "locator": item.locator,
                "limitations": item.limitations[:300],
            }
            for item in evidence
        ],
        "child_results": child_memos,
        "tool_outcomes": [
            {
                "name": message.get("name"),
                "content": str(message.get("content") or "")[:1000],
            }
            for message in state["messages"]
            if message.get("role") == "tool"
        ][-12:],
        "candidate_summary": state.get("last_content", "")[:4000],
    }
    identity_messages = [
        {
            "role": "system",
            "content": _system_prompt(state["identity"], state["limits"]),
        },
        {"role": "user", "content": _task_prompt(state["task"])},
    ]
    output_instruction = (
        "Return only the complete Markdown report, with no JSON wrapper and no "
        "fenced code block. Use the Child research_memo fields as the primary "
        "research input. Resolve overlaps, cover every required direction, "
        "disclose material gaps, and reuse only the supplied "
        "[[EVIDENCE:id]] markers. Copy every complete ID exactly, including its "
        "evidence- prefix; never use only the hash suffix. Do not write a "
        "References section."
        if state["identity"].depth == 0
        else
        "Return one JSON object with status, summary, findings, unresolved, a "
        "concise research_memo about only this assigned direction, and an empty "
        "report_markdown. In the memo, choose and cite the Evidence you judge "
        "useful with supplied [[EVIDENCE:id]] markers; copy every complete ID "
        "exactly, including its evidence- prefix, and disclose remaining gaps."
    )
    return [
        *identity_messages,
        {
            "role": "user",
            "content": (
                "FINAL_SYNTHESIS_SNAPSHOT\n" + output_instruction + " Preserve "
                "uncertainty and do not invent evidence. The runtime will independently enforce research "
                "status and termination reason.\n\nSTATE:\n" + json.dumps(payload, ensure_ascii=False, default=str)
            ),
        },
    ]


def _stable_evidence_id(source_ref: str, excerpt: str) -> str:
    digest = hashlib.sha256(f"{source_ref}\n{excerpt}".encode("utf-8")).hexdigest()
    return f"evidence-{digest[:16]}"


def _evidence_item(
    *,
    finding: Any,
    source_type: str,
    title: Any,
    source_ref: Any,
    locator: Any = "",
    excerpt: Any = "",
    excerpt_type: str = "paraphrase",
    limitations: str = "",
    requirement_id: str = "",
    action_id: str = "",
    artifact_id: str = "",
) -> EvidenceItem | None:
    clean_ref = str(source_ref or "").strip()
    clean_excerpt = str(excerpt or "").strip()
    clean_finding = str(finding or clean_excerpt or title or "").strip()
    if not clean_ref or not clean_finding:
        return None
    return EvidenceItem(
        evidence_id=_stable_evidence_id(
            f"{clean_ref}#{requirement_id}",
            clean_excerpt or clean_finding,
        ),
        finding=clean_finding,
        source_type=source_type,
        title=str(title or clean_ref).strip(),
        source_ref=clean_ref,
        locator=str(locator or clean_ref).strip(),
        excerpt=clean_excerpt[:4000],
        excerpt_type=excerpt_type,
        limitations=limitations,
        requirement_id=requirement_id,
        action_id=action_id,
        artifact_id=artifact_id,
    )


def _extract_evidence(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    *,
    action: NextResearchAction | None = None,
    artifact_id: str = "",
) -> list[EvidenceItem]:
    if action is None or _semantic_tool_error(tool_name, result) is not None:
        return []
    lineage = {
        "requirement_id": action.requirement_id,
        "action_id": _action_id(action),
        "artifact_id": artifact_id,
    }
    evidence: list[EvidenceItem] = []
    if tool_name == "acquire_evidence" and isinstance(result, dict):
        search_backend = str(result.get("search_backend") or "configured search backend")
        for document in result.get("documents", []):
            if not isinstance(document, dict):
                continue
            source_ref = str(document.get("url") or "").strip()
            document_title = str(document.get("title") or source_ref).strip()
            document_format = str(document.get("format") or "html").strip().lower()
            extractor = str(document.get("extractor") or "structured-browser").strip()
            warnings = document.get("warnings", [])
            warning_text = "; ".join(
                str(item).strip()
                for item in warnings
                if isinstance(item, str) and item.strip()
            )
            blocks = document.get("blocks", [])
            if not source_ref or not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                locator = str(block.get("locator") or "").strip()
                heading = str(block.get("heading") or "").strip()
                block_text = str(block.get("text") or "").strip()
                if not locator or not block_text:
                    continue
                if not _is_relevant_evidence(
                    action,
                    finding=block_text[:1000],
                    title=heading or document_title,
                    source_ref=source_ref,
                ):
                    continue
                limitations = (
                    f"Full-content block acquired via {search_backend} and extracted "
                    f"with {extractor}."
                )
                if warning_text:
                    limitations += f" Warnings: {warning_text}"
                found = _evidence_item(
                    finding=block_text[:1000],
                    source_type="paper" if document_format == "pdf" else "web",
                    title=(
                        f"{document_title} — {heading}"
                        if heading and heading != document_title
                        else document_title
                    ),
                    source_ref=source_ref,
                    locator=locator,
                    excerpt=block_text[:4000],
                    limitations=limitations,
                    **lineage,
                )
                if found:
                    evidence.append(found)
    elif tool_name == "web_search" and isinstance(result, dict):
        for item in result.get("results", []):
            if not isinstance(item, dict):
                continue
            if not _is_relevant_evidence(
                action,
                finding=item.get("snippet") or item.get("title"),
                title=item.get("title"),
                source_ref=item.get("url"),
            ):
                continue
            found = _evidence_item(
                finding=item.get("snippet") or item.get("title"),
                source_type="web",
                title=item.get("title"),
                source_ref=item.get("url"),
                locator=item.get("url"),
                excerpt=item.get("snippet"),
                limitations="Search-result snippet; open the source for stronger verification.",
                **lineage,
            )
            if found:
                evidence.append(found)
    elif tool_name == "arxiv_reader" and isinstance(result, dict):
        for item in result.get("papers", []):
            if not isinstance(item, dict):
                continue
            paper_id = item.get("id") or item.get("paper_id") or ""
            source_ref = item.get("pdf_url") or item.get("url") or (f"arxiv:{paper_id}" if paper_id else "")
            if not _is_relevant_evidence(
                action,
                finding=item.get("summary") or item.get("title"),
                title=item.get("title"),
                source_ref=source_ref,
            ):
                continue
            found = _evidence_item(
                finding=item.get("summary") or item.get("title"),
                source_type="paper",
                title=item.get("title"),
                source_ref=source_ref,
                locator=paper_id or source_ref,
                excerpt=item.get("summary"),
                limitations="Paper metadata or abstract; verify the full text when needed.",
                **lineage,
            )
            if found:
                evidence.append(found)
    elif tool_name == "browser" and isinstance(result, dict):
        source_ref = str(result.get("url") or args.get("url") or "").strip()
        document_title = str(result.get("title") or source_ref).strip()
        document_format = str(result.get("format") or "html").strip().lower()
        extractor = str(result.get("extractor") or "structured-browser").strip()
        warnings = result.get("warnings", [])
        warning_text = "; ".join(
            str(item).strip()
            for item in warnings
            if isinstance(item, str) and item.strip()
        )
        blocks = result.get("blocks", [])
        if not source_ref or not isinstance(blocks, list):
            return evidence
        for block in blocks:
            if not isinstance(block, dict):
                continue
            locator = str(block.get("locator") or "").strip()
            heading = str(block.get("heading") or "").strip()
            block_text = str(block.get("text") or "").strip()
            if not locator or not block_text:
                continue
            if not _is_relevant_evidence(
                action,
                finding=block_text[:1000],
                title=heading or document_title,
                source_ref=source_ref,
            ):
                continue
            limitations = f"Full-content block extracted with {extractor}."
            if warning_text:
                limitations += f" Warnings: {warning_text}"
            found = _evidence_item(
                finding=block_text[:1000],
                source_type="paper" if document_format == "pdf" else "web",
                title=(
                    f"{document_title} — {heading}"
                    if heading and heading != document_title
                    else document_title
                ),
                source_ref=source_ref,
                locator=locator,
                excerpt=block_text[:4000],
                limitations=limitations,
                **lineage,
            )
            if found:
                evidence.append(found)
    elif tool_name == "browser":
        browser_text = str(result)
        alternative_match = re.match(
            r"^\[ALTERNATIVE_SOURCE: (https?://[^\]]+)\]",
            browser_text,
            re.IGNORECASE,
        )
        source_ref = (
            alternative_match.group(1) if alternative_match else args.get("url")
        )
        if not _is_relevant_evidence(
            action,
            finding=browser_text[:1000],
            title=source_ref,
            source_ref=source_ref,
        ):
            return evidence
        found = _evidence_item(
            finding=browser_text[:1000],
            source_type="web",
            title=source_ref,
            source_ref=source_ref,
            locator=source_ref,
            excerpt=browser_text[:4000],
            limitations=(
                "Official alternative source used because the requested page was blocked; "
                "exact section locator may be unavailable."
                if alternative_match
                else "Extracted webpage text; exact section locator may be unavailable."
            ),
            **lineage,
        )
        if found:
            evidence.append(found)
    elif tool_name == "file_reader" and isinstance(result, dict):
        path = result.get("path")
        file_format = result.get("format")
        content = result.get("content")
        truncated = result.get("truncated")
        if (
            not isinstance(path, str)
            or file_format not in {"text", "markdown", "pdf", "csv", "json", "docx"}
            or not isinstance(content, str)
            or not isinstance(truncated, bool)
        ):
            return evidence
        clean_path = path.strip()
        relative = PurePosixPath(clean_path)
        windows_path = PureWindowsPath(clean_path)
        if (
            not clean_path
            or clean_path != path
            or "\\" in clean_path
            or ":" in clean_path
            or str(relative) != clean_path
            or relative.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or any(part in {"", ".", ".."} for part in clean_path.split("/"))
            or len(relative.parts) < 2
            or relative.parts[0] not in {"memory", "upload"}
        ):
            return evidence
        limitations = "File excerpt; preserve page or section details when available."
        if truncated:
            limitations += " Reader output was truncated to the configured limit."
        found = _evidence_item(
            finding=content[:1000],
            source_type="file",
            title=clean_path,
            source_ref=clean_path,
            locator=clean_path,
            excerpt=content[:4000],
            excerpt_type="quote",
            limitations=limitations,
            **lineage,
        )
        if found:
            evidence.append(found)
    return evidence


def _deduplicate_evidence(items: Iterable[EvidenceItem]) -> list[EvidenceItem]:
    unique: dict[str, EvidenceItem] = {}
    for item in items:
        unique.setdefault(item.evidence_id, item)
    return list(unique.values())


def _serialize_tool_result(result: Any, max_chars: int) -> str:
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    if len(serialized) <= max_chars:
        return serialized
    return f"{serialized[:max_chars]}\n[TOOL_OUTPUT_TRUNCATED]"


def _coerce_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


_UNRESOLVED_SCOPE = re.compile(
    r"^\[requirements=(?P<requirements>[^;\]]*);scope=(?P<scope>[^\]]*)\]\s*(?P<text>.*)$"
)


def _scoped_unresolved(
    values: Iterable[str],
    candidate: ForkCandidate,
) -> tuple[str, ...]:
    prefix = (
        "[requirements="
        + ",".join(sorted(candidate.requirement_ids))
        + ";scope="
        + _candidate_scope_signature(candidate)
        + "] "
    )
    return tuple(
        value if _UNRESOLVED_SCOPE.match(value) else prefix + value
        for value in (str(item).strip() for item in values)
        if value
    )


def _aggregate_unresolved(values: Iterable[str]) -> tuple[str, ...]:
    scoped: dict[tuple[str, str], list[str]] = {}
    unscoped: dict[str, str] = {}
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        match = _UNRESOLVED_SCOPE.match(value)
        if match is None:
            unscoped.setdefault(" ".join(value.casefold().split()), value)
            continue
        key = (
            " ".join(match.group("requirements").casefold().split()),
            " ".join(match.group("scope").casefold().split()),
        )
        text = match.group("text").strip()
        bucket = scoped.setdefault(key, [])
        normalized = " ".join(text.casefold().split())
        if text and all(" ".join(item.casefold().split()) != normalized for item in bucket):
            bucket.append(text)
    aggregated = [
        f"[requirements={requirements};scope={scope}] " + " | ".join(texts)
        for (requirements, scope), texts in scoped.items()
        if texts
    ]
    return tuple((*aggregated, *unscoped.values()))


def _fork_policy_instance(policy: Any) -> Any:
    fork = getattr(policy, "fork", None)
    if callable(fork):
        return fork()
    try:
        return copy.deepcopy(policy)
    except Exception as exc:
        raise RuntimeError("policy cannot be isolated for a child Research Agent") from exc


def _root_final_policy_instance(policy: Any, max_tokens: int) -> Any:
    """Fork configurable production policies without cloning simple test policies."""
    fork = getattr(policy, "fork", None)
    final_policy = (
        fork()
        if callable(fork) and hasattr(policy, "max_tokens")
        else policy
    )
    if hasattr(final_policy, "max_tokens"):
        final_policy.max_tokens = max_tokens
    return final_policy


def _fork_tool_instances(tools: Iterable[Any]) -> list[Any]:
    isolated: list[Any] = []
    for tool in tools:
        fork = getattr(tool, "fork", None)
        if callable(fork):
            isolated.append(fork())
            continue
        try:
            isolated.append(copy.deepcopy(tool))
        except Exception as exc:
            name = str(getattr(tool, "name", type(tool).__name__))
            raise RuntimeError(f"tool {name!r} cannot be isolated for a child Research Agent") from exc
    return isolated


def _child_task(
    parent: ResearchTask,
    candidate: ForkCandidate,
    parent_requirements: Iterable[ResearchRequirement] = (),
    *,
    assignment_id: str = "",
    parent_assignment_id: str = "",
) -> ResearchTask:
    fingerprint = candidate_fingerprint(candidate)
    # Root Brief planning fields describe the root deliverable. Inheriting them
    # made every homogeneous child assess itself against the entire root Brief,
    # so parallel forks repeated the whole research problem recursively.
    planning_keys = {"research_requirements", "directions", "research_gaps"}
    child_context = {key: value for key, value in parent.context.items() if key not in planning_keys}
    child_context.update({key: value for key, value in candidate.context.items() if key not in planning_keys})
    requirements_by_id = {item.requirement_id: item for item in parent_requirements}
    selected_requirements = [
        requirements_by_id[requirement_id]
        for requirement_id in candidate.requirement_ids
        if requirement_id in requirements_by_id
    ]
    if selected_requirements:
        child_context["research_requirements"] = [
            {
                "requirement_id": item.requirement_id,
                "description": item.description,
                "required": item.required,
                "requires_external_evidence": item.requires_external_evidence,
            }
            for item in selected_requirements
        ]
        child_context["parent_requirement_ids"] = [item.requirement_id for item in selected_requirements]
    else:
        child_context["research_requirements"] = [
            {
                "requirement_id": "R1",
                "description": candidate.objective,
                "required": True,
            }
        ]
    child_context["parent_objective"] = parent.objective
    child_context["own_assignment_id"] = assignment_id
    child_context["parent_assignment_id"] = parent_assignment_id
    child_context["assignment_scope_signature"] = _candidate_scope_signature(candidate)
    return ResearchTask(
        task_id=f"child-{fingerprint[:12]}",
        objective=candidate.objective,
        context=child_context,
        expected_output=candidate.expected_output,
        constraints=parent.constraints,
        require_evidence=parent.require_evidence,
    )


def _child_identity(
    parent: ExecutionIdentity,
    candidate: ForkCandidate,
) -> ExecutionIdentity:
    fingerprint = candidate_fingerprint(candidate)
    return ExecutionIdentity(
        thread_id=f"{parent.thread_id}.child.{fingerprint[:12]}",
        parent_thread_id=parent.thread_id,
        root_thread_id=parent.root_thread_id,
        depth=parent.depth + 1,
    )


def _split_budget(total: int, count: int, *, minimum: int = 0) -> list[int]:
    if count <= 0:
        return []
    required = minimum * count
    if total < required:
        raise ValueError("budget cannot satisfy the minimum allocation")
    distributable = total - required
    base, extra = divmod(distributable, count)
    return [minimum + base + (1 if index < extra else 0) for index in range(count)]


def create_research_agent_state(
    task: ResearchTask,
    identity: ExecutionIdentity,
    limits: AgentLimits,
    *,
    deadline_at: float | None = None,
    subtree_thread_budget: int | None = None,
    subtree_tool_budget: int | None = None,
    subtree_token_budget: int | None = None,
    subtree_retry_budget: int | None = None,
    lineage_objectives: list[str] | None = None,
    initial_evidence: Iterable[EvidenceItem] = (),
) -> ResearchAgentState:
    limits.validate()
    requirements = build_research_requirements(task)
    context = task.context if isinstance(task.context, dict) else {}
    own_assignment_id = str(context.get("own_assignment_id") or "").strip()
    if not own_assignment_id:
        own_assignment_id = _root_assignment_id(identity)
    parent_assignment_id = str(context.get("parent_assignment_id") or "").strip()
    return ResearchAgentState(
        task=task,
        identity=identity,
        limits=limits,
        messages=[],
        notepad_entries=[],
        iteration=0,
        tool_calls_used=0,
        pending_tool_calls=[],
        pending_fork_calls=[],
        pending_stop_reason=None,
        completed_fork_fingerprints=[],
        child_thread_ids=[],
        child_results=[],
        observed_evidence=_deduplicate_evidence(initial_evidence),
        deadline_at=(deadline_at if deadline_at is not None else time.time() + limits.max_elapsed_seconds),
        subtree_thread_budget=(
            subtree_thread_budget if subtree_thread_budget is not None else limits.effective_max_total_agents
        ),
        subtree_tool_budget=(subtree_tool_budget if subtree_tool_budget is not None else limits.max_total_tool_calls),
        subtree_token_budget=(subtree_token_budget if subtree_token_budget is not None else limits.max_total_tokens),
        subtree_retry_budget=(subtree_retry_budget if subtree_retry_budget is not None else limits.max_total_retries),
        total_threads_used=1,
        total_tool_calls_used=0,
        estimated_tokens_used=0,
        retries_used=0,
        source_candidate_count=0,
        source_open_count=0,
        duplicate_source_count=0,
        acquisition_call_count=0,
        execution_events=[],
        compression_manifests=[],
        semantic_memos=[],
        lineage_objectives=(list(lineage_objectives) if lineage_objectives is not None else [task.objective]),
        draft=None,
        draft_raw="",
        last_content="",
        last_assessed_evidence_count=0,
        last_assessed_strategy_attempt_count=0,
        research_requirements=list(requirements),
        coverage=list(initial_coverage(requirements)),
        critical_gaps=[],
        next_actions=[],
        strategy_attempts=[],
        assessment_decision=None,
        assessment_output_status=OutputStatus.VALID,
        assessment_error=None,
        termination_reason=None,
        finalization_requested=False,
        output_status=OutputStatus.VALID,
        stop_reason=None,
        result=None,
        control_action=None,
        control_rationale=None,
        control_target_requirement_ids=[],
        control_decision_error=None,
        control_repair_applied=False,
        local_rounds_since_fork=0,
        own_assignment_id=own_assignment_id,
        parent_assignment_id=parent_assignment_id,
    )


def _fallback_assessment(
    state: ResearchAgentState,
    *,
    attempts: tuple[StrategyAttempt, ...],
    hard_reason: TerminationReason | None = None,
    has_research_tools: bool,
    assessment_error: str | None = None,
) -> ResearchAssessment:
    """Conservative deterministic routing after one failed structure repair."""
    requirements = tuple(state["research_requirements"])
    evidence = _deduplicate_evidence(state["observed_evidence"])
    existing = tuple(state["coverage"])
    if hard_reason is not None:
        return ResearchAssessment(
            decision=ResearchDecision.STOP_RESEARCH,
            coverage=existing,
            critical_gaps=tuple(state["critical_gaps"]),
            termination_reason=hard_reason,
            output_status=OutputStatus.FALLBACK,
        )
    if not evidence and any(
        event.get("kind") == "tool_finished" and not event.get("ok", False)
        for event in state.get("execution_events", [])
    ):
        return ResearchAssessment(
            decision=ResearchDecision.STOP_RESEARCH,
            coverage=existing,
            critical_gaps=tuple(
                CriticalGap(
                    item.requirement_id,
                    item.remaining_gap or "The evidence tool path failed.",
                )
                for item in existing
                if item.status != RequirementStatus.SUPPORTED
            ),
            termination_reason=TerminationReason.TOOL_FAILURE,
            output_status=OutputStatus.FALLBACK,
        )
    if state.get("draft_raw") and not state["task"].require_evidence:
        coverage = tuple(
            RequirementCoverage(
                requirement.requirement_id,
                RequirementStatus.SUPPORTED,
                rationale="The scoped task did not require external evidence.",
            )
            for requirement in requirements
        )
        return ResearchAssessment(
            decision=ResearchDecision.STOP_RESEARCH,
            coverage=coverage,
            termination_reason=TerminationReason.COVERAGE_COMPLETE,
            output_status=OutputStatus.FALLBACK,
        )
    all_required_supported = bool(existing) and all(item.status == RequirementStatus.SUPPORTED for item in existing)
    if all_required_supported and (evidence or not state["task"].require_evidence):
        # A structure failure cannot erase coverage that was already validated in
        # an earlier checkpoint. It also cannot manufacture new support.
        return ResearchAssessment(
            decision=ResearchDecision.STOP_RESEARCH,
            coverage=existing,
            termination_reason=TerminationReason.COVERAGE_COMPLETE,
            output_status=OutputStatus.FALLBACK,
        )
    if not has_research_tools:
        return ResearchAssessment(
            decision=ResearchDecision.STOP_RESEARCH,
            coverage=existing,
            critical_gaps=tuple(
                CriticalGap(item.requirement_id, item.remaining_gap or "Evidence is unavailable.")
                for item in existing
                if item.status != RequirementStatus.SUPPORTED
            ),
            termination_reason=TerminationReason.TOOL_FAILURE,
            output_status=OutputStatus.FALLBACK,
        )
    preserved_gaps = tuple(
        gap
        for gap in state.get("critical_gaps", [])
        if any(
            item.requirement_id == gap.requirement_id and item.status != RequirementStatus.SUPPORTED
            for item in existing
        )
    )
    gaps = preserved_gaps or tuple(
        CriticalGap(
            item.requirement_id,
            item.remaining_gap or "The necessary requirement remains unsupported.",
        )
        for item in existing
        if item.status != RequirementStatus.SUPPORTED
    )
    gap_ids = {item.requirement_id for item in gaps}
    pending_actions = tuple(
        action
        for action in unattempted_actions(
            state.get("next_actions", []),
            attempts,
        )
        if action.requirement_id in gap_ids and action.expected_value in {"high", "medium"}
    )
    if pending_actions:
        return ResearchAssessment(
            decision=ResearchDecision.CONTINUE,
            coverage=existing,
            critical_gaps=gaps,
            next_actions=pending_actions,
            output_status=OutputStatus.FALLBACK,
        )
    incomplete_children = tuple(
        child
        for child in state.get("child_results", [])
        if child.status != ResearchStatus.COMPLETED
    )
    exhausted_child_paths = bool(incomplete_children) and all(
        child.termination_reason in {
            TerminationReason.BUDGET_FORCED,
            TerminationReason.EVIDENCE_EXHAUSTED,
        }
        for child in incomplete_children
    )
    if evidence and assessment_error and exhausted_child_paths:
        return ResearchAssessment(
            decision=ResearchDecision.STOP_RESEARCH,
            coverage=existing,
            critical_gaps=gaps,
            termination_reason=TerminationReason.EVIDENCE_EXHAUSTED,
            exhaustion_reason=(
                "Child research paths reached real local budget or evidence "
                "boundaries, usable Evidence was retained, and no unattempted "
                "root action remained after assessment repair failed."
            ),
            output_status=OutputStatus.FALLBACK,
        )
    return ResearchAssessment(
        decision=ResearchDecision.STOP_RESEARCH,
        coverage=existing,
        critical_gaps=gaps,
        termination_reason=TerminationReason.TOOL_FAILURE,
        output_status=OutputStatus.FALLBACK,
    )


def _runtime_exhaustion_assessment(
    state: ResearchAgentState,
    attempts: tuple[StrategyAttempt, ...],
) -> ResearchAssessment | None:
    """Stop after three distinct no-progress paths for every open requirement.

    The model remains responsible for ordinary semantic sufficiency decisions.
    This conservative runtime fuse only handles the repeated-failure case that
    otherwise burns the whole token budget while cycling across tool families.
    """
    required_ids = {
        requirement.requirement_id
        for requirement in state["research_requirements"]
        if requirement.required and requirement.requires_external_evidence
    }
    open_coverage = tuple(
        item
        for item in state["coverage"]
        if item.requirement_id in required_ids and item.status != RequirementStatus.SUPPORTED
    )
    if not open_coverage:
        return None

    families_by_requirement: dict[str, set[str]] = {}
    for attempt in attempts:
        if attempt.outcome != "no_progress":
            continue
        families_by_requirement.setdefault(attempt.requirement_id, set()).add(attempt.strategy.strip().lower())
    if any(len(families_by_requirement.get(item.requirement_id, set())) < 3 for item in open_coverage):
        return None

    previous_gaps = {
        gap.requirement_id: gap for gap in state.get("critical_gaps", []) if gap.requirement_id in required_ids
    }
    gaps = tuple(
        previous_gaps.get(item.requirement_id)
        or CriticalGap(
            item.requirement_id,
            item.remaining_gap or "Three distinct research strategy families produced no usable evidence.",
            "high",
        )
        for item in open_coverage
    )
    return ResearchAssessment(
        decision=ResearchDecision.STOP_RESEARCH,
        coverage=tuple(state["coverage"]),
        critical_gaps=gaps,
        termination_reason=TerminationReason.EVIDENCE_EXHAUSTED,
        exhaustion_reason=(
            "Every open requirement has three distinct no-progress strategy families; "
            "continuing would repeat exhausted evidence paths."
        ),
        output_status=OutputStatus.VALID,
    )


def build_research_agent_graph(
    policy: Any,
    tools: Iterable[Any] = (),
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    inherit_checkpointer: bool = False,
    child_checkpointer: BaseCheckpointSaver | None = None,
    tool_artifact_store: Any | None = None,
    coordination_board: ResearchBlackboard | None = None,
    homogeneous_fork_config: HomogeneousForkConfig | None = None,
    agent_scheduler: FairAgentScheduler | None = None,
    state_schema: Any = ResearchAgentState,
    allow_fork_tool: bool = True,
) -> Any:
    """Build the one graph shared by root, child, and grandchild Agents."""
    if inherit_checkpointer and checkpointer is not None:
        raise ValueError("cannot set checkpointer when inherit_checkpointer is true")
    tool_list = list(tools)
    fork_settings = homogeneous_fork_config or HomogeneousForkConfig()
    fork_settings.validate()
    if fork_settings.enabled and coordination_board is None:
        raise ValueError(
            "enabled homogeneous Fork control requires a persistent research blackboard"
        )
    tool_map = _build_tool_map(tool_list)
    effective_checkpointer = (
        None if inherit_checkpointer else checkpointer if checkpointer is not None else InMemorySaver()
    )
    descendant_checkpointer = child_checkpointer if child_checkpointer is not None else effective_checkpointer
    live_scheduler = agent_scheduler

    def prepare(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_invocation(state, config)
        with _node_trace("prepare", state) as observation:
            if coordination_board is not None:
                requirements = tuple(
                    state.get("research_requirements")
                    or build_research_requirements(state["task"])
                )
                context = (
                    state["task"].context
                    if isinstance(state["task"].context, dict)
                    else {}
                )
                plan_id = str(
                    context.get("research_plan_id")
                    or f"plan-{state['identity'].root_thread_id}"
                )
                outline = context.get("report_outline", ())
                if state["identity"].depth == 0:
                    coordination_rows = context.get("coordination_requirements")
                    if not isinstance(coordination_rows, (list, tuple)):
                        coordination_rows = [
                            {
                                "requirement_id": item.requirement_id,
                                "description": item.description,
                                "required": item.required,
                                "requires_external_evidence": item.requires_external_evidence,
                            }
                            for item in requirements
                        ]
                    coordination_board.ensure_plan(
                        state["identity"].root_thread_id,
                        plan_id=plan_id,
                        objective=str(
                            context.get("global_objective")
                            or state["task"].objective
                        ),
                        requirements=coordination_rows,
                        report_outline=(
                            outline
                            if isinstance(outline, (list, tuple))
                            else (str(outline),)
                        ),
                    )
                    coordination_board.ensure_root_assignment(
                        state["identity"].root_thread_id,
                        assignment_id=_own_assignment_id(state),
                        owner_thread_id=state["identity"].thread_id,
                        objective=str(context.get("global_objective") or state["task"].objective),
                        requirement_ids=(
                            str(item["requirement_id"])
                            for item in coordination_rows
                            if bool(item.get("required", True))
                        ),
                    )
                    if fork_settings.budget_leases_enabled:
                        coordination_board.ensure_budget_pool(
                            state["identity"].root_thread_id,
                            total_tokens=state["subtree_token_budget"],
                            protected_tokens=min(
                                state["subtree_token_budget"],
                                _finalization_token_reserve(state)
                                + fork_settings.parent_merge_reserve_tokens,
                            ),
                        )
                coordination_board.update_agent(
                    state["identity"].root_thread_id,
                    thread_id=state["identity"].thread_id,
                    parent_thread_id=state["identity"].parent_thread_id or "",
                    depth=state["identity"].depth,
                    requirement_ids=_owned_requirement_ids(state),
                    assignment_id=_own_assignment_id(state),
                    parent_assignment_id=_parent_assignment_id(state),
                    status="running",
                    current_action="prepare",
                )
            if state["messages"]:
                requirements = tuple(state.get("research_requirements") or build_research_requirements(state["task"]))
                migration: dict[str, Any] = {
                    "research_requirements": list(requirements),
                    "coverage": list(state.get("coverage") or initial_coverage(requirements)),
                    "critical_gaps": list(state.get("critical_gaps", [])),
                    "next_actions": list(state.get("next_actions", [])),
                    "strategy_attempts": list(state.get("strategy_attempts", [])),
                    "last_assessed_strategy_attempt_count": state.get(
                        "last_assessed_strategy_attempt_count",
                        len(state.get("strategy_attempts", [])),
                    ),
                    "assessment_decision": state.get("assessment_decision"),
                    "assessment_output_status": state.get("assessment_output_status", OutputStatus.VALID),
                    "assessment_error": state.get("assessment_error"),
                    "termination_reason": state.get("termination_reason"),
                    "finalization_requested": bool(state.get("finalization_requested", False)),
                    "output_status": state.get("output_status", OutputStatus.VALID),
                    "draft_raw": state.get("draft_raw", ""),
                    "last_assessed_evidence_count": int(state.get("last_assessed_evidence_count", 0)),
                    "compression_manifests": list(state.get("compression_manifests", [])),
                    "semantic_memos": list(state.get("semantic_memos", [])),
                    "source_candidate_count": int(state.get("source_candidate_count", 0)),
                    "source_open_count": int(state.get("source_open_count", 0)),
                    "duplicate_source_count": int(state.get("duplicate_source_count", 0)),
                    "acquisition_call_count": int(state.get("acquisition_call_count", 0)),
                    "own_assignment_id": _own_assignment_id(state),
                    "parent_assignment_id": _parent_assignment_id(state),
                }
                observation.add_output({"resumed": True, "state_migrated": True})
                return migration
            messages = [
                {
                    "role": "system",
                    "content": _system_prompt(state["identity"], state["limits"]),
                },
                {"role": "user", "content": _task_prompt(state["task"])},
            ]
            observation.add_output({"resumed": False})
            return {
                "messages": messages,
                "execution_events": [
                    *state["execution_events"],
                    _event("agent_started", state["identity"]),
                ],
            }

    def refresh_budget_lease(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """Top up one child lease from the refundable global pool when needed."""

        _validate_invocation(state, config)
        if (
            not fork_settings.budget_leases_enabled
            or coordination_board is None
            or state["identity"].depth == 0
        ):
            return {}
        lease = coordination_board.budget_lease(
            state["identity"].root_thread_id,
            state["identity"].thread_id,
        )
        if lease is None or lease.get("status") != "active":
            return {}
        granted = int(lease["granted_tokens"])
        used = int(state.get("estimated_tokens_used", 0))
        remaining = max(0, granted - used)
        threshold = max(
            fork_settings.child_topup_tokens,
            _CHILD_FINALIZATION_TOKEN_MINIMUM + _ASSESSMENT_OUTPUT_TOKEN_RESERVE,
        )
        updated_grant = granted
        if remaining <= threshold and granted < int(lease["max_tokens"]):
            updated_grant = coordination_board.request_budget_topup(
                state["identity"].root_thread_id,
                thread_id=state["identity"].thread_id,
                used_tokens=used,
                requested_tokens=fork_settings.child_topup_tokens,
            )
        if updated_grant <= int(state["subtree_token_budget"]):
            return {}
        return {
            "subtree_token_budget": updated_grant,
            "execution_events": [
                *state["execution_events"],
                _event(
                    "child_budget_lease_topped_up",
                    state["identity"],
                    previous_tokens=state["subtree_token_budget"],
                    granted_tokens=updated_grant,
                    used_tokens=used,
                ),
            ],
        }

    async def think_and_plan(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_invocation(state, config)
        finalization_requested = bool(state.get("finalization_requested", False))
        if state["stop_reason"] and not finalization_requested:
            return {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "termination_reason": hard_termination_reason(state["stop_reason"]),
            }
        if not finalization_requested and state["iteration"] >= state["limits"].max_iterations:
            return {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "stop_reason": "max_iterations_exhausted",
                "termination_reason": TerminationReason.BUDGET_FORCED,
            }
        available_seconds = (
            _remaining_finalization_seconds(state)
            if finalization_requested
            else _remaining_research_seconds(state)
        )
        if available_seconds <= 0:
            update: dict[str, Any] = {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "stop_reason": "time_budget_exhausted",
                "termination_reason": TerminationReason.BUDGET_FORCED,
            }
            if finalization_requested:
                update["finalization_requested"] = False
            return update
        remaining_tokens = state["subtree_token_budget"] - state["estimated_tokens_used"]
        consumed_artifact_ids = {
            artifact_id
            for attempt in state.get("strategy_attempts", [])[: state.get("last_assessed_strategy_attempt_count", 0)]
            for artifact_id in attempt.artifact_ids
        }
        working_messages = deterministic_context_cleanup(
            state["messages"],
            consumed_artifact_ids=consumed_artifact_ids,
        )
        state_projection = {
            "coverage": [asdict(item) for item in state.get("coverage", [])],
            "critical_gaps": [asdict(item) for item in state.get("critical_gaps", [])],
            "next_actions": [asdict(item) for item in active_next_actions(state.get("next_actions", []))],
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "requirement_id": item.requirement_id,
                    "artifact_id": item.artifact_id,
                    "finding": item.finding[:300],
                    "source_ref": item.source_ref[:300],
                }
                for item in state.get("observed_evidence", [])[-12:]
            ],
            "strategy_attempts": list(aggregate_strategy_attempts(state.get("strategy_attempts", [])))[-12:],
        }
        deterministic_consolidation = collapse_verified_working_context(
            working_messages,
            state_projection=state_projection,
        )
        compaction_token_charge = 0
        compaction_event: dict[str, Any] | None = None
        if not finalization_requested and deterministic_consolidation != working_messages:
            try:
                compaction_messages = semantic_memo_messages(
                    working_messages,
                    state_projection=state_projection,
                )
                compaction_response = await asyncio.wait_for(
                    call_policy(policy, compaction_messages, []),
                    timeout=max(0.001, available_seconds),
                )
                compaction_token_charge = _estimate_tokens(
                    compaction_messages,
                    compaction_response,
                )
                semantic_memo = validate_semantic_memo(
                    str(compaction_response.get("content") or ""),
                    request_messages=compaction_messages,
                )
                working_messages = collapse_verified_working_context(
                    working_messages,
                    state_projection=state_projection,
                    semantic_memo=semantic_memo,
                )
                compaction_event = _event(
                    "working_context_consolidated",
                    state["identity"],
                    semantic_memo=True,
                )
            except ContextCompactionError as exc:
                working_messages = deterministic_consolidation
                compaction_event = _event(
                    "working_context_consolidated",
                    state["identity"],
                    semantic_memo=False,
                    error=str(exc),
                )
            except Exception as exc:
                working_messages = deterministic_consolidation
                compaction_event = _event(
                    "working_context_consolidated",
                    state["identity"],
                    semantic_memo=False,
                    error=str(exc),
                )
        else:
            working_messages = deterministic_consolidation
        consolidation_manifest = context_consolidation_manifest(working_messages)
        compression_manifests = list(state.get("compression_manifests", []))
        semantic_memos = list(state.get("semantic_memos", []))
        if consolidation_manifest is not None:
            manifest_id = str(consolidation_manifest.get("manifest_id") or "")
            if manifest_id and not any(item.get("manifest_id") == manifest_id for item in compression_manifests):
                compression_manifests.append(consolidation_manifest)
                memo = consolidation_manifest.get("semantic_memo")
                if isinstance(memo, dict):
                    semantic_memos.append({"manifest_id": manifest_id, **memo})
        remaining_tokens = max(0, remaining_tokens - compaction_token_charge)
        available_seconds = (
            _remaining_finalization_seconds(state)
            if finalization_requested
            else _remaining_research_seconds(state)
        )
        board_view: dict[str, Any] = {}
        if coordination_board is not None and not finalization_requested:
            try:
                board_view = coordination_board.snapshot(
                    state["identity"].root_thread_id,
                    viewer_thread_id=state["identity"].thread_id,
                    own_requirement_ids=_owned_requirement_ids(state),
                    own_assignment_id=_own_assignment_id(state),
                    limit=12,
                )
                if board_view:
                    working_messages = [
                        *working_messages,
                        {
                            "role": "user",
                            "content": (
                                "RESEARCH_COORDINATION_BOARD\n"
                                "This is a compact live coordination view, not a new task. "
                                "Stay inside your owned requirements, reuse completed work, "
                                "and signal incidental cross-scope Evidence instead of "
                                "expanding your scope.\n\n"
                                + json.dumps(board_view, ensure_ascii=False, default=str)
                            ),
                        },
                    ]
                coordination_board.update_agent(
                    state["identity"].root_thread_id,
                    thread_id=state["identity"].thread_id,
                    parent_thread_id=state["identity"].parent_thread_id or "",
                    depth=state["identity"].depth,
                    requirement_ids=_owned_requirement_ids(state),
                    assignment_id=_own_assignment_id(state),
                    parent_assignment_id=_parent_assignment_id(state),
                    status="running",
                    current_action="think_and_plan",
                )
                if allow_fork_tool:
                    coordination_board.record_event(
                        state["identity"].root_thread_id,
                        "fork_tool_offered",
                        actor_thread_id=state["identity"].thread_id,
                        payload={"iteration": state["iteration"]},
                    )
            except Exception as exc:
                working_messages = [
                    *working_messages,
                    {
                        "role": "user",
                        "content": (
                            "RESEARCH_COORDINATION_BOARD unavailable for this turn: "
                            f"{type(exc).__name__}. Continue within the scoped task."
                        ),
                    },
                ]
        control_action_value: str | None = None
        control_rationale: str | None = None
        control_targets: list[str] = []
        control_error: str | None = None
        control_repair_applied = False
        control_event: dict[str, Any] | None = None
        if (
            fork_settings.enabled
            and fork_settings.explicit_control_decision
            and allow_fork_tool
            and not finalization_requested
        ):
            remaining_child_slots = _remaining_child_slots(state)
            remaining_total_slots = (
                max(
                    0,
                    state["limits"].effective_max_total_agents
                    - len(board_view.get("assignment_tree", [])),
                )
                if board_view
                else max(
                    0,
                    state["subtree_thread_budget"] - state["total_threads_used"],
                )
            )
            available_lease_tokens: int | None = None
            if fork_settings.budget_leases_enabled and coordination_board is not None:
                try:
                    available_lease_tokens = coordination_board.metrics(
                        state["identity"].root_thread_id
                    ).get("budget_available_tokens", 0)
                except Exception:
                    available_lease_tokens = 0
            fundable_children = _fundable_child_count(
                state,
                min(remaining_child_slots, remaining_total_slots),
                fork_settings=fork_settings,
                available_lease_tokens=available_lease_tokens,
            )
            displayed_delegable_tokens = (
                available_lease_tokens
                if fork_settings.budget_leases_enabled
                else _delegable_token_budget(state)
            )
            has_research_basis = _has_owned_research_basis(state, board_view)
            decision: ResearchControlDecision | None = (
                ResearchControlDecision(
                    ResearchControlAction.LOCAL_RESEARCH,
                    "No complete Child research loop can be funded from the current "
                    "time, token, tool, and Agent capacity; retain the work locally.",
                    _owned_requirement_ids(state),
                    (),
                )
                if fundable_children <= 0
                else None
            )
            root_baseline_decision: ResearchControlDecision | None = None
            if (
                state["identity"].depth == 0
                and not state.get("child_results")
                and not state.get("completed_fork_fingerprints")
                and not has_research_basis
            ):
                root_baseline_decision = initial_root_requirement_decision(
                    state["research_requirements"],
                    max_children=fundable_children,
                    remaining_total_agent_slots=remaining_total_slots,
                )
            if decision is None:
                control_messages = research_control_prompt(
                    objective=state["task"].objective,
                    requirements=tuple(state["research_requirements"]),
                    coverage=(
                        {
                            **asdict(item),
                            "status": item.status.value,
                        }
                        for item in state.get("coverage", [])
                    ),
                    child_summaries=(
                        {
                            "task_id": item.task_id,
                            "status": item.status.value,
                            "summary": item.summary[:500],
                            "research_memo": item.research_memo[:1500],
                            "unresolved": list(item.unresolved[:4]),
                        }
                        for item in state.get("child_results", [])
                    ),
                    board_view=board_view,
                    depth=state["identity"].depth,
                    max_fork_depth=state["limits"].max_fork_depth,
                    max_children=remaining_child_slots,
                    local_rounds_since_fork=int(
                        state.get("local_rounds_since_fork", 0)
                    ),
                    reconsider_after_local_rounds=(
                        fork_settings.reconsider_after_local_rounds
                    ),
                    remaining_total_agent_slots=remaining_total_slots,
                    delegable_token_budget=displayed_delegable_tokens,
                    delegable_tool_budget=_delegable_tool_budget(
                        state,
                        max(1, fundable_children),
                    ),
                    minimum_child_token_budget=_minimum_fork_token_budget(
                        fork_settings
                    ),
                    fundable_child_count=fundable_children,
                    fork_evidence_basis=has_research_basis,
                )
                try:
                    control_response = await asyncio.wait_for(
                        call_policy(policy, control_messages, []),
                        timeout=max(0.001, available_seconds),
                    )
                    control_charge = _estimate_tokens(
                        control_messages,
                        control_response,
                    )
                    compaction_token_charge += control_charge
                    remaining_tokens = max(0, remaining_tokens - control_charge)
                    raw_control = str(control_response.get("content") or "")
                    valid_control_ids = tuple(
                        item.requirement_id for item in state["research_requirements"]
                    )
                    try:
                        decision = parse_research_control_decision(
                            raw_control,
                            valid_requirement_ids=valid_control_ids,
                        )
                    except ValueError as first_error:
                        repair_messages = [
                            *control_messages,
                            {"role": "assistant", "content": raw_control[:12000]},
                            {
                                "role": "user",
                                "content": (
                                    "REPAIR_CONTROL_STRUCTURE_ONLY\n"
                                    f"Validation error: {first_error}\n"
                                    "Return the same semantic decision as valid JSON. "
                                    "Do not change fork to local_research or vice versa. "
                                    "Allowed Fork reasons are exactly: parallel, "
                                    "context_isolation, deep_tool_chain. Every candidate "
                                    "needs requirement_ids and at least one exact reason."
                                ),
                            },
                        ]
                        repaired_control = await asyncio.wait_for(
                            call_policy(policy, repair_messages, []),
                            timeout=max(0.001, _remaining_research_seconds(state)),
                        )
                        repair_charge = _estimate_tokens(
                            repair_messages,
                            repaired_control,
                        )
                        compaction_token_charge += repair_charge
                        remaining_tokens = max(0, remaining_tokens - repair_charge)
                        decision = parse_research_control_decision(
                            str(repaired_control.get("content") or ""),
                            valid_requirement_ids=valid_control_ids,
                        )
                        control_repair_applied = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    decision = None
                    control_error = f"{type(exc).__name__}: {exc}"
                    control_action_value = ResearchControlAction.LOCAL_RESEARCH.value
                    control_rationale = (
                        "Control decision was invalid or unavailable; continue one "
                        "bounded local round without forcing Fork."
                    )
            if root_baseline_decision is not None:
                decision = ResearchControlDecision(
                    ResearchControlAction.FORK,
                    (
                        decision.rationale
                        if decision is not None
                        else root_baseline_decision.rationale
                    ),
                    root_baseline_decision.target_requirement_ids,
                    root_baseline_decision.fork_candidates,
                )
            if (
                decision is not None
                and should_scout_before_fork(
                    decision,
                    depth=state["identity"].depth,
                    has_research_basis=has_research_basis,
                )
            ):
                decision = ResearchControlDecision(
                    ResearchControlAction.LOCAL_RESEARCH,
                    (
                        "Recursive Fork deferred until this Assignment observes a "
                        "query, source, or Evidence signal; scout locally first."
                    ),
                    decision.target_requirement_ids,
                    (),
                )
            if decision is not None:
                control_action_value = decision.action.value
                control_rationale = decision.rationale
                control_targets = list(decision.target_requirement_ids)
            if coordination_board is not None:
                try:
                    coordination_board.record_event(
                        state["identity"].root_thread_id,
                        (
                            "fork_called"
                            if decision is not None
                            and decision.action is ResearchControlAction.FORK
                            else "fork_not_called"
                        ),
                        actor_thread_id=state["identity"].thread_id,
                        payload={
                            "iteration": state["iteration"],
                            "control_action": control_action_value,
                            "rationale": control_rationale,
                            "error": control_error,
                            "repair_applied": control_repair_applied,
                        },
                    )
                except Exception:
                    pass
            control_event = _event(
                "research_control_decided",
                state["identity"],
                action=control_action_value,
                rationale=control_rationale,
                target_requirement_ids=control_targets,
                error=control_error,
                repair_applied=control_repair_applied,
            )
            if (
                decision is not None
                and decision.action is ResearchControlAction.FORK
            ):
                fork_call = decision_as_fork_tool_call(
                    decision,
                    call_id=f"control-fork-{state['iteration']}",
                )
                return {
                    "pending_tool_calls": [],
                    "pending_fork_calls": [fork_call],
                    "iteration": state["iteration"] + 1,
                    "estimated_tokens_used": min(
                        state["subtree_token_budget"],
                        state["estimated_tokens_used"] + compaction_token_charge,
                    ),
                    "control_action": control_action_value,
                    "control_rationale": control_rationale,
                    "control_target_requirement_ids": control_targets,
                    "control_decision_error": control_error,
                    "control_repair_applied": control_repair_applied,
                    "local_rounds_since_fork": 0,
                    "execution_events": [
                        *state["execution_events"],
                        *([compaction_event] if compaction_event is not None else []),
                        control_event,
                    ],
                }
            if (
                decision is not None
                and decision.action
                in {ResearchControlAction.MERGE, ResearchControlAction.COMPLETE}
            ):
                return {
                    "pending_tool_calls": [],
                    "pending_fork_calls": [],
                    "iteration": state["iteration"] + 1,
                    "last_content": decision.rationale,
                    "draft": None,
                    "draft_raw": "",
                    "estimated_tokens_used": min(
                        state["subtree_token_budget"],
                        state["estimated_tokens_used"] + compaction_token_charge,
                    ),
                    "control_action": control_action_value,
                    "control_rationale": control_rationale,
                    "control_target_requirement_ids": control_targets,
                    "control_decision_error": control_error,
                    "control_repair_applied": control_repair_applied,
                    "local_rounds_since_fork": int(
                        state.get("local_rounds_since_fork", 0)
                    ),
                    "execution_events": [
                        *state["execution_events"],
                        *([compaction_event] if compaction_event is not None else []),
                        control_event,
                    ],
                }
        policy_messages = _bounded_finalization_messages(state) if finalization_requested else working_messages
        estimated_prompt_tokens = max(
            1,
            sum(len(str(item.get("content") or "")) for item in policy_messages) // 4,
        )
        reserve = (
            0
            if finalization_requested
            else (
                _finalization_token_reserve(state)
                + estimated_prompt_tokens
                + _ASSESSMENT_OUTPUT_TOKEN_RESERVE
            )
        )
        if remaining_tokens <= reserve or estimated_prompt_tokens >= max(1, remaining_tokens - reserve):
            update = {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "stop_reason": "token_budget_exhausted",
                "termination_reason": TerminationReason.BUDGET_FORCED,
                "estimated_tokens_used": (state["estimated_tokens_used"] + compaction_token_charge),
                "messages": working_messages,
                "compression_manifests": compression_manifests,
                "semantic_memos": semantic_memos,
            }
            if compaction_event is not None:
                update["execution_events"] = [
                    *state["execution_events"],
                    compaction_event,
                ]
            if control_event is not None:
                update["execution_events"] = [
                    *state["execution_events"],
                    *([compaction_event] if compaction_event is not None else []),
                    control_event,
                ]
            if finalization_requested:
                update["finalization_requested"] = False
            return update

        with _node_trace("think_and_plan", state) as observation:
            # Tool availability can be bound per async research run, so resolve
            # schemas here instead of freezing a deny-all scope at graph compile.
            schemas = (
                []
                if finalization_requested
                else [
                    *_tool_schemas(tool_list),
                    *(
                        [fork_tool_schema()]
                        if allow_fork_tool
                        and not fork_settings.explicit_control_decision
                        else []
                    ),
                    *([_cross_scope_signal_schema()] if coordination_board is not None else []),
                ]
            )
            invocation_policy = policy
            root_direct_final = bool(
                finalization_requested and state["identity"].depth == 0
            )
            if root_direct_final:
                invocation_policy = _root_final_policy_instance(
                    policy,
                    fork_settings.root_final_max_tokens,
                )
            action_retries = 0
            while True:
                try:
                    response = await asyncio.wait_for(
                        call_policy(invocation_policy, policy_messages, schemas),
                        timeout=max(0.001, available_seconds),
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    observation.set_error("time budget exhausted")
                    update = {
                        "pending_tool_calls": [],
                        "pending_fork_calls": [],
                        "stop_reason": "time_budget_exhausted",
                        "termination_reason": TerminationReason.BUDGET_FORCED,
                        "iteration": state["iteration"] + 1,
                        "retries_used": state["retries_used"] + action_retries,
                        "messages": working_messages,
                        "compression_manifests": compression_manifests,
                        "semantic_memos": semantic_memos,
                    }
                    if finalization_requested:
                        update["finalization_requested"] = False
                    return update
                except Exception as exc:
                    retry_available = min(
                        state["limits"].max_retries_per_action - action_retries,
                        state["subtree_retry_budget"] - state["retries_used"],
                    )
                    if retry_available <= 0:
                        observation.set_error(str(exc))
                        update = {
                            "pending_tool_calls": [],
                            "pending_fork_calls": [],
                            "stop_reason": f"policy_error: {exc}",
                            "termination_reason": TerminationReason.TOOL_FAILURE,
                            "iteration": state["iteration"] + 1,
                            "retries_used": state["retries_used"] + action_retries,
                            "messages": working_messages,
                            "compression_manifests": compression_manifests,
                            "semantic_memos": semantic_memos,
                        }
                        if finalization_requested:
                            update["finalization_requested"] = False
                        return update
                    action_retries += 1

            continuation_charge = 0
            continuation_count = 0
            if root_direct_final:
                combined_content = str(response.get("content") or "")
                finish_reason = str(response.get("finish_reason") or "")
                output_tokens_used = _completion_tokens(response)
                while (
                    finish_reason == "length"
                    and output_tokens_used < fork_settings.root_final_output_token_budget
                    and _remaining_finalization_seconds(state) > 0
                ):
                    remaining_output = (
                        fork_settings.root_final_output_token_budget
                        - output_tokens_used
                    )
                    continuation_policy = _root_final_policy_instance(
                        policy,
                        min(
                            fork_settings.root_final_max_tokens,
                            remaining_output,
                        ),
                    )
                    continuation_messages = [
                        {
                            "role": "system",
                            "content": (
                                "Continue an incomplete Markdown research report. "
                                "Return only the continuation, without repeating prior "
                                "text, without JSON, and without a References section."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"OBJECTIVE:\n{state['task'].objective}\n\n"
                                "Continue immediately after this report tail and finish "
                                "all remaining sections:\n\n"
                                + combined_content[-12000:]
                            ),
                        },
                    ]
                    next_response = await asyncio.wait_for(
                        call_policy(continuation_policy, continuation_messages, []),
                        timeout=max(0.001, _remaining_finalization_seconds(state)),
                    )
                    next_content = str(next_response.get("content") or "").strip()
                    if not next_content:
                        break
                    combined_content = (
                        combined_content.rstrip() + "\n\n" + next_content
                    )
                    continuation_charge += _estimate_tokens(
                        continuation_messages,
                        next_response,
                    )
                    output_tokens_used += _completion_tokens(next_response)
                    finish_reason = str(next_response.get("finish_reason") or "")
                    continuation_count += 1
                response = {
                    **response,
                    "content": combined_content,
                    "finish_reason": finish_reason or response.get("finish_reason"),
                }

            token_charge = min(
                _estimate_tokens(policy_messages, response) + continuation_charge,
                remaining_tokens,
            )
            token_exhausted = token_charge >= remaining_tokens

            content = str(response.get("content") or "")
            calls = [] if finalization_requested else _normalize_tool_calls(response.get("tool_calls"))
            if (
                coordination_board is not None
                and allow_fork_tool
                and not finalization_requested
                and not fork_settings.explicit_control_decision
            ):
                fork_called = any(
                    call["function"].get("name") == FORK_TOOL_NAME
                    for call in calls
                )
                try:
                    coordination_board.record_event(
                        state["identity"].root_thread_id,
                        "fork_called" if fork_called else "fork_not_called",
                        actor_thread_id=state["identity"].thread_id,
                        payload={
                            "iteration": state["iteration"],
                            "selected_tools": [
                                call["function"].get("name") for call in calls
                            ],
                        },
                    )
                except Exception:
                    pass
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if calls:
                assistant_message["tool_calls"] = calls
            if response.get("reasoning_content"):
                assistant_message["reasoning_content"] = response["reasoning_content"]

            update: dict[str, Any] = {
                "messages": [*working_messages, assistant_message],
                "iteration": state["iteration"] + 1,
                "last_content": content,
                "pending_stop_reason": None,
                "finalization_requested": False,
                "estimated_tokens_used": (state["estimated_tokens_used"] + compaction_token_charge + token_charge),
                "retries_used": state["retries_used"] + action_retries,
                "compression_manifests": compression_manifests,
                "semantic_memos": semantic_memos,
            }
            if control_action_value is not None:
                update.update({
                    "control_action": control_action_value,
                    "control_rationale": control_rationale,
                    "control_target_requirement_ids": control_targets,
                    "control_decision_error": control_error,
                    "control_repair_applied": control_repair_applied,
                    "local_rounds_since_fork": (
                        int(state.get("local_rounds_since_fork", 0)) + 1
                        if control_action_value
                        == ResearchControlAction.LOCAL_RESEARCH.value
                        else int(state.get("local_rounds_since_fork", 0))
                    ),
                })
            if (
                compaction_event is not None
                or control_event is not None
                or continuation_count
            ):
                update["execution_events"] = [
                    *state["execution_events"],
                    *([compaction_event] if compaction_event is not None else []),
                    *([control_event] if control_event is not None else []),
                    *(
                        [
                            _event(
                                "root_report_continued",
                                state["identity"],
                                continuation_count=continuation_count,
                                finish_reason=response.get("finish_reason"),
                            )
                        ]
                        if continuation_count else []
                    ),
                ]
            if token_exhausted:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft_raw"] = content
                update["draft"] = (
                    _root_markdown_draft(state, content)
                    if root_direct_final
                    else _try_parse_final_draft(content)
                )
                update["stop_reason"] = "token_budget_exhausted"
                update["termination_reason"] = TerminationReason.BUDGET_FORCED
                observation.add_output({"action": "stop", "reason": "token_budget"})
                return update
            if finalization_requested:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft_raw"] = content
                update["draft"] = (
                    _root_markdown_draft(state, content)
                    if root_direct_final
                    else _try_parse_final_draft(content)
                )
                observation.add_output({"action": "finalize_output"})
                return update
            if not calls:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft_raw"] = content
                update["draft"] = _try_parse_final_draft(content)
                observation.add_output({"action": "assess_candidate"})
                return update

            fork_calls = (
                [
                    call
                    for call in calls
                    if call["function"].get("name") == FORK_TOOL_NAME
                ]
                if allow_fork_tool
                else []
            )
            if fork_calls:
                assistant_message["tool_calls"] = fork_calls
                update["messages"] = [*working_messages, assistant_message]
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = fork_calls
                observation.add_output({"action": "fork_children", "fork_calls": len(fork_calls)})
                return update

            update["pending_fork_calls"] = []
            remaining = min(
                state["limits"].max_tool_calls - state["tool_calls_used"],
                state["subtree_tool_budget"] - state["total_tool_calls_used"],
            )
            if remaining <= 0:
                update["pending_tool_calls"] = []
                update["stop_reason"] = "max_tool_calls_exhausted"
                update["termination_reason"] = TerminationReason.BUDGET_FORCED
                observation.add_output({"action": "stop", "reason": "tool_budget"})
                return update
            if len(calls) > remaining:
                update["pending_tool_calls"] = calls[:remaining]
                update["pending_stop_reason"] = "max_tool_calls_exhausted"
            else:
                update["pending_tool_calls"] = calls
            observation.add_output({"action": "use_tool", "tool_calls": len(update["pending_tool_calls"])})
            return update

    async def assess_research_state(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """Use the same policy for a validated Continue/Replan/Stop decision."""
        _validate_invocation(state, config)
        evidence = _deduplicate_evidence(state["observed_evidence"])
        evidence_count = len(evidence)
        previous_count = int(state.get("last_assessed_evidence_count", 0))
        new_evidence_ids = tuple(item.evidence_id for item in evidence[previous_count:])
        attempts = tuple(state.get("strategy_attempts", []))
        hard_reason = hard_termination_reason(state.get("stop_reason"))
        forced_stop_reason: str | None = None
        if hard_reason is None and state.get("termination_reason") in {
            TerminationReason.BUDGET_FORCED,
            TerminationReason.TOOL_FAILURE,
            TerminationReason.USER_CANCELLED,
        }:
            hard_reason = state["termination_reason"]
        recent_tool_failures = tuple(
            str(event.get("error") or event.get("tool") or "tool failure")
            for event in state["execution_events"][-12:]
            if (event.get("kind") == "tool_finished" and not event.get("ok", False))
            or event.get("kind") == "tool_rejected_unbound"
        )
        recent_tool_outcomes = tuple(
            {
                "name": str(message.get("name") or "tool"),
                "content": str(message.get("content") or "")[:1000],
            }
            for message in state["messages"]
            if message.get("role") == "tool"
        )[-8:]
        assessment_error: str | None = None
        token_charge = 0
        if hard_reason is not None:
            assessment = _fallback_assessment(
                state,
                attempts=attempts,
                hard_reason=hard_reason,
                has_research_tools=bool(tool_list),
            )
        elif (runtime_exhaustion := _runtime_exhaustion_assessment(state, attempts)):
            assessment = runtime_exhaustion
        else:
            prompt = assessment_schema_prompt(
                task=state["task"],
                requirements=tuple(state["research_requirements"]),
                coverage=tuple(state["coverage"]),
                evidence=evidence,
                critical_gaps=tuple(state["critical_gaps"]),
                attempts=attempts,
                child_results=tuple(state.get("child_results", [])),
                candidate_final=state.get("draft_raw", ""),
                recent_tool_failures=recent_tool_failures,
                recent_tool_outcomes=recent_tool_outcomes,
                focus_evidence_ids=new_evidence_ids,
            )
            assessment_messages = [
                {
                    "role": "system",
                    "content": (
                        "You are the sufficiency-assessment step inside the same "
                        "PaperPilot Research AgentGraph. Return structured JSON only."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
            remaining_tokens = state["subtree_token_budget"] - state["estimated_tokens_used"]
            assessment_input_tokens = max(
                1,
                sum(len(str(item.get("content") or "")) for item in assessment_messages) // 4,
            )
            finalization_reserve = _finalization_token_reserve(state)
            if remaining_tokens <= (assessment_input_tokens + _ASSESSMENT_OUTPUT_TOKEN_RESERVE + finalization_reserve):
                forced_stop_reason = "token_budget_exhausted"
                hard_reason = TerminationReason.BUDGET_FORCED
                assessment = _fallback_assessment(
                    state,
                    attempts=attempts,
                    hard_reason=hard_reason,
                    has_research_tools=bool(tool_list),
                )
            else:
                try:
                    if _remaining_research_seconds(state) <= 0:
                        raise asyncio.TimeoutError
                    response = await asyncio.wait_for(
                        call_policy(policy, assessment_messages, []),
                        timeout=max(0.001, _remaining_research_seconds(state)),
                    )
                    token_charge += _estimate_tokens(assessment_messages, response)
                    raw_assessment = str(response.get("content") or "")
                    try:
                        assessment = parse_research_assessment(
                            raw_assessment,
                            requirements=tuple(state["research_requirements"]),
                            evidence=evidence,
                            attempts=attempts,
                            require_evidence=state["task"].require_evidence,
                        )
                    except AssessmentValidationError as exc:
                        assessment_error = str(exc)
                        repair_messages = [
                            assessment_messages[0],
                            assessment_messages[1],
                            {"role": "assistant", "content": raw_assessment},
                            {
                                "role": "user",
                                "content": repair_assessment_prompt(
                                    raw_assessment,
                                    str(exc),
                                    evidence_bindings={
                                        item.evidence_id: item.requirement_id for item in evidence
                                    },
                                ),
                            },
                        ]
                        repaired = await asyncio.wait_for(
                            call_policy(policy, repair_messages, []),
                            timeout=max(0.001, _remaining_research_seconds(state)),
                        )
                        token_charge += _estimate_tokens(repair_messages, repaired)
                        assessment = parse_research_assessment(
                            str(repaired.get("content") or ""),
                            requirements=tuple(state["research_requirements"]),
                            evidence=evidence,
                            attempts=attempts,
                            require_evidence=state["task"].require_evidence,
                            output_status=OutputStatus.REPAIRED,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    assessment_error = f"{type(exc).__name__}: {exc}"
                    assessment = _fallback_assessment(
                        state,
                        attempts=attempts,
                        has_research_tools=bool(tool_list),
                        assessment_error=assessment_error,
                    )

        reconciled_attempts = (
            reconcile_strategy_attempt_outcomes(attempts, assessment.coverage)
            if assessment.output_status != OutputStatus.FALLBACK
            else attempts
        )
        scheduled_actions = merge_next_action_queue(
            state.get("next_actions", []),
            assessment,
            active_consumed=(len(attempts) > state.get("last_assessed_strategy_attempt_count", 0)),
        )
        messages = list(state["messages"])
        draft = state.get("draft")
        draft_raw = state.get("draft_raw", "")
        finalization_requested = False
        if assessment.decision in {ResearchDecision.CONTINUE, ResearchDecision.REPLAN}:
            messages.append(
                {
                    "role": "user",
                    "content": control_message(
                        assessment,
                        scheduled_actions=scheduled_actions,
                    ),
                }
            )
            draft = None
            draft_raw = ""
        elif draft is None:
            messages.append({"role": "user", "content": finalization_prompt(assessment)})
            finalization_requested = True
            draft_raw = ""

        if coordination_board is not None and state["identity"].depth == 0:
            for item in assessment.coverage:
                try:
                    coordination_board.update_requirement_coverage(
                        state["identity"].root_thread_id,
                        item.requirement_id,
                        status=item.status.value,
                        evidence_ids=item.evidence_ids,
                        rationale=item.rationale,
                        remaining_gap=item.remaining_gap or "",
                        actor_thread_id=state["identity"].thread_id,
                    )
                except Exception:
                    # The checkpointed assessment remains authoritative if the
                    # passive coordination view is temporarily unavailable.
                    pass

        update: dict[str, Any] = {
            "messages": messages,
            "draft": draft,
            "draft_raw": draft_raw,
            "last_assessed_evidence_count": evidence_count,
            "research_requirements": list(state["research_requirements"]),
            "coverage": list(assessment.coverage),
            "critical_gaps": list(assessment.critical_gaps),
            "next_actions": list(scheduled_actions),
            "strategy_attempts": list(reconciled_attempts),
            "last_assessed_strategy_attempt_count": len(reconciled_attempts),
            "assessment_decision": assessment.decision,
            "assessment_output_status": assessment.output_status,
            "assessment_error": assessment_error,
            "termination_reason": assessment.termination_reason,
            "finalization_requested": finalization_requested,
            "estimated_tokens_used": min(
                state["subtree_token_budget"],
                state["estimated_tokens_used"] + token_charge,
            ),
            "execution_events": [
                *state["execution_events"],
                _event(
                    "research_state_assessed",
                    state["identity"],
                    decision=assessment.decision.value,
                    termination_reason=(
                        assessment.termination_reason.value if assessment.termination_reason is not None else None
                    ),
                    evidence_count=evidence_count,
                    new_evidence_count=len(new_evidence_ids),
                    critical_gap_count=len(assessment.critical_gaps),
                    next_action_count=len(scheduled_actions),
                    assessment_output_status=assessment.output_status.value,
                    assessment_error=assessment_error,
                ),
            ],
        }
        if forced_stop_reason is not None:
            update["stop_reason"] = forced_stop_reason
        return update

    async def _execute_pending_tools(
        state: ResearchAgentState,
    ) -> dict[str, Any]:
        new_messages: list[dict[str, Any]] = []
        collected = list(state["observed_evidence"])
        local_tool_calls = state["tool_calls_used"]
        total_tool_calls = state["total_tool_calls_used"]
        retries_used = state["retries_used"]
        source_candidate_count = int(state.get("source_candidate_count", 0))
        source_open_count = int(state.get("source_open_count", 0))
        duplicate_source_count = int(state.get("duplicate_source_count", 0))
        acquisition_call_count = int(state.get("acquisition_call_count", 0))
        strategy_attempts = list(state.get("strategy_attempts", []))
        events = list(state["execution_events"])
        stop_reason = state["pending_stop_reason"]

        notepad = tool_map.get("notepad")
        notepad_scope = (
            notepad.bind_snapshot(state.get("notepad_entries", [])) if isinstance(notepad, NotepadTool) else None
        )

        def _checkpointed_notepad() -> list[dict[str, Any]]:
            if isinstance(notepad, NotepadTool):
                return notepad.to_dict()
            return list(state.get("notepad_entries", []))

        if notepad_scope is not None:
            notepad_scope.__enter__()
        try:
            for call in state["pending_tool_calls"]:
                if (
                    local_tool_calls >= state["limits"].max_tool_calls
                    or total_tool_calls >= state["subtree_tool_budget"]
                ):
                    stop_reason = "max_tool_calls_exhausted"
                    break
                function = call["function"]
                tool_name = str(function.get("name", ""))
                raw_arguments = function.get("arguments", "{}")
                if isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                else:
                    try:
                        arguments = json.loads(str(raw_arguments or "{}"))
                    except json.JSONDecodeError:
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}

                if (
                    tool_name == _CROSS_SCOPE_SIGNAL_TOOL_NAME
                    and coordination_board is not None
                ):
                    evidence_id = str(arguments.get("evidence_id") or "").strip()
                    target_requirement_id = str(
                        arguments.get("target_requirement_id") or ""
                    ).strip()
                    known_evidence_ids = {item.evidence_id for item in collected}
                    board_plan = coordination_board.snapshot(
                        state["identity"].root_thread_id,
                        viewer_thread_id=state["identity"].thread_id,
                    ).get("plan", {})
                    valid_requirement_ids = {
                        str(item.get("requirement_id") or "")
                        for item in board_plan.get("requirements", [])
                        if isinstance(item, dict)
                    }
                    own_ids = set(_owned_requirement_ids(state))
                    if evidence_id not in known_evidence_ids:
                        signal_result = {
                            "status": "rejected",
                            "error": "cross-scope signal requires already-collected Evidence",
                        }
                    elif target_requirement_id not in valid_requirement_ids:
                        signal_result = {
                            "status": "rejected",
                            "error": "unknown target requirement",
                        }
                    elif target_requirement_id in own_ids:
                        signal_result = {
                            "status": "rejected",
                            "error": "target requirement is already owned by this Agent",
                        }
                    else:
                        signal_id = coordination_board.publish_signal(
                            state["identity"].root_thread_id,
                            evidence_id=evidence_id,
                            discovered_by=state["identity"].thread_id,
                            target_requirement_id=target_requirement_id,
                            parent_thread_id=state["identity"].parent_thread_id or "",
                            message=str(arguments.get("message") or ""),
                        )
                        signal_result = {
                            "status": "published",
                            "signal_id": signal_id,
                            "target_requirement_id": target_requirement_id,
                        }
                        events.append(
                            _event(
                                "cross_scope_signal_published",
                                state["identity"],
                                signal_id=signal_id,
                                evidence_id=evidence_id,
                                target_requirement_id=target_requirement_id,
                            )
                        )
                    new_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": tool_name,
                            "content": _serialize_tool_result(
                                signal_result,
                                state["limits"].max_tool_output_chars,
                            ),
                        }
                    )
                    continue

                tool = tool_map.get(tool_name)
                scheduled_actions = active_next_actions(state.get("next_actions", []))
                if not scheduled_actions and state.get("assessment_decision") is None and tool_name != "notepad":
                    argument_query = str(arguments.get("query") or "").strip()
                    if not argument_query:
                        argument_query = " ".join(
                            str(value).strip()
                            for value in arguments.values()
                            if isinstance(value, (str, int, float)) and str(value).strip()
                        )
                    bootstrap_requirements = tuple(
                        requirement
                        for requirement in state["research_requirements"]
                        if requirement.required and requirement.requires_external_evidence
                    )
                    if not argument_query and bootstrap_requirements and _tool_accepts_empty_arguments(tool):
                        argument_query = bootstrap_requirements[0].description
                    if argument_query and bootstrap_requirements:
                        scheduled_actions = (
                            _identified_action(
                                NextResearchAction(
                                    bootstrap_requirements[0].requirement_id,
                                    "other",
                                    argument_query,
                                    "high",
                                    "Establish initial source-locatable evidence.",
                                )
                            ),
                        )
                matched_action = _action_for_tool_call(
                    tool_name,
                    arguments,
                    scheduled_actions,
                )
                if (
                    matched_action is not None
                    and getattr(tool, "accepts_relevance_query", False)
                    and not str(arguments.get("query") or "").strip()
                ):
                    # Browser can rank late sections only when it receives the
                    # action-bound Core Question. Third-party tools are left
                    # untouched unless they explicitly advertise support.
                    arguments = {**arguments, "query": matched_action.query}
                if tool_name != "notepad" and matched_action is None:
                    error = (
                        "research tool call rejected because it is not bound to the "
                        "single active requirement/action/strategy"
                    )
                    events.append(
                        _event(
                            "tool_rejected_unbound",
                            state["identity"],
                            tool=tool_name,
                            error=error,
                        )
                    )
                    new_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": tool_name,
                            "content": _serialize_tool_result(
                                {"error": error},
                                state["limits"].max_tool_output_chars,
                            ),
                        }
                    )
                    continue
                board_query = ""
                board_query_claimed = False
                if (
                    coordination_board is not None
                    and tool_name == "acquire_evidence"
                    and matched_action is not None
                ):
                    board_query = str(
                        arguments.get("query") or matched_action.query
                    ).strip()
                    try:
                        query_claim = coordination_board.claim_query(
                            state["identity"].root_thread_id,
                            requirement_id=matched_action.requirement_id,
                            owner_thread_id=state["identity"].thread_id,
                            assignment_id=_own_assignment_id(state),
                            parent_assignment_id=_parent_assignment_id(state),
                            query=board_query,
                            lease_seconds=_BLACKBOARD_LEASE_SECONDS,
                        )
                    except Exception as exc:
                        events.append(
                            _event(
                                "blackboard_query_claim_failed",
                                state["identity"],
                                error=f"{type(exc).__name__}: {exc}",
                                requirement_id=matched_action.requirement_id,
                            )
                        )
                    else:
                        if not query_claim.acquired:
                            strategy_attempts.append(
                                StrategyAttempt(
                                    requirement_id=matched_action.requirement_id,
                                    strategy=matched_action.strategy,
                                    query=board_query,
                                    outcome="no_progress",
                                    action_id=_action_id(matched_action),
                                    artifact_ids=query_claim.artifact_ids,
                                )
                            )
                            events.append(
                                _event(
                                    "blackboard_query_reused"
                                    if query_claim.reason == "completed"
                                    else "blackboard_query_skipped_running",
                                    state["identity"],
                                    requirement_id=matched_action.requirement_id,
                                    owner_thread_id=query_claim.owner_thread_id,
                                    artifact_ids=list(query_claim.artifact_ids),
                                )
                            )
                            new_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "name": tool_name,
                                    "content": _serialize_tool_result(
                                        {
                                            "status": "reused"
                                            if query_claim.reason == "completed"
                                            else "in_progress_elsewhere",
                                            "owner_thread_id": query_claim.owner_thread_id,
                                            "artifact_ids": list(query_claim.artifact_ids),
                                            "coordination": (
                                                "Do not repeat this query; use existing "
                                                "Evidence or continue with a distinct strategy."
                                            ),
                                        },
                                        state["limits"].max_tool_output_chars,
                                    ),
                                }
                            )
                            continue
                        board_query_claimed = True
                open_circuit = next(
                    (
                        event
                        for event in reversed(events)
                        if event.get("kind") == "tool_unavailable"
                        and event.get("tool") == tool_name
                        and event.get("circuit_open") is True
                    ),
                    None,
                )
                if open_circuit is not None:
                    error = (
                        f"tool unavailable circuit is open: "
                        f"{open_circuit.get('message') or tool_name}"
                    )
                    if matched_action is not None:
                        strategy_attempts.append(
                            StrategyAttempt(
                                requirement_id=matched_action.requirement_id,
                                strategy=matched_action.strategy,
                                query=str(arguments.get("query") or matched_action.query),
                                outcome="no_progress",
                                action_id=_action_id(matched_action),
                            )
                        )
                    events.append(
                        _event(
                            "tool_call_skipped_unavailable",
                            state["identity"],
                            tool=tool_name,
                            alert_id=open_circuit.get("alert_id"),
                            error=error,
                            requirement_id=(
                                matched_action.requirement_id
                                if matched_action is not None
                                else None
                            ),
                        )
                    )
                    new_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": tool_name,
                            "content": _serialize_tool_result(
                                {
                                    "error": error,
                                    "availability_alert_id": open_circuit.get(
                                        "alert_id"
                                    ),
                                },
                                state["limits"].max_tool_output_chars,
                            ),
                        }
                    )
                    continue
                result: Any
                error: str | None = None
                action_retries = 0
                permanent_error = False
                with trace_block(
                    f"research_agent.tool.{tool_name or 'unknown'}",
                    run_type="tool",
                    inputs={"tool": tool_name, **_identity_metadata(state["identity"])},
                    tags=["paperpilot", "research-agent", "tool"],
                ) as observation:
                    while True:
                        if _remaining_research_seconds(state) <= 0:
                            error = "time budget exhausted"
                            result = {"error": error}
                            stop_reason = "time_budget_exhausted"
                            break
                        local_tool_calls += 1
                        total_tool_calls += 1
                        if tool is None:
                            error = f"unknown tool: {tool_name}"
                            result = {"error": error}
                        else:
                            try:
                                execution = tool.execute(**arguments)
                                if inspect.isawaitable(execution):
                                    result = await asyncio.wait_for(
                                        execution,
                                        timeout=max(
                                            0.001,
                                            _remaining_research_seconds(state),
                                        ),
                                    )
                                else:
                                    result = execution
                                error = _semantic_tool_error(tool_name, result)
                                if error is not None:
                                    permanent_error = True
                            except asyncio.CancelledError:
                                raise
                            except asyncio.TimeoutError:
                                error = "time budget exhausted"
                                result = {"error": error}
                                stop_reason = "time_budget_exhausted"
                            except FileReaderError as exc:
                                error = str(exc)
                                result = {"error": error}
                                permanent_error = True
                            except Exception as exc:
                                error = str(exc)
                                result = {"error": error}

                        immediate_alert = classify_tool_availability(
                            tool_name,
                            error,
                            arguments,
                        )
                        if (
                            immediate_alert is not None
                            and immediate_alert.category != "service_degraded"
                        ):
                            # Quota, auth, adapter, blocked-source, and TLS failures
                            # will not improve by immediately repeating the same call.
                            permanent_error = True

                        retry_available = min(
                            state["limits"].max_retries_per_action - action_retries,
                            state["subtree_retry_budget"] - retries_used,
                        )
                        call_available = min(
                            state["limits"].max_tool_calls - local_tool_calls,
                            state["subtree_tool_budget"] - total_tool_calls,
                        )
                        if error is None or permanent_error or stop_reason == "time_budget_exhausted":
                            break
                        if retry_available <= 0 or call_available <= 0:
                            break
                        action_retries += 1
                        retries_used += 1

                    if error:
                        observation.set_error(error)
                    else:
                        observation.add_output({"ok": True, "retries": action_retries})

                availability_alert = classify_tool_availability(
                    tool_name,
                    error,
                    arguments,
                )
                fallback_alerts = (
                    classify_fallback_backend_alerts(
                        tool_name,
                        result,
                        arguments,
                    )
                    if error is None
                    else ()
                )
                if availability_alert is not None and isinstance(result, dict):
                    result = {
                        **result,
                        "availability_alert": asdict(availability_alert),
                    }
                if fallback_alerts and isinstance(result, dict):
                    result = {
                        **result,
                        "availability_alerts": [
                            asdict(alert) for alert in fallback_alerts
                        ],
                    }
                artifact_id = _tool_artifact_id(tool_name, arguments, result)
                artifact_reread = (
                    tool_name == "file_reader"
                    and arguments.get("root") == "artifact"
                    and isinstance(arguments.get("path"), str)
                    and re.fullmatch(r"artifact-[0-9a-f]{16}\.json", str(arguments["path"])) is not None
                )
                if artifact_reread:
                    artifact_id = str(arguments["path"])[:-5]
                if (
                    coordination_board is not None
                    and board_query
                    and board_query_claimed
                ):
                    try:
                        coordination_board.complete_query(
                            state["identity"].root_thread_id,
                            board_query,
                            owner_thread_id=state["identity"].thread_id,
                            artifact_ids=(artifact_id,),
                            failed=error is not None,
                        )
                    except Exception as exc:
                        events.append(
                            _event(
                                "blackboard_query_completion_failed",
                                state["identity"],
                                error=f"{type(exc).__name__}: {exc}",
                                artifact_id=artifact_id,
                            )
                        )
                if tool_name == "acquire_evidence" and isinstance(result, dict):
                    acquisition_metrics = result.get("metrics", {})
                    if isinstance(acquisition_metrics, dict):
                        source_candidate_count += max(
                            0, int(acquisition_metrics.get("candidate_count", 0))
                        )
                        source_open_count += max(
                            0, int(acquisition_metrics.get("opened_count", 0))
                        )
                        duplicate_source_count += max(
                            0,
                            int(acquisition_metrics.get("duplicate_candidate_count", 0))
                            + int(acquisition_metrics.get("cache_hit_count", 0)),
                        )
                    acquisition_call_count += 1
                    if coordination_board is not None:
                        documents_by_url = {
                            canonicalize_source_url(str(item.get("url") or "")): item
                            for item in result.get("documents", [])
                            if isinstance(item, dict) and item.get("url")
                        }
                        errors_by_url = {
                            canonicalize_source_url(str(item.get("url") or "")): item
                            for item in result.get("fetch_errors", [])
                            if isinstance(item, dict) and item.get("url")
                        }
                        for selected_url in result.get("selected_urls", []):
                            canonical_url = canonicalize_source_url(str(selected_url))
                            try:
                                source_claim = coordination_board.claim_source(
                                    state["identity"].root_thread_id,
                                    owner_thread_id=state["identity"].thread_id,
                                    requirement_id=(
                                        matched_action.requirement_id
                                        if matched_action is not None else ""
                                    ),
                                    assignment_id=_own_assignment_id(state),
                                    parent_assignment_id=_parent_assignment_id(state),
                                    url=canonical_url,
                                    lease_seconds=_BLACKBOARD_LEASE_SECONDS,
                                )
                                if source_claim.acquired:
                                    fetch_error = errors_by_url.get(canonical_url)
                                    coordination_board.complete_source(
                                        state["identity"].root_thread_id,
                                        canonical_url,
                                        owner_thread_id=state["identity"].thread_id,
                                        artifact_id=(
                                            artifact_id
                                            if canonical_url in documents_by_url else ""
                                        ),
                                        error=(
                                            str(fetch_error.get("error") or "fetch failed")
                                            if fetch_error is not None else ""
                                        ),
                                    )
                            except Exception as exc:
                                events.append(
                                    _event(
                                        "blackboard_source_update_failed",
                                        state["identity"],
                                        source_ref=canonical_url,
                                        error=f"{type(exc).__name__}: {exc}",
                                    )
                                )
                extracted: list[EvidenceItem] = []
                if error is None and matched_action is not None:
                    extracted = [
                        replace(
                            item,
                            assignment_id=_own_assignment_id(state),
                            parent_assignment_id=_parent_assignment_id(state),
                        )
                        for item in _extract_evidence(
                            tool_name,
                            arguments,
                            result,
                            action=matched_action,
                            artifact_id=artifact_id,
                        )
                    ]
                    known_ids = {item.evidence_id for item in collected}
                    new_ids = tuple(item.evidence_id for item in extracted if item.evidence_id not in known_ids)
                    collected.extend(extracted)
                else:
                    new_ids = ()
                if matched_action is not None:
                    strategy_attempts.append(
                        StrategyAttempt(
                            requirement_id=matched_action.requirement_id,
                            strategy=matched_action.strategy,
                            query=str(arguments.get("query") or matched_action.query),
                            outcome="evidence_found" if new_ids else "no_progress",
                            evidence_ids=new_ids,
                            action_id=_action_id(matched_action),
                            artifact_ids=(artifact_id,),
                        )
                    )
                events.append(
                    _event(
                        "tool_finished",
                        state["identity"],
                        tool=tool_name,
                        ok=error is None,
                        retries=action_retries,
                        error=error,
                        artifact_id=artifact_id,
                        requirement_id=(matched_action.requirement_id if matched_action is not None else None),
                        action_id=(_action_id(matched_action) if matched_action is not None else None),
                        assignment_id=_own_assignment_id(state),
                        parent_assignment_id=_parent_assignment_id(state),
                        strategy=(matched_action.strategy if matched_action is not None else None),
                    )
                )
                if availability_alert is not None and not any(
                    event.get("kind") == "tool_unavailable"
                    and event.get("alert_id") == availability_alert.alert_id
                    for event in events
                ):
                    events.append(
                        _event(
                            "tool_unavailable",
                            state["identity"],
                            **asdict(availability_alert),
                        )
                    )
                for fallback_alert in fallback_alerts:
                    if any(
                        event.get("kind") == "tool_unavailable"
                        and event.get("alert_id") == fallback_alert.alert_id
                        for event in events
                    ):
                        continue
                    events.append(
                        _event(
                            "tool_unavailable",
                            state["identity"],
                            **asdict(fallback_alert),
                        )
                    )
                full_tool_result = json.dumps(
                    result,
                    ensure_ascii=False,
                    default=str,
                )
                tool_message_content: str
                if artifact_reread and isinstance(result, dict):
                    tool_message_content = json.dumps(
                        {
                            "status": "artifact_reread",
                            "artifact_id": artifact_id,
                            "artifact_path": str(result.get("path") or ""),
                            "artifact_read_root": "artifact",
                            "artifact_read_path": str(arguments["path"]),
                            "content_hash": str(result.get("content_hash") or ""),
                            "preview": str(result.get("content") or ""),
                            "truncated": bool(result.get("truncated", False)),
                        },
                        ensure_ascii=False,
                    )
                elif tool_artifact_store is not None and tool_name != "notepad":
                    try:
                        receipt = await asyncio.to_thread(
                            tool_artifact_store.persist_tool_artifact,
                            artifact_id,
                            tool_name=tool_name,
                            arguments=arguments,
                            result=result,
                            origin_thread_id=state["identity"].thread_id,
                            artifact_scope_id=state["identity"].root_thread_id,
                        )
                        required_receipt = (
                            receipt.get("artifact_id") == artifact_id
                            and isinstance(receipt.get("artifact_path"), str)
                            and isinstance(receipt.get("content_hash"), str)
                            and len(str(receipt.get("content_hash"))) == 64
                            and isinstance(receipt.get("size_bytes"), int)
                        )
                        if not required_receipt:
                            raise RuntimeError("invalid tool artifact receipt")
                        preview_limit = (
                            1200 if len(full_tool_result) > _TOOL_ARTIFACT_OFFLOAD_CHARS else len(full_tool_result)
                        )
                        tool_message_content = json.dumps(
                            {
                                "status": "offloaded",
                                "artifact_id": artifact_id,
                                "artifact_path": receipt["artifact_path"],
                                "artifact_read_root": "artifact",
                                "artifact_read_path": f"{artifact_id}.json",
                                "content_hash": receipt["content_hash"],
                                "size_bytes": receipt["size_bytes"],
                                "evidence_ids": list(new_ids),
                                "preview": full_tool_result[:preview_limit],
                            },
                            ensure_ascii=False,
                        )
                        events.append(
                            _event(
                                "tool_artifact_offloaded",
                                state["identity"],
                                artifact_id=artifact_id,
                                artifact_path=receipt["artifact_path"],
                                content_hash=receipt["content_hash"],
                                size_bytes=receipt["size_bytes"],
                            )
                        )
                    except Exception as exc:
                        # Losslessness wins over context size: if durable storage
                        # cannot be proven, keep the complete original payload.
                        tool_message_content = full_tool_result
                        events.append(
                            _event(
                                "tool_artifact_offload_failed",
                                state["identity"],
                                artifact_id=artifact_id,
                                error=str(exc),
                            )
                        )
                else:
                    tool_message_content = _serialize_tool_result(
                        result,
                        state["limits"].max_tool_output_chars,
                    )
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": tool_name,
                        "content": tool_message_content,
                    }
                )
                if stop_reason == "time_budget_exhausted":
                    break
            notepad_entries = _checkpointed_notepad()
        finally:
            if notepad_scope is not None:
                notepad_scope.__exit__(None, None, None)

        research_tool_names = set(tool_map) - {"notepad"}
        circuit_tools = {
            str(event.get("tool") or "")
            for event in events
            if event.get("kind") == "tool_unavailable"
            and event.get("circuit_open") is True
        }
        if (
            stop_reason is None
            and research_tool_names
            and research_tool_names.issubset(circuit_tools)
        ):
            stop_reason = "tool_services_unavailable"

        return {
            "messages": [*state["messages"], *new_messages],
            "notepad_entries": notepad_entries,
            "tool_calls_used": local_tool_calls,
            "total_tool_calls_used": total_tool_calls,
            "retries_used": retries_used,
            "source_candidate_count": source_candidate_count,
            "source_open_count": source_open_count,
            "duplicate_source_count": duplicate_source_count,
            "acquisition_call_count": acquisition_call_count,
            "execution_events": events,
            "pending_tool_calls": [],
            "observed_evidence": _deduplicate_evidence(collected),
            "strategy_attempts": strategy_attempts,
            "stop_reason": stop_reason,
            "pending_stop_reason": None,
        }

    async def use_tools(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_invocation(state, config)
        with _node_trace("use_tools", state) as observation:
            update = await _execute_pending_tools(state)
            observation.add_output(
                {
                    "tool_calls_used": update["tool_calls_used"],
                    "evidence_count": len(update["observed_evidence"]),
                }
            )
            return update

    async def _run_child(
        state: ResearchAgentState,
        candidate: ForkCandidate,
        *,
        assignment_id: str,
        thread_budget: int,
        tool_budget: int,
        token_budget: int,
        retry_budget: int,
    ) -> tuple[str, str, ResearchResult, list[dict[str, Any]]]:
        child_task = _child_task(
            state["task"],
            candidate,
            state["research_requirements"],
            assignment_id=assignment_id,
            parent_assignment_id=_own_assignment_id(state),
        )
        child_identity = _child_identity(state["identity"], candidate)
        if coordination_board is not None:
            try:
                coordination_board.update_assignment_node(
                    state["identity"].root_thread_id,
                    assignment_id,
                    owner_thread_id=child_identity.thread_id,
                    status="researching",
                    lease_seconds=_BLACKBOARD_LEASE_SECONDS,
                )
            except Exception:
                pass
        try:
            child_state = await _run_research_agent_state(
                child_task,
                _fork_policy_instance(policy),
                _fork_tool_instances(tool_list),
                identity=child_identity,
                limits=state["limits"],
                checkpointer=descendant_checkpointer,
                tool_artifact_store=tool_artifact_store,
                coordination_board=coordination_board,
                homogeneous_fork_config=fork_settings,
                agent_scheduler=live_scheduler,
                deadline_at=state["deadline_at"],
                subtree_thread_budget=thread_budget,
                subtree_tool_budget=tool_budget,
                subtree_token_budget=token_budget,
                subtree_retry_budget=retry_budget,
                lineage_objectives=[
                    *state["lineage_objectives"],
                    candidate.objective,
                ],
            )
            result = child_state["result"]
            if not isinstance(result, ResearchResult):
                raise TypeError("child graph completed without a ResearchResult")
            events = child_state["execution_events"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = ResearchResult(
                task_id=child_task.task_id,
                status=ResearchStatus.FAILED,
                summary="",
                unresolved=(f"child_execution_error: {exc}",),
                stop_reason="child_execution_error",
            )
            events = [
                _event(
                    "agent_failed",
                    child_identity,
                    error=str(exc),
                )
            ]
        result = replace(
            result,
            unresolved=_scoped_unresolved(result.unresolved, candidate),
        )
        if coordination_board is not None:
            assignment_status = (
                "completed"
                if result.status == ResearchStatus.COMPLETED
                else "failed"
                if result.status == ResearchStatus.FAILED
                else "blocked"
            )
            try:
                coordination_board.update_assignment_node(
                    state["identity"].root_thread_id,
                    assignment_id,
                    owner_thread_id=child_identity.thread_id,
                    status=assignment_status,
                )
            except Exception:
                pass
            if fork_settings.budget_leases_enabled:
                try:
                    coordination_board.release_budget_lease(
                        state["identity"].root_thread_id,
                        thread_id=child_identity.thread_id,
                        used_tokens=result.estimated_tokens_used,
                    )
                except Exception:
                    pass
        return candidate_fingerprint(candidate), child_identity.thread_id, result, events

    async def fork_children(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        nonlocal live_scheduler
        _validate_invocation(state, config)
        if live_scheduler is None:
            live_scheduler = FairAgentScheduler(
                run_id=state["identity"].root_thread_id,
                max_concurrent_agents=state["limits"].effective_max_concurrent_agents,
                max_total_agents=state["limits"].effective_max_total_agents,
                board=coordination_board,
            )
            await live_scheduler.activate_root(state["identity"].thread_id)
        candidates: list[ForkCandidate] = []
        for call in state["pending_fork_calls"]:
            candidates.extend(parse_fork_candidates(call["function"].get("arguments", "{}")))

        if coordination_board is not None:
            remaining_thread_slots = max(
                0,
                state["limits"].effective_max_total_agents
                - coordination_board.metrics(state["identity"].root_thread_id).get(
                    "assignment_count", 0
                ),
            )
        else:
            remaining_thread_slots = max(
                0,
                min(
                    state["subtree_thread_budget"] - state["total_threads_used"],
                    live_scheduler.remaining_total_capacity(),
                ),
            )
        remaining_children = max(
            0,
            min(
                _remaining_child_slots(state),
                remaining_thread_slots,
            ),
        )
        terminal_rejected_fingerprints: list[str] = []
        if (
            state["identity"].depth >= state["limits"].max_fork_depth
            or remaining_children <= 0
        ):
            terminal_rejected_fingerprints.extend(
                candidate_fingerprint(candidate) for candidate in candidates
            )
        accepted, rejected = evaluate_fork_candidates(
            candidates,
            parent_task=state["task"],
            identity=state["identity"],
            max_fork_depth=state["limits"].max_fork_depth,
            max_children=remaining_children,
            parent_requirement_ids=(item.requirement_id for item in state["research_requirements"]),
            completed_fingerprints=state["completed_fork_fingerprints"],
            ancestor_objectives=state["lineage_objectives"],
        )
        if not candidates:
            rejected.append("fork request contained no valid candidates")

        accepted, _, token_budgets, resource_rejected = _fork_resource_allocations(
            state,
            accepted,
            fork_settings=fork_settings,
            check_token_budget=not fork_settings.budget_leases_enabled,
        )
        for candidate in resource_rejected:
            rejected.append(
                f"{candidate.objective}: resource preflight failed; the allocated "
                "time/token/tool budget cannot complete control -> search/read -> "
                "memo, so retain this task locally"
            )
            terminal_rejected_fingerprints.append(candidate_fingerprint(candidate))

        if fork_settings.budget_leases_enabled and coordination_board is not None:
            funded_candidates: list[ForkCandidate] = []
            funded_token_budgets: list[int] = []
            minimum_tokens = _minimum_fork_token_budget(fork_settings)
            for candidate in accepted:
                child_identity = _child_identity(state["identity"], candidate)
                try:
                    granted = coordination_board.grant_budget_lease(
                        state["identity"].root_thread_id,
                        thread_id=child_identity.thread_id,
                        parent_thread_id=state["identity"].thread_id,
                        requested_tokens=fork_settings.initial_child_lease_tokens,
                        max_tokens=fork_settings.max_child_lease_tokens,
                    )
                except Exception as exc:
                    rejected.append(
                        f"{candidate.objective}: child budget lease unavailable "
                        f"({type(exc).__name__}); retain this task locally"
                    )
                    terminal_rejected_fingerprints.append(
                        candidate_fingerprint(candidate)
                    )
                    continue
                if granted < minimum_tokens:
                    rejected.append(
                        f"{candidate.objective}: resource preflight failed; child "
                        f"token lease {granted} is below the {minimum_tokens} tokens "
                        "needed for control -> search/read -> memo, so retain this "
                        "task locally"
                    )
                    terminal_rejected_fingerprints.append(
                        candidate_fingerprint(candidate)
                    )
                    try:
                        coordination_board.release_budget_lease(
                            state["identity"].root_thread_id,
                            thread_id=child_identity.thread_id,
                            used_tokens=0,
                        )
                    except Exception:
                        pass
                    continue
                funded_candidates.append(candidate)
                funded_token_budgets.append(granted)
            accepted = funded_candidates
            token_budgets = funded_token_budgets

        assignment_ids = [
            _candidate_assignment_id(_own_assignment_id(state), candidate)
            for candidate in accepted
        ]
        if coordination_board is not None and accepted:
            rows = []
            for candidate, assignment_id in zip(accepted, assignment_ids):
                child_identity = _child_identity(state["identity"], candidate)
                rows.append({
                    "assignment_id": assignment_id,
                    "parent_assignment_id": _own_assignment_id(state),
                    "owner_thread_id": child_identity.thread_id,
                    "parent_thread_id": state["identity"].thread_id,
                    "requirement_ids": candidate.requirement_ids,
                    "objective": candidate.objective,
                    "scope_signature": _candidate_scope_signature(candidate),
                    "reasons": tuple(reason.value for reason in candidate.reasons),
                    "status": "queued",
                })
            outcomes = coordination_board.register_assignment_nodes(
                state["identity"].root_thread_id,
                rows,
                actor_thread_id=state["identity"].thread_id,
                lease_seconds=_BLACKBOARD_LEASE_SECONDS,
                max_total_assignments=state["limits"].effective_max_total_agents,
                max_children_per_parent=state["limits"].effective_max_children_per_agent,
                max_depth=state["limits"].max_fork_depth,
            )
            scoped_candidates: list[ForkCandidate] = []
            scoped_assignment_ids: list[str] = []
            scoped_token_budgets: list[int] = []
            for candidate, assignment_id, token_budget in zip(
                accepted,
                assignment_ids,
                token_budgets,
            ):
                claim = outcomes[assignment_id]
                if not claim.acquired:
                    rejected.append(
                        f"{candidate.objective}: assignment rejected ({claim.reason})"
                    )
                    if claim.reason in {
                        "global_thread_limit_reached",
                        "parent_child_limit_reached",
                        "fork_depth_limit_reached",
                        "duplicate_sibling_scope",
                    }:
                        terminal_rejected_fingerprints.append(
                            candidate_fingerprint(candidate)
                        )
                    if fork_settings.budget_leases_enabled:
                        child_identity = _child_identity(state["identity"], candidate)
                        try:
                            coordination_board.release_budget_lease(
                                state["identity"].root_thread_id,
                                thread_id=child_identity.thread_id,
                                used_tokens=0,
                            )
                        except Exception:
                            pass
                    continue
                scoped_candidates.append(candidate)
                scoped_assignment_ids.append(assignment_id)
                scoped_token_budgets.append(token_budget)
            accepted = scoped_candidates
            assignment_ids = scoped_assignment_ids
            token_budgets = scoped_token_budgets

        if not fork_settings.budget_leases_enabled:
            token_budgets = _split_budget(
                _delegable_token_budget(state),
                len(accepted),
            )

        thread_budgets = (
            [state["limits"].effective_max_total_agents] * len(accepted)
            if coordination_board is not None
            else _split_budget(
                remaining_thread_slots,
                len(accepted),
                minimum=1,
            )
        )
        tool_budgets = _split_budget(
            _delegable_tool_budget(state, len(accepted)),
            len(accepted),
        )
        retry_budgets = _split_budget(
            max(0, state["subtree_retry_budget"] - state["retries_used"]),
            len(accepted),
        )

        if coordination_board is not None:
            coordination_board.record_event(
                state["identity"].root_thread_id,
                "fork_candidates_evaluated",
                actor_thread_id=state["identity"].thread_id,
                payload={
                    "accepted": [candidate.objective for candidate in accepted],
                    "rejected": rejected,
                },
            )

        with _node_trace("fork_children", state) as observation:
            scheduling_rows = list(zip(
                accepted,
                assignment_ids,
                thread_budgets,
                tool_budgets,
                token_budgets,
                retry_budgets,
            ))
            scheduled: list[ScheduledAgent[tuple[str, str, ResearchResult, list[dict[str, Any]]]]] = []
            for candidate, assignment_id, thread_budget, tool_budget, token_budget, retry_budget in scheduling_rows:
                child_identity = _child_identity(state["identity"], candidate)
                scheduled.append(ScheduledAgent(
                    assignment_id=assignment_id,
                    thread_id=child_identity.thread_id,
                    parent_assignment_id=_own_assignment_id(state),
                    runner=lambda candidate=candidate, assignment_id=assignment_id,
                    thread_budget=thread_budget, tool_budget=tool_budget,
                    token_budget=token_budget, retry_budget=retry_budget: _run_child(
                        state,
                        candidate,
                        assignment_id=assignment_id,
                        thread_budget=thread_budget,
                        tool_budget=tool_budget,
                        token_budget=token_budget,
                        retry_budget=retry_budget,
                    ),
                    can_start=lambda token_budget=token_budget: (
                        time.time() < state["deadline_at"] and token_budget > 0
                    ),
                ))
            raw_completed = await live_scheduler.run_children(
                parent_thread_id=state["identity"].thread_id,
                parent_assignment_id=_own_assignment_id(state),
                children=scheduled,
            ) if scheduled else []
            completed: list[tuple[str, str, ResearchResult, list[dict[str, Any]]]] = []
            for (candidate, assignment_id, *_), outcome in zip(scheduling_rows, raw_completed):
                if isinstance(outcome, tuple):
                    completed.append(outcome)
                    continue
                child_identity = _child_identity(state["identity"], candidate)
                cancelled = isinstance(outcome, ScheduledAgentCancelled)
                reason = (
                    "queued assignment cancelled due to time/token safety boundary"
                    if cancelled
                    else f"queued child execution failed: {outcome}"
                )
                completed.append((
                    candidate_fingerprint(candidate),
                    child_identity.thread_id,
                    ResearchResult(
                        task_id=_child_task(state["task"], candidate).task_id,
                        status=ResearchStatus.FAILED,
                        summary="",
                        unresolved=_scoped_unresolved((reason,), candidate),
                        stop_reason=(
                            "queued_cancelled_due_to_budget"
                            if cancelled else "child_execution_error"
                        ),
                        termination_reason=(
                            TerminationReason.BUDGET_FORCED
                            if cancelled else TerminationReason.TOOL_FAILURE
                        ),
                    ),
                    [_event(
                        "queued_assignment_cancelled" if cancelled else "agent_failed",
                        child_identity,
                        assignment_id=assignment_id,
                        error=reason,
                    )],
                ))
            fingerprints = [item[0] for item in completed]
            child_thread_ids = [item[1] for item in completed]
            new_results = [item[2] for item in completed]
            child_events = [event for item in completed for event in item[3]]
            all_results = [*state["child_results"], *new_results]
            evidence = _deduplicate_evidence(
                [
                    *state["observed_evidence"],
                    *(item for result in new_results for item in result.evidence),
                ]
            )
            response_payload = {
                "accepted": [
                    {
                        "task_id": result.task_id,
                        "thread_id": thread_id,
                        "assignment_id": assignment_id,
                        "status": result.status.value,
                    }
                    for assignment_id, thread_id, result in zip(
                        assignment_ids, child_thread_ids, new_results
                    )
                ],
                "rejected": rejected,
                "results": [
                    {
                        "task_id": result.task_id,
                        "status": result.status.value,
                        "summary": result.summary[:1000],
                        "research_memo": result.research_memo[:3500],
                        "unresolved": list(result.unresolved[:8]),
                        "coverage": [
                            {
                                "requirement_id": item.requirement_id,
                                "status": item.status.value,
                                "remaining_gap": item.remaining_gap,
                            }
                            for item in result.coverage
                        ],
                    }
                    for result in new_results
                ],
            }
            response_content = _serialize_tool_result(
                response_payload,
                state["limits"].max_tool_output_chars,
            )
            tool_messages = [
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": FORK_TOOL_NAME,
                    "content": response_content,
                }
                for call in state["pending_fork_calls"]
            ]
            observation.add_output(
                {
                    "accepted": len(new_results),
                    "rejected": len(rejected),
                    "successful": sum(result.status == ResearchStatus.COMPLETED for result in new_results),
                }
            )

        return {
            "messages": [*state["messages"], *tool_messages],
            "pending_fork_calls": [],
            "completed_fork_fingerprints": [
                *state["completed_fork_fingerprints"],
                *fingerprints,
                *terminal_rejected_fingerprints,
            ],
            "child_thread_ids": [*state["child_thread_ids"], *child_thread_ids],
            "child_results": all_results,
            "observed_evidence": evidence,
            "coverage": list(merge_child_coverage_evidence(state["coverage"], new_results)),
            "strategy_attempts": [
                *state.get("strategy_attempts", []),
                *(
                    StrategyAttempt(
                        requirement_id=attempt.requirement_id,
                        strategy=attempt.strategy,
                        query=attempt.query,
                        outcome=attempt.outcome,
                        evidence_ids=attempt.evidence_ids,
                        action_id=attempt.action_id,
                        artifact_ids=attempt.artifact_ids,
                    )
                    for result in new_results
                    for attempt in result.strategy_attempts
                ),
            ],
            "total_threads_used": (state["total_threads_used"] + sum(result.thread_count for result in new_results)),
            "total_tool_calls_used": (
                state["total_tool_calls_used"] + sum(result.tool_calls_used for result in new_results)
            ),
            "estimated_tokens_used": (
                state["estimated_tokens_used"] + sum(result.estimated_tokens_used for result in new_results)
            ),
            "retries_used": (state["retries_used"] + sum(result.retries_used for result in new_results)),
            "source_candidate_count": (
                int(state.get("source_candidate_count", 0))
                + sum(result.source_candidate_count for result in new_results)
            ),
            "source_open_count": (
                int(state.get("source_open_count", 0))
                + sum(result.source_open_count for result in new_results)
            ),
            "duplicate_source_count": (
                int(state.get("duplicate_source_count", 0))
                + sum(result.duplicate_source_count for result in new_results)
            ),
            "acquisition_call_count": (
                int(state.get("acquisition_call_count", 0))
                + sum(result.acquisition_call_count for result in new_results)
            ),
            "execution_events": [
                *state["execution_events"],
                *child_events,
                _event(
                    "fork_finished",
                    state["identity"],
                    accepted=len(new_results),
                    rejected=len(rejected),
                ),
            ],
        }

    async def finalize_output(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """Accept Root Markdown or validate a Child JSON memo, then fall back safely."""
        _validate_invocation(state, config)
        raw = state.get("draft_raw", "")
        draft = state.get("draft")
        output_status = OutputStatus.VALID
        error: str | None = None
        token_charge = 0
        if draft is None and state["identity"].depth == 0:
            draft = _root_markdown_draft(state, raw)
            if draft is None:
                error = "final Root Markdown is empty"
                retry_messages = [
                    *_bounded_finalization_messages(state),
                    {
                        "role": "user",
                        "content": (
                            "FINAL_OUTPUT_RETRY\nReturn only the complete Markdown "
                            "report now. Do not return JSON or a fenced code block."
                        ),
                    },
                ]
                remaining_tokens = (
                    state["subtree_token_budget"] - state["estimated_tokens_used"]
                )
                retry_input_tokens = max(
                    1,
                    sum(
                        len(str(item.get("content") or ""))
                        for item in retry_messages
                    ) // 4,
                )
                if (
                    _remaining_finalization_seconds(state) > 0
                    and remaining_tokens > retry_input_tokens
                ):
                    try:
                        retry_policy = _root_final_policy_instance(
                            policy,
                            fork_settings.root_final_max_tokens,
                        )
                        response = await asyncio.wait_for(
                            call_policy(retry_policy, retry_messages, []),
                            timeout=max(0.001, _remaining_finalization_seconds(state)),
                        )
                        token_charge = _estimate_tokens(retry_messages, response)
                        draft = _root_markdown_draft(
                            state,
                            str(response.get("content") or ""),
                        )
                        if draft is not None:
                            output_status = OutputStatus.REPAIRED
                    except asyncio.CancelledError:
                        raise
                    except Exception as retry_exc:
                        error = (
                            f"{error}; retry failed: "
                            f"{type(retry_exc).__name__}: {retry_exc}"
                        )
        if draft is None and state["identity"].depth > 0:
            try:
                if not raw.strip():
                    raise AssessmentValidationError("final response is empty")
                draft = _parse_final_draft(raw)
            except AssessmentValidationError as exc:
                error = str(exc)
                if raw.strip():
                    repair_messages = [
                        {
                            "role": "system",
                            "content": (
                                "Repair final Research Agent output structure only. "
                                "Return JSON and never call tools."
                            ),
                        },
                        {"role": "user", "content": repair_final_prompt(raw, error)},
                    ]
                else:
                    repair_messages = [
                        *_bounded_finalization_messages(state),
                        {
                            "role": "user",
                            "content": (
                                "FINAL_OUTPUT_RETRY\nThe prior final response contained no "
                                "answer text. Answer now without analysis or tool calls. Return "
                                "exactly one JSON object with status, summary, findings, "
                                "unresolved, research_memo, and report_markdown."
                            ),
                        },
                    ]
                remaining_tokens = state["subtree_token_budget"] - state["estimated_tokens_used"]
                repair_input_tokens = max(
                    1,
                    sum(len(str(item.get("content") or "")) for item in repair_messages) // 4,
                )
                if (
                    _remaining_finalization_seconds(state) > 0
                    and remaining_tokens > repair_input_tokens
                ):
                    try:
                        response = await asyncio.wait_for(
                            call_policy(policy, repair_messages, []),
                            timeout=max(0.001, _remaining_finalization_seconds(state)),
                        )
                        token_charge = _estimate_tokens(repair_messages, response)
                        draft = _parse_final_draft(str(response.get("content") or ""))
                        output_status = OutputStatus.REPAIRED
                    except asyncio.CancelledError:
                        raise
                    except Exception as repair_exc:
                        error = f"{error}; repair failed: {type(repair_exc).__name__}: {repair_exc}"
        if draft is None:
            evidence = _deduplicate_evidence(state["observed_evidence"])
            plain = raw.strip()
            draft = {
                "status": "partial" if plain or evidence else "failed",
                "summary": plain or ("Research stopped before a structured synthesis was available."),
                "findings": [item.finding for item in evidence[:12]],
                "unresolved": ["Final output used a deterministic fallback after one structure repair."],
                "research_memo": "",
                "report_markdown": "",
            }
            output_status = OutputStatus.FALLBACK

        with _node_trace("finalize_output", state) as observation:
            observation.add_output(
                {
                    "output_status": output_status.value,
                    "repair_error": error,
                }
            )
        return {
            "draft": draft,
            "output_status": output_status,
            "estimated_tokens_used": min(
                state["subtree_token_budget"],
                state["estimated_tokens_used"] + token_charge,
            ),
            "execution_events": [
                *state["execution_events"],
                _event(
                    "final_output_validated",
                    state["identity"],
                    output_status=output_status.value,
                    repair_error=error,
                ),
            ],
        }

    def synthesize(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_invocation(state, config)
        with _node_trace("synthesize", state) as observation:
            draft = state["draft"] or {}
            summary = str(draft.get("summary") or state["last_content"] or "").strip()
            findings = _coerce_strings(draft.get("findings"))
            unresolved = _coerce_strings(draft.get("unresolved"))
            evidence = _deduplicate_evidence(state["observed_evidence"])
            research_memo = str(draft.get("research_memo") or "").strip()
            report_markdown = str(draft.get("report_markdown") or "").strip()
            if state["identity"].depth > 0 and not research_memo:
                memo_lines = ["## Direction summary", "", summary or "No summary available."]
                if findings:
                    memo_lines.extend(("", "## Findings", ""))
                    for index, finding in enumerate(findings):
                        marker = (
                            f" [[EVIDENCE:{evidence[index].evidence_id}]]"
                            if index < len(evidence) else ""
                        )
                        memo_lines.append(f"- {finding}{marker}")
                if unresolved:
                    memo_lines.extend(("", "## Remaining gaps", ""))
                    memo_lines.extend(f"- {item}" for item in unresolved)
                research_memo = "\n".join(memo_lines).strip()
            if state["identity"].depth == 0 and not report_markdown:
                report_lines = ["# Research result", "", "## Summary", "", summary or "No summary available."]
                if findings:
                    report_lines.extend(("", "## Findings", ""))
                    for index, finding in enumerate(findings):
                        marker = (
                            f" [[EVIDENCE:{evidence[index].evidence_id}]]"
                            if index < len(evidence) else ""
                        )
                        report_lines.append(f"- {finding}{marker}")
                if unresolved:
                    report_lines.extend(("", "## Remaining gaps", ""))
                    report_lines.extend(f"- {item}" for item in unresolved)
                report_markdown = "\n".join(report_lines).strip()
            stop_reason = state["stop_reason"]
            child_results = tuple(state.get("child_results", []))
            incomplete_children = [
                child for child in child_results if child.status != ResearchStatus.COMPLETED
            ]
            for child in child_results:
                if child.status != ResearchStatus.COMPLETED:
                    unresolved.append(f"Child task {child.task_id} returned {child.status.value}.")
                unresolved.extend(child.unresolved)

            alerts_by_id = {}
            for event in state["execution_events"]:
                if event.get("kind") != "tool_unavailable":
                    continue
                alert = availability_alert_from_event(event)
                if alert.alert_id:
                    alerts_by_id[alert.alert_id] = alert
            for child in child_results:
                for alert in child.tool_alerts:
                    alerts_by_id[alert.alert_id] = alert
            tool_alerts = tuple(alerts_by_id.values())
            for alert in tool_alerts:
                unresolved.append(
                    f"External information alert: {alert.message} {alert.action_required}"
                )

            requested_status = str(draft.get("status") or "completed").lower()
            termination_reason = state.get("termination_reason")
            coverage = tuple(state.get("coverage", []))
            all_required_supported = bool(coverage) and all(
                item.status == RequirementStatus.SUPPORTED for item in coverage
            )
            usable = bool(state.get("draft_raw", "").strip() or findings or evidence)
            if stop_reason:
                unresolved.append(stop_reason)
            if not summary and not findings:
                status = ResearchStatus.FAILED
                unresolved.append("Agent returned no usable summary or findings.")
            elif state["task"].require_evidence and not evidence:
                status = ResearchStatus.PARTIAL
                unresolved.append("No source-locatable evidence was collected.")
            elif requested_status == ResearchStatus.FAILED.value:
                status = ResearchStatus.FAILED
            elif requested_status == ResearchStatus.PARTIAL.value:
                status = ResearchStatus.PARTIAL
            else:
                status = ResearchStatus.COMPLETED
            if termination_reason == TerminationReason.COVERAGE_COMPLETE:
                status = (
                    ResearchStatus.COMPLETED
                    if all_required_supported and (evidence or not state["task"].require_evidence)
                    else ResearchStatus.PARTIAL
                )
            elif termination_reason == TerminationReason.SATURATED:
                status = ResearchStatus.COMPLETED if all_required_supported else ResearchStatus.PARTIAL
            elif termination_reason in {
                TerminationReason.EVIDENCE_EXHAUSTED,
                TerminationReason.BUDGET_FORCED,
            }:
                status = ResearchStatus.PARTIAL if usable else ResearchStatus.FAILED
            elif termination_reason in {
                TerminationReason.TOOL_FAILURE,
                TerminationReason.USER_CANCELLED,
            }:
                status = ResearchStatus.PARTIAL if usable else ResearchStatus.FAILED

            result = ResearchResult(
                task_id=state["task"].task_id,
                status=status,
                summary=summary,
                findings=tuple(findings),
                evidence=tuple(evidence),
                unresolved=_aggregate_unresolved(unresolved),
                tool_alerts=tool_alerts,
                child_result_refs=tuple(
                    child.task_id for child in state.get("child_results", [])
                ),
                stop_reason=stop_reason,
                termination_reason=termination_reason,
                output_status=state.get("output_status", OutputStatus.FALLBACK),
                coverage=coverage,
                critical_gaps=tuple(state.get("critical_gaps", [])),
                next_actions=tuple(state.get("next_actions", [])),
                strategy_attempts=tuple(state.get("strategy_attempts", [])),
                iterations=state["iteration"],
                tool_calls_used=state["total_tool_calls_used"],
                thread_count=state["total_threads_used"],
                estimated_tokens_used=state["estimated_tokens_used"],
                retries_used=state["retries_used"],
                source_candidate_count=int(state.get("source_candidate_count", 0)),
                source_open_count=int(state.get("source_open_count", 0)),
                duplicate_source_count=int(state.get("duplicate_source_count", 0)),
                acquisition_call_count=int(state.get("acquisition_call_count", 0)),
                research_memo=research_memo,
                report_markdown=report_markdown,
            )
            if coordination_board is not None:
                try:
                    coordination_board.update_assignment_node(
                        state["identity"].root_thread_id,
                        _own_assignment_id(state),
                        owner_thread_id=state["identity"].thread_id,
                        status=(
                            "completed"
                            if result.status == ResearchStatus.COMPLETED
                            else "failed"
                            if result.status == ResearchStatus.FAILED
                            else "blocked"
                        ),
                    )
                    coordination_board.update_agent(
                        state["identity"].root_thread_id,
                        thread_id=state["identity"].thread_id,
                        parent_thread_id=state["identity"].parent_thread_id or "",
                        depth=state["identity"].depth,
                        requirement_ids=_owned_requirement_ids(state),
                        assignment_id=_own_assignment_id(state),
                        parent_assignment_id=_parent_assignment_id(state),
                        status=result.status.value,
                        current_action="finished",
                    )
                except Exception:
                    pass
            observation.add_output(
                {
                    "status": result.status.value,
                    "evidence_count": len(result.evidence),
                    "iterations": result.iterations,
                }
            )
            return {
                "result": result,
                "execution_events": [
                    *state["execution_events"],
                    _event(
                        "agent_finished",
                        state["identity"],
                        status=result.status.value,
                        termination_reason=(
                            result.termination_reason.value if result.termination_reason is not None else None
                        ),
                        output_status=result.output_status.value,
                    ),
                ],
            }

    def route_after_think(state: ResearchAgentState) -> str:
        if allow_fork_tool and state.get("pending_fork_calls", []):
            return "fork_children"
        if state["pending_tool_calls"]:
            return "use_tools"
        if state.get("assessment_decision") == ResearchDecision.STOP_RESEARCH and not state.get(
            "finalization_requested"
        ):
            return "finalize_output"
        return "assess_research_state"

    def route_after_assessment(state: ResearchAgentState) -> str:
        if state.get("assessment_decision") in {
            ResearchDecision.CONTINUE,
            ResearchDecision.REPLAN,
        }:
            return "think_and_plan"
        if state.get("finalization_requested"):
            return "think_and_plan"
        return "finalize_output"

    builder = StateGraph(state_schema)
    builder.add_node("prepare", prepare)
    if fork_settings.budget_leases_enabled:
        builder.add_node("refresh_budget_lease", refresh_budget_lease)
        builder.add_node(
            "refresh_budget_before_assessment",
            refresh_budget_lease,
        )
    builder.add_node("think_and_plan", think_and_plan)
    builder.add_node("use_tools", use_tools)
    if allow_fork_tool:
        builder.add_node("fork_children", fork_children)
    builder.add_node("assess_research_state", assess_research_state)
    builder.add_node("finalize_output", finalize_output)
    builder.add_node("synthesize", synthesize)
    builder.add_edge(START, "prepare")
    if fork_settings.budget_leases_enabled:
        builder.add_edge("prepare", "refresh_budget_lease")
        builder.add_edge("refresh_budget_lease", "think_and_plan")
    else:
        builder.add_edge("prepare", "think_and_plan")
    think_routes = {
        "use_tools": "use_tools",
        "assess_research_state": (
            "refresh_budget_before_assessment"
            if fork_settings.budget_leases_enabled else "assess_research_state"
        ),
        "finalize_output": "finalize_output",
    }
    if allow_fork_tool:
        think_routes["fork_children"] = "fork_children"
    builder.add_conditional_edges(
        "think_and_plan",
        route_after_think,
        think_routes,
    )
    if fork_settings.budget_leases_enabled:
        builder.add_edge("use_tools", "refresh_budget_before_assessment")
        builder.add_edge(
            "refresh_budget_before_assessment",
            "assess_research_state",
        )
    else:
        builder.add_edge("use_tools", "assess_research_state")
    if allow_fork_tool:
        builder.add_edge(
            "fork_children",
            (
                "refresh_budget_before_assessment"
                if fork_settings.budget_leases_enabled else "assess_research_state"
            ),
        )
    builder.add_conditional_edges(
        "assess_research_state",
        route_after_assessment,
        {
            "think_and_plan": (
                "refresh_budget_lease"
                if fork_settings.budget_leases_enabled else "think_and_plan"
            ),
            "finalize_output": "finalize_output",
        },
    )
    builder.add_edge("finalize_output", "synthesize")
    builder.add_edge("synthesize", END)
    return builder.compile(checkpointer=effective_checkpointer)


async def _run_research_agent_state(
    task: ResearchTask,
    policy: Any,
    tools: Iterable[Any] = (),
    *,
    identity: ExecutionIdentity,
    limits: AgentLimits | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    deadline_at: float | None = None,
    subtree_thread_budget: int | None = None,
    subtree_tool_budget: int | None = None,
    subtree_token_budget: int | None = None,
    subtree_retry_budget: int | None = None,
    lineage_objectives: list[str] | None = None,
    tool_artifact_store: Any | None = None,
    coordination_board: ResearchBlackboard | None = None,
    homogeneous_fork_config: HomogeneousForkConfig | None = None,
    agent_scheduler: FairAgentScheduler | None = None,
    initial_evidence: Iterable[EvidenceItem] = (),
) -> ResearchAgentState:
    """Start or resume one deterministic Agent thread and return its final state."""
    effective_limits = limits or AgentLimits()
    graph = build_research_agent_graph(
        policy,
        tools,
        checkpointer=checkpointer,
        child_checkpointer=checkpointer,
        tool_artifact_store=tool_artifact_store,
        coordination_board=coordination_board,
        homogeneous_fork_config=homogeneous_fork_config,
        agent_scheduler=agent_scheduler,
    )
    config = {"configurable": {"thread_id": identity.thread_id}}
    snapshot = await graph.aget_state(config)
    if snapshot.values:
        saved_task = snapshot.values.get("task")
        saved_identity = snapshot.values.get("identity")
        if saved_task != task or saved_identity != identity:
            raise ValueError("checkpoint thread belongs to a different task or identity")
        saved_result = snapshot.values.get("result")
        if isinstance(saved_result, ResearchResult):
            return ResearchAgentState(**snapshot.values)
        final_state = await graph.ainvoke(None, config=config)
    else:
        final_state = await graph.ainvoke(
            create_research_agent_state(
                task,
                identity,
                effective_limits,
                deadline_at=deadline_at,
                subtree_thread_budget=subtree_thread_budget,
                subtree_tool_budget=subtree_tool_budget,
                subtree_token_budget=subtree_token_budget,
                subtree_retry_budget=subtree_retry_budget,
                lineage_objectives=lineage_objectives,
                initial_evidence=initial_evidence,
            ),
            config=config,
        )
    return ResearchAgentState(**final_state)


async def run_research_agent(
    task: ResearchTask,
    policy: Any,
    tools: Iterable[Any] = (),
    *,
    identity: ExecutionIdentity,
    limits: AgentLimits | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    tool_artifact_store: Any | None = None,
    coordination_board: ResearchBlackboard | None = None,
    homogeneous_fork_config: HomogeneousForkConfig | None = None,
    initial_evidence: Iterable[EvidenceItem] = (),
) -> ResearchResult:
    """Run one scoped task through the shared homogeneous AgentGraph."""
    final_state = await _run_research_agent_state(
        task,
        policy,
        tools,
        identity=identity,
        limits=limits,
        checkpointer=checkpointer,
        tool_artifact_store=tool_artifact_store,
        coordination_board=coordination_board,
        homogeneous_fork_config=homogeneous_fork_config,
        initial_evidence=initial_evidence,
    )
    result = final_state.get("result")
    if not isinstance(result, ResearchResult):
        raise TypeError("Research AgentGraph completed without a ResearchResult")
    return result

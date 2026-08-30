"""The checkpointed LangGraph implementation shared by every Agent level."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ..tools.file_reader import FileReaderError
from ..tools.notepad import NotepadTool
from ..utils.tracing import trace_block, trace_context
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
from .research_sufficiency import (
    active_next_actions,
    AssessmentValidationError,
    ResearchAssessment,
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
    unattempted_actions,
)


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
    execution_events: list[dict[str, Any]]
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


_FINALIZATION_OUTPUT_TOKEN_RESERVE = 1024
_ASSESSMENT_OUTPUT_TOKEN_RESERVE = 2048
_ASSESSMENT_INPUT_TOKEN_RESERVE = 10000
_FINALIZATION_TIME_RESERVE_FRACTION = 0.1


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


def _remaining_research_seconds(state: ResearchAgentState) -> float:
    """Leave each recursive level time to synthesize and return upstream."""
    per_level_reserve = (
        state["limits"].max_elapsed_seconds
        * _FINALIZATION_TIME_RESERVE_FRACTION
    )
    reserve = per_level_reserve * (state["identity"].depth + 1)
    return _remaining_seconds(state) - reserve


def _delegable_token_budget(state: ResearchAgentState) -> int:
    """Retain enough subtree tokens for parent assessment and final synthesis."""
    remaining = max(
        0,
        state["subtree_token_budget"] - state["estimated_tokens_used"],
    )
    desired_reserve = (
        _ASSESSMENT_INPUT_TOKEN_RESERVE
        + _ASSESSMENT_OUTPUT_TOKEN_RESERVE
        + _FINALIZATION_OUTPUT_TOKEN_RESERVE
    )
    parent_reserve = min(desired_reserve, remaining // 2)
    return remaining - parent_reserve


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
        raise ValueError(
            "LangGraph configurable.thread_id must match identity.thread_id"
        )


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
        if identity.depth < limits.max_fork_depth and limits.max_children > 0
        else "The fork depth or child budget is closed; continue locally and do not fork."
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

When the task is complete, stop calling tools and return one JSON object:
{{
  "status": "completed" | "partial" | "failed",
  "summary": "concise synthesis",
  "findings": ["atomic finding"],
  "unresolved": ["remaining uncertainty"]
}}
After tool or child results, the same policy performs a checkpointed structured
research-state assessment. Continue and Replan instructions keep tools available
and identify requirement-scoped gaps. Stop Research alone enters final synthesis.
Do not wrap the final JSON in commentary. There is no separate Planner,
Manager, or Summarizer Agent.
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
        and (
            action.query.strip().lower() in argument_text
            or argument_text in action.query.strip().lower()
        )
    ]
    if len(exact) == 1:
        return exact[0]
    # The control message instructs the next loop to work only on its pending
    # actions. A single pending action can therefore be attributed to a real
    # research-tool call even when the policy rewrites the concrete query.
    if len(candidates) == 1 and argument_text:
        return candidates[0]
    return None


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
    return payload


def _try_parse_final_draft(content: str) -> dict[str, Any] | None:
    try:
        return _parse_final_draft(content)
    except AssessmentValidationError:
        return None


def _bounded_finalization_messages(
    state: ResearchAgentState,
) -> list[dict[str, Any]]:
    """Build a compact same-policy snapshot instead of replaying long history."""
    evidence = _deduplicate_evidence(state["observed_evidence"])
    if len(evidence) > 32:
        evidence = [*evidence[:16], *evidence[-16:]]
    payload = {
        "objective": state["task"].objective,
        "expected_output": state["task"].expected_output,
        "constraints": list(state["task"].constraints),
        "termination_reason": (
            state["termination_reason"].value
            if state.get("termination_reason") is not None
            else hard_termination_reason(state.get("stop_reason")).value
            if hard_termination_reason(state.get("stop_reason")) is not None
            else None
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
                "limitations": item.limitations[:300],
            }
            for item in evidence
        ],
        "child_results": [
            {
                "task_id": item.task_id,
                "status": item.status.value,
                "summary": item.summary[:1000],
                "unresolved": list(item.unresolved[:8]),
            }
            for item in state["child_results"]
        ],
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
    return [
        *identity_messages,
        {
            "role": "user",
            "content": (
                "FINAL_SYNTHESIS_SNAPSHOT\nReturn one JSON object with status "
                "(completed|partial|failed), summary, findings (array of strings), "
                "and unresolved (array of strings). Preserve uncertainty and do not "
                "invent evidence. The runtime will independently enforce research "
                "status and termination reason.\n\nSTATE:\n"
                + json.dumps(payload, ensure_ascii=False, default=str)
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
) -> EvidenceItem | None:
    clean_ref = str(source_ref or "").strip()
    clean_excerpt = str(excerpt or "").strip()
    clean_finding = str(finding or clean_excerpt or title or "").strip()
    if not clean_ref or not clean_finding:
        return None
    return EvidenceItem(
        evidence_id=_stable_evidence_id(clean_ref, clean_excerpt or clean_finding),
        finding=clean_finding,
        source_type=source_type,
        title=str(title or clean_ref).strip(),
        source_ref=clean_ref,
        locator=str(locator or clean_ref).strip(),
        excerpt=clean_excerpt[:4000],
        excerpt_type=excerpt_type,
        limitations=limitations,
    )


def _extract_evidence(tool_name: str, args: dict[str, Any], result: Any) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    if tool_name == "web_search" and isinstance(result, dict):
        for item in result.get("results", []):
            if not isinstance(item, dict):
                continue
            found = _evidence_item(
                finding=item.get("snippet") or item.get("title"),
                source_type="web",
                title=item.get("title"),
                source_ref=item.get("url"),
                locator=item.get("url"),
                excerpt=item.get("snippet"),
                limitations="Search-result snippet; open the source for stronger verification.",
            )
            if found:
                evidence.append(found)
    elif tool_name == "arxiv_reader" and isinstance(result, dict):
        for item in result.get("papers", []):
            if not isinstance(item, dict):
                continue
            paper_id = item.get("id") or item.get("paper_id") or ""
            source_ref = item.get("pdf_url") or item.get("url") or (
                f"arxiv:{paper_id}" if paper_id else ""
            )
            found = _evidence_item(
                finding=item.get("summary") or item.get("title"),
                source_type="paper",
                title=item.get("title"),
                source_ref=source_ref,
                locator=paper_id or source_ref,
                excerpt=item.get("summary"),
                limitations="Paper metadata or abstract; verify the full text when needed.",
            )
            if found:
                evidence.append(found)
    elif tool_name == "browser":
        found = _evidence_item(
            finding=str(result)[:1000],
            source_type="web",
            title=args.get("url"),
            source_ref=args.get("url"),
            locator=args.get("url"),
            excerpt=str(result)[:4000],
            limitations="Extracted webpage text; exact section locator may be unavailable.",
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


def _fork_policy_instance(policy: Any) -> Any:
    fork = getattr(policy, "fork", None)
    if callable(fork):
        return fork()
    try:
        return copy.deepcopy(policy)
    except Exception as exc:
        raise RuntimeError("policy cannot be isolated for a child Research Agent") from exc


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
            raise RuntimeError(
                f"tool {name!r} cannot be isolated for a child Research Agent"
            ) from exc
    return isolated


def _child_task(
    parent: ResearchTask,
    candidate: ForkCandidate,
    parent_requirements: Iterable[ResearchRequirement] = (),
) -> ResearchTask:
    fingerprint = candidate_fingerprint(candidate)
    # Root Brief planning fields describe the root deliverable. Inheriting them
    # made every homogeneous child assess itself against the entire root Brief,
    # so parallel forks repeated the whole research problem recursively.
    planning_keys = {"research_requirements", "directions", "research_gaps"}
    child_context = {
        key: value
        for key, value in parent.context.items()
        if key not in planning_keys
    }
    child_context.update(
        {
            key: value
            for key, value in candidate.context.items()
            if key not in planning_keys
        }
    )
    requirements_by_id = {
        item.requirement_id: item for item in parent_requirements
    }
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
            }
            for item in selected_requirements
        ]
        child_context["parent_requirement_ids"] = [
            item.requirement_id for item in selected_requirements
        ]
    else:
        child_context["research_requirements"] = [
            {
                "requirement_id": "R1",
                "description": candidate.objective,
                "required": True,
            }
        ]
    child_context["parent_objective"] = parent.objective
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
) -> ResearchAgentState:
    limits.validate()
    requirements = build_research_requirements(task)
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
        observed_evidence=[],
        deadline_at=(
            deadline_at
            if deadline_at is not None
            else time.time() + limits.max_elapsed_seconds
        ),
        subtree_thread_budget=(
            subtree_thread_budget
            if subtree_thread_budget is not None
            else limits.max_total_threads
        ),
        subtree_tool_budget=(
            subtree_tool_budget
            if subtree_tool_budget is not None
            else limits.max_total_tool_calls
        ),
        subtree_token_budget=(
            subtree_token_budget
            if subtree_token_budget is not None
            else limits.max_total_tokens
        ),
        subtree_retry_budget=(
            subtree_retry_budget
            if subtree_retry_budget is not None
            else limits.max_total_retries
        ),
        total_threads_used=1,
        total_tool_calls_used=0,
        estimated_tokens_used=0,
        retries_used=0,
        execution_events=[],
        lineage_objectives=(
            list(lineage_objectives)
            if lineage_objectives is not None
            else [task.objective]
        ),
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
    )


def _fallback_assessment(
    state: ResearchAgentState,
    *,
    attempts: tuple[StrategyAttempt, ...],
    hard_reason: TerminationReason | None = None,
    has_research_tools: bool,
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
    all_required_supported = bool(existing) and all(
        item.status == RequirementStatus.SUPPORTED for item in existing
    )
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
            item.requirement_id == gap.requirement_id
            and item.status != RequirementStatus.SUPPORTED
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
        if action.requirement_id in gap_ids
        and action.expected_value in {"high", "medium"}
    )
    if pending_actions:
        return ResearchAssessment(
            decision=ResearchDecision.CONTINUE,
            coverage=existing,
            critical_gaps=gaps,
            next_actions=pending_actions,
            output_status=OutputStatus.FALLBACK,
        )
    return ResearchAssessment(
        decision=ResearchDecision.STOP_RESEARCH,
        coverage=existing,
        critical_gaps=gaps,
        termination_reason=TerminationReason.TOOL_FAILURE,
        output_status=OutputStatus.FALLBACK,
    )


def build_research_agent_graph(
    policy: Any,
    tools: Iterable[Any] = (),
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    inherit_checkpointer: bool = False,
    child_checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Build the one graph shared by root, child, and grandchild Agents."""
    if inherit_checkpointer and checkpointer is not None:
        raise ValueError("cannot set checkpointer when inherit_checkpointer is true")
    tool_list = list(tools)
    tool_map = _build_tool_map(tool_list)
    effective_checkpointer = (
        None
        if inherit_checkpointer
        else checkpointer if checkpointer is not None else InMemorySaver()
    )
    descendant_checkpointer = (
        child_checkpointer
        if child_checkpointer is not None
        else effective_checkpointer
    )

    def prepare(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_invocation(state, config)
        with _node_trace("prepare", state) as observation:
            if state["messages"]:
                requirements = tuple(
                    state.get("research_requirements")
                    or build_research_requirements(state["task"])
                )
                migration: dict[str, Any] = {
                    "research_requirements": list(requirements),
                    "coverage": list(
                        state.get("coverage") or initial_coverage(requirements)
                    ),
                    "critical_gaps": list(state.get("critical_gaps", [])),
                    "next_actions": list(state.get("next_actions", [])),
                    "strategy_attempts": list(state.get("strategy_attempts", [])),
                    "last_assessed_strategy_attempt_count": state.get(
                        "last_assessed_strategy_attempt_count",
                        len(state.get("strategy_attempts", [])),
                    ),
                    "assessment_decision": state.get("assessment_decision"),
                    "assessment_output_status": state.get(
                        "assessment_output_status", OutputStatus.VALID
                    ),
                    "assessment_error": state.get("assessment_error"),
                    "termination_reason": state.get("termination_reason"),
                    "finalization_requested": bool(
                        state.get("finalization_requested", False)
                    ),
                    "output_status": state.get("output_status", OutputStatus.VALID),
                    "draft_raw": state.get("draft_raw", ""),
                    "last_assessed_evidence_count": int(
                        state.get("last_assessed_evidence_count", 0)
                    ),
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
            _remaining_seconds(state)
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
        remaining_tokens = (
            state["subtree_token_budget"] - state["estimated_tokens_used"]
        )
        policy_messages = (
            _bounded_finalization_messages(state)
            if finalization_requested
            else state["messages"]
        )
        estimated_prompt_tokens = max(
            1,
            sum(len(str(item.get("content") or "")) for item in policy_messages)
            // 4,
        )
        reserve = (
            0
            if finalization_requested
            else estimated_prompt_tokens + _FINALIZATION_OUTPUT_TOKEN_RESERVE
        )
        if (
            remaining_tokens <= reserve
            or estimated_prompt_tokens >= max(1, remaining_tokens - reserve)
        ):
            update = {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "stop_reason": "token_budget_exhausted",
                "termination_reason": TerminationReason.BUDGET_FORCED,
            }
            if finalization_requested:
                update["finalization_requested"] = False
            return update

        with _node_trace("think_and_plan", state) as observation:
            # Tool availability can be bound per async research run, so resolve
            # schemas here instead of freezing a deny-all scope at graph compile.
            schemas = [] if finalization_requested else [*_tool_schemas(tool_list), fork_tool_schema()]
            action_retries = 0
            while True:
                try:
                    response = await asyncio.wait_for(
                        call_policy(policy, policy_messages, schemas),
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
                        }
                        if finalization_requested:
                            update["finalization_requested"] = False
                        return update
                    action_retries += 1

            token_charge = min(
                _estimate_tokens(policy_messages, response),
                remaining_tokens,
            )
            token_exhausted = token_charge >= remaining_tokens

            content = str(response.get("content") or "")
            calls = [] if finalization_requested else _normalize_tool_calls(response.get("tool_calls"))
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if calls:
                assistant_message["tool_calls"] = calls
            if response.get("reasoning_content"):
                assistant_message["reasoning_content"] = response["reasoning_content"]

            update: dict[str, Any] = {
                "messages": [*state["messages"], assistant_message],
                "iteration": state["iteration"] + 1,
                "last_content": content,
                "pending_stop_reason": None,
                "finalization_requested": False,
                "estimated_tokens_used": state["estimated_tokens_used"] + token_charge,
                "retries_used": state["retries_used"] + action_retries,
            }
            if token_exhausted:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft_raw"] = content
                update["draft"] = _try_parse_final_draft(content)
                update["stop_reason"] = "token_budget_exhausted"
                update["termination_reason"] = TerminationReason.BUDGET_FORCED
                observation.add_output({"action": "stop", "reason": "token_budget"})
                return update
            if finalization_requested:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft_raw"] = content
                update["draft"] = _try_parse_final_draft(content)
                observation.add_output({"action": "finalize_output"})
                return update
            if not calls:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft_raw"] = content
                update["draft"] = _try_parse_final_draft(content)
                observation.add_output({"action": "assess_candidate"})
                return update

            fork_calls = [
                call
                for call in calls
                if call["function"].get("name") == FORK_TOOL_NAME
            ]
            if fork_calls:
                assistant_message["tool_calls"] = fork_calls
                update["messages"] = [*state["messages"], assistant_message]
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = fork_calls
                observation.add_output(
                    {"action": "fork_children", "fork_calls": len(fork_calls)}
                )
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
            observation.add_output(
                {"action": "use_tool", "tool_calls": len(update["pending_tool_calls"])}
            )
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
        new_evidence_ids = tuple(
            item.evidence_id for item in evidence[previous_count:]
        )
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
            if event.get("kind") == "tool_finished" and not event.get("ok", False)
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
        else:
            prompt = assessment_schema_prompt(
                task=state["task"],
                requirements=tuple(state["research_requirements"]),
                coverage=tuple(state["coverage"]),
                evidence=evidence,
                critical_gaps=tuple(state["critical_gaps"]),
                attempts=attempts,
                child_results=tuple(state["child_results"]),
                candidate_final=state.get("draft_raw", ""),
                recent_tool_failures=recent_tool_failures,
                recent_tool_outcomes=recent_tool_outcomes,
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
            remaining_tokens = (
                state["subtree_token_budget"] - state["estimated_tokens_used"]
            )
            assessment_input_tokens = max(
                1,
                sum(len(str(item.get("content") or "")) for item in assessment_messages)
                // 4,
            )
            finalization_reserve = max(
                _FINALIZATION_OUTPUT_TOKEN_RESERVE,
                sum(
                    len(str(item.get("content") or ""))
                    for item in state["messages"]
                )
                // 4
                + _FINALIZATION_OUTPUT_TOKEN_RESERVE,
            )
            if remaining_tokens <= (
                assessment_input_tokens
                + _ASSESSMENT_OUTPUT_TOKEN_RESERVE
                + finalization_reserve
            ):
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
                                "content": repair_assessment_prompt(raw_assessment, str(exc)),
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
                    )

        reconciled_attempts = (
            reconcile_strategy_attempt_outcomes(attempts, assessment.coverage)
            if assessment.output_status != OutputStatus.FALLBACK
            else attempts
        )
        scheduled_actions = merge_next_action_queue(
            state.get("next_actions", []),
            assessment,
            active_consumed=(
                len(attempts)
                > state.get("last_assessed_strategy_attempt_count", 0)
            ),
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
                    assessment.termination_reason.value
                    if assessment.termination_reason is not None
                    else None
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
        strategy_attempts = list(state.get("strategy_attempts", []))
        events = list(state["execution_events"])
        stop_reason = state["pending_stop_reason"]

        notepad = tool_map.get("notepad")
        notepad_scope = (
            notepad.bind_snapshot(state.get("notepad_entries", []))
            if isinstance(notepad, NotepadTool)
            else None
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

                tool = tool_map.get(tool_name)
                matched_action = _action_for_tool_call(
                    tool_name,
                    arguments,
                    active_next_actions(state.get("next_actions", [])),
                )
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
                                error = None
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

                        retry_available = min(
                            state["limits"].max_retries_per_action - action_retries,
                            state["subtree_retry_budget"] - retries_used,
                        )
                        call_available = min(
                            state["limits"].max_tool_calls - local_tool_calls,
                            state["subtree_tool_budget"] - total_tool_calls,
                        )
                        if (
                            error is None
                            or permanent_error
                            or stop_reason == "time_budget_exhausted"
                        ):
                            break
                        if retry_available <= 0 or call_available <= 0:
                            break
                        action_retries += 1
                        retries_used += 1

                    if error:
                        observation.set_error(error)
                    else:
                        observation.add_output({"ok": True, "retries": action_retries})

                extracted: list[EvidenceItem] = []
                if error is None:
                    extracted = _extract_evidence(tool_name, arguments, result)
                    known_ids = {item.evidence_id for item in collected}
                    new_ids = tuple(
                        item.evidence_id
                        for item in extracted
                        if item.evidence_id not in known_ids
                    )
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
                    )
                )
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": tool_name,
                        "content": _serialize_tool_result(
                            result,
                            state["limits"].max_tool_output_chars,
                        ),
                    }
                )
                if stop_reason == "time_budget_exhausted":
                    break
            notepad_entries = _checkpointed_notepad()
        finally:
            if notepad_scope is not None:
                notepad_scope.__exit__(None, None, None)

        return {
            "messages": [*state["messages"], *new_messages],
            "notepad_entries": notepad_entries,
            "tool_calls_used": local_tool_calls,
            "total_tool_calls_used": total_tool_calls,
            "retries_used": retries_used,
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
        thread_budget: int,
        tool_budget: int,
        token_budget: int,
        retry_budget: int,
    ) -> tuple[str, str, ResearchResult, list[dict[str, Any]]]:
        child_task = _child_task(
            state["task"],
            candidate,
            state["research_requirements"],
        )
        child_identity = _child_identity(state["identity"], candidate)
        try:
            child_state = await _run_research_agent_state(
                child_task,
                _fork_policy_instance(policy),
                _fork_tool_instances(tool_list),
                identity=child_identity,
                limits=state["limits"],
                checkpointer=descendant_checkpointer,
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
        return candidate_fingerprint(candidate), child_identity.thread_id, result, events

    async def fork_children(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_invocation(state, config)
        candidates: list[ForkCandidate] = []
        for call in state["pending_fork_calls"]:
            candidates.extend(
                parse_fork_candidates(call["function"].get("arguments", "{}"))
            )

        remaining_thread_slots = max(
            0,
            state["subtree_thread_budget"] - state["total_threads_used"],
        )
        remaining_children = max(
            0,
            min(
                state["limits"].max_children
                - len(set(state["completed_fork_fingerprints"])),
                remaining_thread_slots,
            ),
        )
        accepted, rejected = evaluate_fork_candidates(
            candidates,
            parent_task=state["task"],
            identity=state["identity"],
            max_fork_depth=state["limits"].max_fork_depth,
            max_children=remaining_children,
            parent_requirement_ids=(
                item.requirement_id for item in state["research_requirements"]
            ),
            completed_fingerprints=state["completed_fork_fingerprints"],
            ancestor_objectives=state["lineage_objectives"],
        )
        if not candidates:
            rejected.append("fork request contained no valid candidates")
        if _remaining_seconds(state) <= 0:
            rejected.extend(
                f"{candidate.objective}: time budget exhausted"
                for candidate in accepted
            )
            accepted = []

        thread_budgets = _split_budget(
            remaining_thread_slots,
            len(accepted),
            minimum=1,
        )
        tool_budgets = _split_budget(
            _delegable_tool_budget(state, len(accepted)),
            len(accepted),
        )
        token_budgets = _split_budget(
            _delegable_token_budget(state),
            len(accepted),
        )
        retry_budgets = _split_budget(
            max(0, state["subtree_retry_budget"] - state["retries_used"]),
            len(accepted),
        )

        with _node_trace("fork_children", state) as observation:
            completed = await asyncio.gather(
                *(
                    _run_child(
                        state,
                        candidate,
                        thread_budget=thread_budget,
                        tool_budget=tool_budget,
                        token_budget=token_budget,
                        retry_budget=retry_budget,
                    )
                    for candidate, thread_budget, tool_budget, token_budget, retry_budget
                    in zip(
                        accepted,
                        thread_budgets,
                        tool_budgets,
                        token_budgets,
                        retry_budgets,
                    )
                )
            )
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
                        "status": result.status.value,
                    }
                    for thread_id, result in zip(child_thread_ids, new_results)
                ],
                "rejected": rejected,
                "results": [asdict(result) for result in new_results],
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
                    "successful": sum(
                        result.status == ResearchStatus.COMPLETED
                        for result in new_results
                    ),
                }
            )

        return {
            "messages": [*state["messages"], *tool_messages],
            "pending_fork_calls": [],
            "completed_fork_fingerprints": [
                *state["completed_fork_fingerprints"],
                *fingerprints,
            ],
            "child_thread_ids": [*state["child_thread_ids"], *child_thread_ids],
            "child_results": all_results,
            "observed_evidence": evidence,
            "coverage": list(
                merge_child_coverage_evidence(state["coverage"], new_results)
            ),
            "strategy_attempts": [
                *state.get("strategy_attempts", []),
                *(
                    StrategyAttempt(
                        requirement_id=attempt.requirement_id,
                        strategy=attempt.strategy,
                        query=attempt.query,
                        outcome=attempt.outcome,
                        evidence_ids=attempt.evidence_ids,
                    )
                    for result in new_results
                    for attempt in result.strategy_attempts
                ),
            ],
            "total_threads_used": (
                state["total_threads_used"]
                + sum(result.thread_count for result in new_results)
            ),
            "total_tool_calls_used": (
                state["total_tool_calls_used"]
                + sum(result.tool_calls_used for result in new_results)
            ),
            "estimated_tokens_used": (
                state["estimated_tokens_used"]
                + sum(result.estimated_tokens_used for result in new_results)
            ),
            "retries_used": (
                state["retries_used"]
                + sum(result.retries_used for result in new_results)
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
        """Validate the final JSON, repair once without tools, then fall back safely."""
        _validate_invocation(state, config)
        raw = state.get("draft_raw", "")
        draft = state.get("draft")
        output_status = OutputStatus.VALID
        error: str | None = None
        token_charge = 0
        if draft is None:
            try:
                if not raw.strip():
                    raise AssessmentValidationError("final response is empty")
                draft = _parse_final_draft(raw)
            except AssessmentValidationError as exc:
                error = str(exc)
                if raw.strip() and _remaining_seconds(state) > 0 and (
                    state["subtree_token_budget"] - state["estimated_tokens_used"] > 0
                ):
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
                    try:
                        response = await asyncio.wait_for(
                            call_policy(policy, repair_messages, []),
                            timeout=max(0.001, _remaining_seconds(state)),
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
                    "summary": plain or (
                        "Research stopped before a structured synthesis was available."
                    ),
                    "findings": [item.finding for item in evidence[:12]],
                    "unresolved": [
                        "Final output used a deterministic fallback after one structure repair."
                    ],
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
            stop_reason = state["stop_reason"]
            incomplete_children = [
                child
                for child in state["child_results"]
                if child.status != ResearchStatus.COMPLETED
            ]
            for child in state["child_results"]:
                if child.status != ResearchStatus.COMPLETED:
                    unresolved.append(
                        f"Child task {child.task_id} returned {child.status.value}."
                    )
                unresolved.extend(child.unresolved)

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
                unresolved=tuple(dict.fromkeys(unresolved)),
                child_result_refs=tuple(
                    child.task_id for child in state["child_results"]
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
            )
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
                            result.termination_reason.value
                            if result.termination_reason is not None
                            else None
                        ),
                        output_status=result.output_status.value,
                    ),
                ],
            }

    def route_after_think(state: ResearchAgentState) -> str:
        if state["pending_fork_calls"]:
            return "fork_children"
        if state["pending_tool_calls"]:
            return "use_tools"
        if (
            state.get("assessment_decision") == ResearchDecision.STOP_RESEARCH
            and not state.get("finalization_requested")
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

    builder = StateGraph(ResearchAgentState)
    builder.add_node("prepare", prepare)
    builder.add_node("think_and_plan", think_and_plan)
    builder.add_node("use_tools", use_tools)
    builder.add_node("fork_children", fork_children)
    builder.add_node("assess_research_state", assess_research_state)
    builder.add_node("finalize_output", finalize_output)
    builder.add_node("synthesize", synthesize)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "think_and_plan")
    builder.add_conditional_edges(
        "think_and_plan",
        route_after_think,
        {
            "fork_children": "fork_children",
            "use_tools": "use_tools",
            "assess_research_state": "assess_research_state",
            "finalize_output": "finalize_output",
        },
    )
    builder.add_edge("use_tools", "assess_research_state")
    builder.add_edge("fork_children", "assess_research_state")
    builder.add_conditional_edges(
        "assess_research_state",
        route_after_assessment,
        {
            "think_and_plan": "think_and_plan",
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
) -> ResearchAgentState:
    """Start or resume one deterministic Agent thread and return its final state."""
    effective_limits = limits or AgentLimits()
    graph = build_research_agent_graph(
        policy,
        tools,
        checkpointer=checkpointer,
        child_checkpointer=checkpointer,
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
) -> ResearchResult:
    """Run one scoped task through the shared homogeneous AgentGraph."""
    final_state = await _run_research_agent_state(
        task,
        policy,
        tools,
        identity=identity,
        limits=limits,
        checkpointer=checkpointer,
    )
    result = final_state.get("result")
    if not isinstance(result, ResearchResult):
        raise TypeError("Research AgentGraph completed without a ResearchResult")
    return result

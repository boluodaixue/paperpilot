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
    EvidenceItem,
    ExecutionIdentity,
    ForkCandidate,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
)
from .policy import call_policy


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
    last_content: str
    stop_reason: str | None
    result: ResearchResult | None


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


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


def _parse_draft(content: str) -> dict[str, Any]:
    stripped = (content or "").strip()
    candidate = stripped
    fenced = _JSON_FENCE.search(stripped)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "partial" if stripped else "failed",
            "summary": stripped,
            "findings": [stripped] if stripped else [],
            "unresolved": ["Model returned an unstructured final response."],
        }
    if not isinstance(payload, dict):
        return {
            "status": "failed",
            "summary": "",
            "findings": [],
            "unresolved": ["Model final response was not a JSON object."],
        }
    return payload


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
) -> ResearchTask:
    fingerprint = candidate_fingerprint(candidate)
    return ResearchTask(
        task_id=f"child-{fingerprint[:12]}",
        objective=candidate.objective,
        context={
            **parent.context,
            **candidate.context,
            "parent_objective": parent.objective,
        },
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
        last_content="",
        stop_reason=None,
        result=None,
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
                observation.add_output({"resumed": True})
                return {}
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
        if state["stop_reason"]:
            return {"pending_tool_calls": [], "pending_fork_calls": []}
        if state["iteration"] >= state["limits"].max_iterations:
            return {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "stop_reason": "max_iterations_exhausted",
            }
        if _remaining_seconds(state) <= 0:
            return {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "stop_reason": "time_budget_exhausted",
            }
        remaining_tokens = (
            state["subtree_token_budget"] - state["estimated_tokens_used"]
        )
        estimated_prompt_tokens = max(
            1,
            sum(len(str(item.get("content") or "")) for item in state["messages"])
            // 4,
        )
        if remaining_tokens <= 0 or estimated_prompt_tokens >= remaining_tokens:
            return {
                "pending_tool_calls": [],
                "pending_fork_calls": [],
                "stop_reason": "token_budget_exhausted",
            }

        with _node_trace("think_and_plan", state) as observation:
            # Tool availability can be bound per async research run, so resolve
            # schemas here instead of freezing a deny-all scope at graph compile.
            schemas = [*_tool_schemas(tool_list), fork_tool_schema()]
            action_retries = 0
            while True:
                try:
                    response = await asyncio.wait_for(
                        call_policy(policy, state["messages"], schemas),
                        timeout=max(0.001, _remaining_seconds(state)),
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    observation.set_error("time budget exhausted")
                    return {
                        "pending_tool_calls": [],
                        "pending_fork_calls": [],
                        "stop_reason": "time_budget_exhausted",
                        "iteration": state["iteration"] + 1,
                        "retries_used": state["retries_used"] + action_retries,
                    }
                except Exception as exc:
                    retry_available = min(
                        state["limits"].max_retries_per_action - action_retries,
                        state["subtree_retry_budget"] - state["retries_used"],
                    )
                    if retry_available <= 0:
                        observation.set_error(str(exc))
                        return {
                            "pending_tool_calls": [],
                            "pending_fork_calls": [],
                            "stop_reason": f"policy_error: {exc}",
                            "iteration": state["iteration"] + 1,
                            "retries_used": state["retries_used"] + action_retries,
                        }
                    action_retries += 1

            token_charge = min(
                _estimate_tokens(state["messages"], response),
                remaining_tokens,
            )
            token_exhausted = token_charge >= remaining_tokens

            content = str(response.get("content") or "")
            calls = _normalize_tool_calls(response.get("tool_calls"))
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
                "estimated_tokens_used": state["estimated_tokens_used"] + token_charge,
                "retries_used": state["retries_used"] + action_retries,
            }
            if token_exhausted:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft"] = _parse_draft(content)
                update["stop_reason"] = "token_budget_exhausted"
                observation.add_output({"action": "stop", "reason": "token_budget"})
                return update
            if not calls:
                update["pending_tool_calls"] = []
                update["pending_fork_calls"] = []
                update["draft"] = _parse_draft(content)
                observation.add_output({"action": "synthesize"})
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

    async def _execute_pending_tools(
        state: ResearchAgentState,
    ) -> dict[str, Any]:
        new_messages: list[dict[str, Any]] = []
        collected = list(state["observed_evidence"])
        local_tool_calls = state["tool_calls_used"]
        total_tool_calls = state["total_tool_calls_used"]
        retries_used = state["retries_used"]
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
                        if _remaining_seconds(state) <= 0:
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
                                        timeout=max(0.001, _remaining_seconds(state)),
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

                if error is None:
                    collected.extend(_extract_evidence(tool_name, arguments, result))
                events.append(
                    _event(
                        "tool_finished",
                        state["identity"],
                        tool=tool_name,
                        ok=error is None,
                        retries=action_retries,
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
        child_task = _child_task(state["task"], candidate)
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
            max(0, state["subtree_tool_budget"] - state["total_tool_calls_used"]),
            len(accepted),
        )
        token_budgets = _split_budget(
            max(0, state["subtree_token_budget"] - state["estimated_tokens_used"]),
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
            for child in incomplete_children:
                unresolved.append(
                    f"Child task {child.task_id} returned {child.status.value}."
                )
                unresolved.extend(child.unresolved)

            requested_status = str(draft.get("status") or "completed").lower()
            if stop_reason:
                status = ResearchStatus.PARTIAL if summary or evidence else ResearchStatus.FAILED
                unresolved.append(stop_reason)
            elif not summary and not findings:
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
            if status == ResearchStatus.COMPLETED and incomplete_children:
                status = ResearchStatus.PARTIAL

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
                    ),
                ],
            }

    def route_after_think(state: ResearchAgentState) -> str:
        if state["pending_fork_calls"]:
            return "fork_children"
        if state["pending_tool_calls"]:
            return "use_tools"
        return "synthesize"

    builder = StateGraph(ResearchAgentState)
    builder.add_node("prepare", prepare)
    builder.add_node("think_and_plan", think_and_plan)
    builder.add_node("use_tools", use_tools)
    builder.add_node("fork_children", fork_children)
    builder.add_node("synthesize", synthesize)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "think_and_plan")
    builder.add_conditional_edges(
        "think_and_plan",
        route_after_think,
        {
            "fork_children": "fork_children",
            "use_tools": "use_tools",
            "synthesize": "synthesize",
        },
    )
    builder.add_edge("use_tools", "think_and_plan")
    builder.add_edge("fork_children", "think_and_plan")
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

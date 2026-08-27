"""The single LangGraph implementation used by every Research Agent level.

N1 deliberately contains no user-confirmation workflow, fork branch, durable
Markdown memory, RCS, or Red/Blue stage.  A root and a child differ only in
their task and :class:`ExecutionIdentity`; both execute this exact graph.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
from contextlib import contextmanager
from typing import Any, Iterable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ..utils.tracing import trace_block, trace_context
from .models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
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
    """Only fields read or written by the N1 homogeneous AgentGraph."""

    task: ResearchTask
    identity: ExecutionIdentity
    limits: AgentLimits
    messages: list[dict[str, Any]]
    iteration: int
    tool_calls_used: int
    pending_tool_calls: list[dict[str, Any]]
    pending_stop_reason: str | None
    observed_evidence: list[EvidenceItem]
    draft: dict[str, Any] | None
    last_content: str
    stop_reason: str | None
    result: ResearchResult | None


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


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


def _system_prompt(identity: ExecutionIdentity) -> str:
    return f"""You are a PaperPilot Research Agent at depth {identity.depth}.
Every Research Agent uses this same research loop. In N1 you must not fork.

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
Do not wrap the final JSON in commentary. Planning and summarization are both
your own responsibilities; there is no separate Planner or Summarizer.
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
    elif tool_name == "file_reader":
        found = _evidence_item(
            finding=str(result)[:1000],
            source_type="file",
            title=args.get("file_path"),
            source_ref=args.get("file_path"),
            locator=args.get("file_path"),
            excerpt=str(result)[:4000],
            excerpt_type="quote",
            limitations="File excerpt; preserve page or section details when available.",
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


def create_research_agent_state(
    task: ResearchTask,
    identity: ExecutionIdentity,
    limits: AgentLimits,
) -> ResearchAgentState:
    return ResearchAgentState(
        task=task,
        identity=identity,
        limits=limits,
        messages=[],
        iteration=0,
        tool_calls_used=0,
        pending_tool_calls=[],
        pending_stop_reason=None,
        observed_evidence=[],
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
) -> Any:
    """Build the one graph shared by root, child, and future grandchild Agents."""
    if inherit_checkpointer and checkpointer is not None:
        raise ValueError("cannot set checkpointer when inherit_checkpointer is true")
    tool_list = list(tools)
    tool_map = _build_tool_map(tool_list)
    schemas = _tool_schemas(tool_list)

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
                {"role": "system", "content": _system_prompt(state["identity"])},
                {"role": "user", "content": _task_prompt(state["task"])},
            ]
            observation.add_output({"resumed": False})
            return {"messages": messages}

    async def think_and_plan(
        state: ResearchAgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_invocation(state, config)
        if state["stop_reason"]:
            return {"pending_tool_calls": []}
        if state["iteration"] >= state["limits"].max_iterations:
            return {
                "pending_tool_calls": [],
                "stop_reason": "max_iterations_exhausted",
            }

        with _node_trace("think_and_plan", state) as observation:
            try:
                response = await call_policy(policy, state["messages"], schemas)
            except Exception as exc:
                observation.set_error(str(exc))
                return {
                    "pending_tool_calls": [],
                    "stop_reason": f"policy_error: {exc}",
                    "iteration": state["iteration"] + 1,
                }

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
            }
            if not calls:
                update["pending_tool_calls"] = []
                update["draft"] = _parse_draft(content)
                observation.add_output({"action": "synthesize"})
                return update

            remaining = state["limits"].max_tool_calls - state["tool_calls_used"]
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

        for call in state["pending_tool_calls"]:
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
            with trace_block(
                f"research_agent.tool.{tool_name or 'unknown'}",
                run_type="tool",
                inputs={"tool": tool_name, **_identity_metadata(state["identity"])},
                tags=["paperpilot", "research-agent", "tool"],
            ) as observation:
                if tool is None:
                    error = f"unknown tool: {tool_name}"
                    result = {"error": error}
                else:
                    try:
                        result = tool.execute(**arguments)
                        if inspect.isawaitable(result):
                            result = await result
                    except Exception as exc:
                        error = str(exc)
                        result = {"error": error}
                if error:
                    observation.set_error(error)
                else:
                    observation.add_output({"ok": True})

            if error is None:
                collected.extend(_extract_evidence(tool_name, arguments, result))
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

        return {
            "messages": [*state["messages"], *new_messages],
            "tool_calls_used": state["tool_calls_used"] + len(state["pending_tool_calls"]),
            "pending_tool_calls": [],
            "observed_evidence": _deduplicate_evidence(collected),
            "stop_reason": state["pending_stop_reason"],
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

            result = ResearchResult(
                task_id=state["task"].task_id,
                status=status,
                summary=summary,
                findings=tuple(findings),
                evidence=tuple(evidence),
                unresolved=tuple(dict.fromkeys(unresolved)),
                child_result_refs=(),
                stop_reason=stop_reason,
                iterations=state["iteration"],
                tool_calls_used=state["tool_calls_used"],
            )
            observation.add_output(
                {
                    "status": result.status.value,
                    "evidence_count": len(result.evidence),
                    "iterations": result.iterations,
                }
            )
            return {"result": result}

    def route_after_think(state: ResearchAgentState) -> str:
        return "use_tools" if state["pending_tool_calls"] else "synthesize"

    builder = StateGraph(ResearchAgentState)
    builder.add_node("prepare", prepare)
    builder.add_node("think_and_plan", think_and_plan)
    builder.add_node("use_tools", use_tools)
    builder.add_node("synthesize", synthesize)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "think_and_plan")
    builder.add_conditional_edges(
        "think_and_plan",
        route_after_think,
        {"use_tools": "use_tools", "synthesize": "synthesize"},
    )
    builder.add_edge("use_tools", "think_and_plan")
    builder.add_edge("synthesize", END)
    effective_checkpointer = (
        None
        if inherit_checkpointer
        else checkpointer if checkpointer is not None else InMemorySaver()
    )
    return builder.compile(checkpointer=effective_checkpointer)


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
    effective_limits = limits or AgentLimits()
    graph = build_research_agent_graph(
        policy,
        tools,
        checkpointer=checkpointer,
    )
    final_state = await graph.ainvoke(
        create_research_agent_state(task, identity, effective_limits),
        config={"configurable": {"thread_id": identity.thread_id}},
    )
    result = final_state.get("result")
    if not isinstance(result, ResearchResult):
        raise TypeError("Research AgentGraph completed without a ResearchResult")
    return result

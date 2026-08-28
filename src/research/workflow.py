"""Root workflow: align, confirm, run the homogeneous graph, and persist."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterable, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..utils.tracing import trace_block, trace_context
from .agent_graph import build_research_agent_graph, create_research_agent_state
from .memory import MarkdownMemoryStore
from .models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
    MemoryManifest,
    ReportReviewOutcome,
    ResearchBrief,
    ResearchResult,
    ResearchTask,
    ResearchWorkflowResult,
)
from .policy import call_policy
from .report_review import review_final_report
from .retrieval import MarkdownMemoryIndex, MemorySearchHit
from .vault import validate_memory_id


__all__ = [
    "ResearchWorkflowState",
    "build_research_workflow",
    "create_research_workflow_state",
    "resume_research_workflow",
]


class ResearchWorkflowState(TypedDict, total=False):
    """Fields used by the outer workflow and embedded Research AgentGraph."""

    question: str
    memory_id: str | None
    retrieved_memory: list[dict[str, Any]]
    brief: ResearchBrief | None
    alignment_messages: list[dict[str, Any]]
    revision_feedback: str | None
    confirmed: bool
    identity: ExecutionIdentity
    limits: AgentLimits
    task: ResearchTask
    messages: list[dict[str, Any]]
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
    report_markdown: str | None
    memory_manifest: MemoryManifest | None
    workflow_result: ResearchWorkflowResult | None
    report_review: ReportReviewOutcome | None


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _validate_root_state(
    state: ResearchWorkflowState,
    config: RunnableConfig,
) -> None:
    question = str(state.get("question") or "").strip()
    if not question:
        raise ValueError("question must be a non-empty string")
    identity = state["identity"]
    identity.validate()
    if identity.depth != 0:
        raise ValueError("the user-alignment workflow requires a root identity")
    state["limits"].validate()
    checkpoint_thread_id = config.get("configurable", {}).get("thread_id")
    if checkpoint_thread_id != identity.thread_id:
        raise ValueError(
            "LangGraph configurable.thread_id must match identity.thread_id"
        )


@contextmanager
def _workflow_trace(name: str, state: ResearchWorkflowState):
    identity = state["identity"]
    metadata = {
        "thread_id": identity.thread_id,
        "parent_thread_id": identity.parent_thread_id,
        "root_thread_id": identity.root_thread_id,
        "depth": identity.depth,
    }
    with trace_context(
        session_id=identity.root_thread_id,
        trace_name="paperpilot.research.workflow",
        tags=["paperpilot", "research-workflow", name],
        metadata=metadata,
    ):
        with trace_block(
            f"research_workflow.{name}",
            run_type="chain",
            inputs=metadata,
            tags=["paperpilot", "research-workflow", name],
        ) as observation:
            yield observation


def _alignment_system_prompt() -> str:
    return """You are the root PaperPilot Research Agent before research begins.
Align the task with the user. Do not call research tools and do not perform the
research yet. Return exactly one JSON object:
{
  "objective": "confirmed research objective",
  "scope": ["included scope"],
  "directions": ["research direction"],
  "constraints": ["constraint"],
  "expected_output": "expected final deliverable"
}
The brief must be concrete enough that the user can confirm or revise it.
"""


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _bounded_memory_hits(
    hits: Iterable[MemorySearchHit],
) -> list[dict[str, Any]]:
    return [
        {
            "path": hit.relative_path,
            "title": hit.title[:300],
            "summary": hit.summary[:1200],
            "wikilinks": [link[:300] for link in hit.wikilinks[:8]],
        }
        for hit in tuple(hits)[:5]
    ]


def _memory_alignment_message(
    memory_id: str,
    retrieved_memory: list[dict[str, Any]],
) -> dict[str, str]:
    if retrieved_memory:
        introduction = (
            "Use the following deterministic matches from the selected Memory to "
            "separate known information from remaining research gaps."
        )
    else:
        introduction = (
            "No relevant notes were found in the selected Memory. Do not claim that "
            "prior Memory information was used."
        )
    payload = {
        "memory_id": memory_id,
        "hits": retrieved_memory,
    }
    return {
        "role": "system",
        "content": (
            f"{introduction}\n"
            "The Memory ID and note paths below are fixed by PaperPilot; do not "
            "replace or invent them. Return known_information and research_gaps as "
            "arrays in the research brief JSON.\n"
            f"MEMORY_CONTEXT_JSON:\n{json.dumps(payload, ensure_ascii=False)}"
        ),
    }


def _parse_brief(
    content: str,
    *,
    question: str,
    revision: int,
    memory_id: str | None = None,
    retrieved_memory: Iterable[dict[str, Any]] = (),
) -> ResearchBrief:
    candidate = (content or "").strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("alignment policy must return a JSON research brief") from exc
    if not isinstance(payload, dict):
        raise ValueError("alignment policy must return a JSON object")

    objective = str(payload.get("objective") or "").strip()
    directions = _as_string_tuple(payload.get("directions"))
    expected_output = str(payload.get("expected_output") or "").strip()
    if not objective:
        raise ValueError("research brief objective cannot be empty")
    if not directions:
        raise ValueError("research brief must contain at least one direction")
    if not expected_output:
        raise ValueError("research brief expected_output cannot be empty")
    fixed_memory = tuple(retrieved_memory) if memory_id is not None else ()
    memory_paths = tuple(
        str(item["path"])
        for item in fixed_memory
        if str(item.get("path") or "").strip()
    )
    if memory_id is None:
        known_information: tuple[str, ...] = ()
        research_gaps: tuple[str, ...] = ()
    else:
        if not fixed_memory:
            known_information = ()
        elif "known_information" in payload:
            known_information = _as_string_tuple(payload.get("known_information"))
        else:
            known_information = tuple(
                str(item["summary"]).strip()
                for item in fixed_memory
                if str(item.get("summary") or "").strip()
            )
        research_gaps = (
            _as_string_tuple(payload.get("research_gaps"))
            if "research_gaps" in payload
            else directions
        )
    return ResearchBrief(
        question=question,
        objective=objective,
        scope=_as_string_tuple(payload.get("scope")),
        directions=directions,
        constraints=_as_string_tuple(payload.get("constraints")),
        expected_output=expected_output,
        revision=revision,
        memory_id=memory_id,
        memory_paths=memory_paths,
        known_information=known_information,
        research_gaps=research_gaps,
    )


def _assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": str(response.get("content") or ""),
    }
    if response.get("reasoning_content"):
        message["reasoning_content"] = response["reasoning_content"]
    return message


def _review_payload(brief: ResearchBrief) -> dict[str, Any]:
    return {
        "kind": "research_brief_confirmation",
        "brief": asdict(brief),
        "allowed_actions": ["confirm", "modify"],
    }


def _parse_review(value: Any) -> tuple[bool, str | None]:
    if isinstance(value, str):
        clean = value.strip()
        if clean.lower() in {"confirm", "confirmed", "approve", "approved", "确认"}:
            return True, None
        if not clean:
            raise ValueError("review response cannot be empty")
        return False, clean
    if not isinstance(value, dict):
        raise ValueError("review response must be a string or object")
    action = str(value.get("action") or "").strip().lower()
    if action == "confirm":
        return True, None
    if action == "modify":
        feedback = str(value.get("feedback") or value.get("message") or "").strip()
        if not feedback:
            raise ValueError("modify action requires feedback")
        return False, feedback
    raise ValueError("review action must be confirm or modify")


def create_research_workflow_state(
    question: str,
    identity: ExecutionIdentity,
    limits: AgentLimits | None = None,
    *,
    memory_id: str | None = None,
) -> ResearchWorkflowState:
    """Create the root workflow input used for the first ``ainvoke`` call."""
    if memory_id is not None:
        validate_memory_id(memory_id)
    return ResearchWorkflowState(
        question=question,
        memory_id=memory_id,
        retrieved_memory=[],
        brief=None,
        alignment_messages=[],
        revision_feedback=None,
        confirmed=False,
        identity=identity,
        limits=limits or AgentLimits(),
        report_markdown=None,
        memory_manifest=None,
        workflow_result=None,
        report_review=None,
        result=None,
    )


def build_research_workflow(
    policy: Any,
    tools: Iterable[Any],
    memory_store: MarkdownMemoryStore,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    report_review_enabled: bool = False,
) -> Any:
    """Build the root workflow around the same homogeneous Research AgentGraph."""
    tool_list = list(tools)
    effective_checkpointer = (
        checkpointer if checkpointer is not None else InMemorySaver()
    )
    research_agent_graph = build_research_agent_graph(
        policy,
        tool_list,
        inherit_checkpointer=True,
        child_checkpointer=effective_checkpointer,
    )
    memory_index = MarkdownMemoryIndex(memory_store)

    def validate_workflow_state(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> None:
        _validate_root_state(state, config)
        memory_id = state.get("memory_id")
        if memory_id is not None:
            memory_store.get_memory(validate_memory_id(memory_id))

    async def draft_brief(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        memory_id = state.get("memory_id")
        retrieved_memory: list[dict[str, Any]] = []
        if memory_id is not None:
            try:
                hits = memory_index.search(
                    memory_id,
                    state["question"],
                    limit=5,
                )
            except FileNotFoundError:
                # Compatible Stores may expose a descriptor without local Markdown.
                # A deleted selected Memory still fails this second existence check.
                memory_store.get_memory(memory_id)
                hits = ()
            retrieved_memory = _bounded_memory_hits(hits)
        messages = [
            {"role": "system", "content": _alignment_system_prompt()},
            *(
                [_memory_alignment_message(memory_id, retrieved_memory)]
                if memory_id is not None
                else []
            ),
            {"role": "user", "content": state["question"]},
        ]
        with _workflow_trace("draft_brief", state) as observation:
            response = await call_policy(policy, messages, [])
            brief = _parse_brief(
                str(response.get("content") or ""),
                question=state["question"],
                revision=0,
                memory_id=memory_id,
                retrieved_memory=retrieved_memory,
            )
            observation.add_output({"revision": brief.revision})
        return {
            "brief": brief,
            "retrieved_memory": retrieved_memory,
            "alignment_messages": [*messages, _assistant_message(response)],
            "revision_feedback": None,
            "confirmed": False,
        }

    def review_brief(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        brief = state.get("brief")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("draft_brief must produce a ResearchBrief")
        response = interrupt(_review_payload(brief))
        confirmed, feedback = _parse_review(response)
        return {
            "confirmed": confirmed,
            "revision_feedback": feedback,
        }

    async def revise_brief(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        current = state.get("brief")
        feedback = str(state.get("revision_feedback") or "").strip()
        if not isinstance(current, ResearchBrief):
            raise TypeError("cannot revise a missing ResearchBrief")
        if not feedback:
            raise ValueError("revision feedback cannot be empty")

        messages = [
            *state.get("alignment_messages", []),
            {
                "role": "user",
                "content": (
                    "Revise the research brief using this user feedback. Return the same "
                    f"JSON schema only.\n\nFeedback: {feedback}"
                ),
            },
        ]
        with _workflow_trace("revise_brief", state) as observation:
            response = await call_policy(policy, messages, [])
            brief = _parse_brief(
                str(response.get("content") or ""),
                question=state["question"],
                revision=current.revision + 1,
                memory_id=state.get("memory_id"),
                retrieved_memory=state.get("retrieved_memory", []),
            )
            observation.add_output({"revision": brief.revision})
        return {
            "brief": brief,
            "alignment_messages": [*messages, _assistant_message(response)],
            "revision_feedback": None,
            "confirmed": False,
        }

    def prepare_research(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        brief = state.get("brief")
        if not state.get("confirmed"):
            raise ValueError("research cannot start before user confirmation")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("confirmed workflow requires a ResearchBrief")
        task_context: dict[str, Any] = {
            "original_question": state["question"],
            "scope": list(brief.scope),
            "directions": list(brief.directions),
        }
        if brief.memory_id is not None:
            task_context.update(
                {
                    "retrieved_memory": state.get("retrieved_memory", []),
                    "known_information": list(brief.known_information),
                    "research_gaps": list(brief.research_gaps),
                }
            )
        task = ResearchTask(
            task_id=f"root-task-{state['identity'].root_thread_id}",
            objective=brief.objective,
            context=task_context,
            expected_output=brief.expected_output,
            constraints=brief.constraints,
            require_evidence=True,
        )
        return dict(
            create_research_agent_state(
                task,
                state["identity"],
                state["limits"],
            )
        )

    async def persist_result(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        brief = state.get("brief")
        result = state.get("result")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("workflow completed research without a ResearchBrief")
        if not isinstance(result, ResearchResult):
            raise TypeError("Research AgentGraph completed without a ResearchResult")
        with _workflow_trace("persist", state) as observation:
            memory_id = state.get("memory_id")
            if memory_id is None:
                report_markdown, manifest = await asyncio.to_thread(
                    memory_store.persist_research,
                    brief,
                    result,
                    state["identity"],
                )
            else:
                report_markdown, manifest = await asyncio.to_thread(
                    memory_store.persist_research,
                    brief,
                    result,
                    state["identity"],
                    memory_id=memory_id,
                )
            workflow_result = ResearchWorkflowResult(
                brief=brief,
                research_result=result,
                report_markdown=report_markdown,
                memory_manifest=manifest,
                memory_id=memory_id,
            )
            observation.add_output(
                {
                    "report_path": manifest.report_path,
                    "evidence_count": len(manifest.evidence_paths),
                    "source_count": len(manifest.source_paths),
                }
            )
        return {
            "report_markdown": report_markdown,
            "memory_manifest": manifest,
            "workflow_result": workflow_result,
            "report_review": None,
        }

    async def postprocess_report(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        if not report_review_enabled:
            return {"report_review": None}

        brief = state.get("brief")
        result = state.get("result")
        original_report = state.get("report_markdown")
        manifest = state.get("memory_manifest")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("report review requires a ResearchBrief")
        if not isinstance(result, ResearchResult):
            raise TypeError("report review requires a ResearchResult")
        if not isinstance(original_report, str) or not original_report.strip():
            raise TypeError("report review requires the persisted Markdown report")
        if not isinstance(manifest, MemoryManifest):
            raise TypeError("report review requires a MemoryManifest")

        with _workflow_trace("postprocess_report", state) as observation:
            try:
                final_report, outcome = await review_final_report(
                    policy,
                    original_report,
                    result,
                    manifest,
                )
                workflow_result = ResearchWorkflowResult(
                    brief=brief,
                    research_result=result,
                    report_markdown=final_report,
                    memory_manifest=manifest,
                    report_review=outcome,
                    memory_id=state.get("memory_id"),
                )
                if final_report != original_report:
                    await asyncio.to_thread(
                        memory_store.replace_report,
                        manifest.report_path,
                        final_report,
                    )
                observation.add_output(
                    {
                        "applied": outcome.applied,
                        "issue_count": len(outcome.issues),
                        "edit_count": len(outcome.edits),
                        "fallback": False,
                    }
                )
                return {
                    "report_markdown": final_report,
                    "workflow_result": workflow_result,
                    "report_review": outcome,
                }
            except Exception as exc:
                outcome = ReportReviewOutcome(
                    applied=False,
                    fallback_reason=f"{type(exc).__name__}: {exc}",
                )
                workflow_result = ResearchWorkflowResult(
                    brief=brief,
                    research_result=result,
                    report_markdown=original_report,
                    memory_manifest=manifest,
                    report_review=outcome,
                    memory_id=state.get("memory_id"),
                )
                observation.add_output(
                    {
                        "applied": False,
                        "issue_count": 0,
                        "edit_count": 0,
                        "fallback": True,
                        "fallback_reason": outcome.fallback_reason,
                    }
                )
                return {
                    "report_markdown": original_report,
                    "workflow_result": workflow_result,
                    "report_review": outcome,
                }

    def route_after_review(state: ResearchWorkflowState) -> str:
        return "prepare_research" if state.get("confirmed") else "revise_brief"

    builder = StateGraph(ResearchWorkflowState)
    builder.add_node("draft_brief", draft_brief)
    builder.add_node("review_brief", review_brief)
    builder.add_node("revise_brief", revise_brief)
    builder.add_node("prepare_research", prepare_research)
    builder.add_node("research_agent", research_agent_graph)
    builder.add_node("persist_result", persist_result)
    builder.add_node("postprocess_report", postprocess_report)
    builder.add_edge(START, "draft_brief")
    builder.add_edge("draft_brief", "review_brief")
    builder.add_conditional_edges(
        "review_brief",
        route_after_review,
        {
            "prepare_research": "prepare_research",
            "revise_brief": "revise_brief",
        },
    )
    builder.add_edge("revise_brief", "review_brief")
    builder.add_edge("prepare_research", "research_agent")
    builder.add_edge("research_agent", "persist_result")
    builder.add_edge("persist_result", "postprocess_report")
    builder.add_edge("postprocess_report", END)
    return builder.compile(checkpointer=effective_checkpointer)


async def resume_research_workflow(
    graph: Any,
    *,
    thread_id: str,
    action: str,
    feedback: str | None = None,
) -> ResearchWorkflowState:
    """Resume one interrupted brief review with a confirm or modify decision."""
    payload: dict[str, Any] = {"action": action}
    if feedback is not None:
        payload["feedback"] = feedback
    return await graph.ainvoke(
        Command(resume=payload),
        config={"configurable": {"thread_id": thread_id}},
    )

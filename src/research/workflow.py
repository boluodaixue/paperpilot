"""Root workflow: align, confirm, run the homogeneous graph, and persist."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import Any, Iterable, TypedDict

import yaml
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..utils.tracing import trace_block, trace_context
from .agent_graph import build_research_agent_graph, create_research_agent_state
from .citation_audit import (
    audit_citations,
    citation_followup_directives,
    deterministic_citation_issues,
    repair_citations,
)
from .memory import MarkdownMemoryStore, MemoryWriteConflictError
from .models import (
    AgentLimits,
    CriticalGap,
    EvidenceItem,
    ExecutionIdentity,
    MemoryManifest,
    NextResearchAction,
    OutputStatus,
    ReportReviewOutcome,
    RequirementCoverage,
    ResearchBrief,
    ResearchDecision,
    ResearchRequirement,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
    ResearchWorkflowResult,
    StrategyAttempt,
    TerminationReason,
)
from .policy import call_policy
from .rendering import (
    managed_note_id,
    render_evidence_note,
    render_report,
    render_source_note,
    report_note_id,
    safe_note_id,
    source_note_id,
    render_v2_report,
)
from .report_composer import EVIDENCE_MARKER, compose_report, drop_unsafe_markdown_lines
from .report_review import review_final_report
from .research_sufficiency import atomic_requirement_descriptions
from .research_challenge import (
    execute_supplemental_work_packets,
    merge_supervisor_outcome,
    run_research_challenge_loop,
)
from .research_planner import plan_research
from .research_worker import run_research_worker
from .research_supervisor import (
    SupervisorBudget,
    plan_supplemental_work_packets,
    run_research_supervisor,
)
from .retrieval import MarkdownMemoryIndex, MemorySearchHit
from .vault import validate_frontmatter, validate_memory_id
from .vault_write_service import VaultWriteService
from .v2_contracts import (
    CitationAuditOutcome,
    ReportDraft,
    ResearchArchitecture,
    ResearchChallengeLoopOutcome,
    ResearchPlan,
    SupervisorOutcome,
    SupervisorV2Config,
)

__all__ = [
    "ResearchWorkflowState",
    "build_research_workflow",
    "create_research_workflow_state",
    "resume_research_workflow",
]


class ResearchWorkflowState(TypedDict, total=False):
    """Fields used by the outer workflow and embedded Research AgentGraph."""

    question: str
    workflow_type: str
    workflow_status: str
    thread_id: str
    session_id: str | None
    created_at: float
    expires_at: float | None
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
    report_markdown: str | None
    memory_manifest: MemoryManifest | None
    workflow_result: ResearchWorkflowResult | None
    report_review: ReportReviewOutcome | None
    failure_code: str | None
    research_architecture: ResearchArchitecture
    supervisor_v2_config: SupervisorV2Config
    v2_plan: ResearchPlan | None
    v2_supervisor_outcome: SupervisorOutcome | None
    v2_challenge_outcome: ResearchChallengeLoopOutcome | None
    v2_report_draft: ReportDraft | None
    v2_citation_audit: CitationAuditOutcome | None
    v2_report_body: str | None
    v2_citation_followup_used: bool
    v2_citation_followup_question_ids: list[str]
    v2_citation_followup_guidance: dict[str, list[str]]


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _managed_research_timestamp(
    report_markdown: str,
    *,
    memory_id: str,
    root_thread_id: str,
) -> str:
    """Recover the one timestamp used by an already-published managed bundle."""
    lines = report_markdown.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise MemoryWriteConflictError("research persist replay conflict: existing report has no frontmatter")
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing is None:
        raise MemoryWriteConflictError("research persist replay conflict: existing report frontmatter is not closed")
    try:
        raw_frontmatter = yaml.safe_load("".join(lines[1:closing]))
        if not isinstance(raw_frontmatter, dict):
            raise ValueError("frontmatter must be a mapping")
        frontmatter = validate_frontmatter(raw_frontmatter)
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise MemoryWriteConflictError(
            "research persist replay conflict: existing report frontmatter is invalid"
        ) from exc
    if (
        frontmatter.get("type") != "report"
        or frontmatter.get("memory_id") != memory_id
        or frontmatter.get("root_thread_id") != root_thread_id
    ):
        raise MemoryWriteConflictError(
            "research persist replay conflict: existing report identity does not match checkpoint"
        )
    created_at = frontmatter.get("created_at")
    updated_at = frontmatter.get("updated_at")
    if not isinstance(created_at, str) or updated_at != created_at:
        raise MemoryWriteConflictError("research persist replay conflict: existing report timestamp is not stable")
    return created_at


def _reuse_existing_research_commit(
    memory_store: MarkdownMemoryStore,
    brief: ResearchBrief,
    result: ResearchResult,
    identity: ExecutionIdentity,
    *,
    memory_id: str | None,
    report_body_markdown: str | None = None,
) -> tuple[str, MemoryManifest] | None:
    """Return an exact prior commit, or reject an unsafe persist-node replay."""
    report_note = report_note_id(identity.root_thread_id)
    base_path = f"Memories/{memory_id}/" if memory_id is not None else ""
    report_path = f"{base_path}reports/{report_note}.md"
    try:
        existing_report = memory_store.read_text(report_path)
    except FileNotFoundError:
        return None

    timestamp = (
        _managed_research_timestamp(
            existing_report,
            memory_id=memory_id,
            root_thread_id=identity.root_thread_id,
        )
        if memory_id is not None
        else None
    )
    unique_evidence = list({item.evidence_id: item for item in result.evidence}.values())
    evidence_note_by_id = {
        evidence.evidence_id: (
            managed_note_id("Evidence", evidence.evidence_id)
            if memory_id is not None
            else safe_note_id("Evidence", evidence.evidence_id)
        )
        for evidence in unique_evidence
    }
    source_note_by_ref: dict[str, str] = {}
    for evidence in unique_evidence:
        source_note_by_ref.setdefault(evidence.source_ref, source_note_id(evidence))

    renderer = render_v2_report if report_body_markdown is not None else render_report
    renderer_kwargs = dict(
        report_note=report_note,
        evidence_notes=evidence_note_by_id,
        root_thread_id=identity.root_thread_id,
        memory_id=memory_id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    expected_report = (
        renderer(brief, result, report_body_markdown, **renderer_kwargs)
        if report_body_markdown is not None
        else renderer(brief, result, **renderer_kwargs)
    )
    expected_files: list[tuple[str, str]] = []
    source_paths: list[str] = []
    for source_ref, source_note in source_note_by_ref.items():
        evidence = next(item for item in unique_evidence if item.source_ref == source_ref)
        source_path = f"{base_path}sources/{source_note}.md"
        source_paths.append(source_path)
        expected_files.append(
            (
                source_path,
                render_source_note(
                    source_note,
                    evidence,
                    memory_id=memory_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
            )
        )

    evidence_paths: list[str] = []
    for evidence in unique_evidence:
        evidence_note = evidence_note_by_id[evidence.evidence_id]
        evidence_path = f"{base_path}evidence/{evidence_note}.md"
        evidence_paths.append(evidence_path)
        expected_files.append(
            (
                evidence_path,
                render_evidence_note(
                    evidence,
                    evidence_note=evidence_note,
                    source_note=source_note_by_ref[evidence.source_ref],
                    memory_id=memory_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
            )
        )

    if existing_report != expected_report:
        raise MemoryWriteConflictError(
            "research persist replay conflict: existing report content does not match checkpoint"
        )
    for path, expected_markdown in expected_files:
        try:
            existing_markdown = memory_store.read_text(path)
        except FileNotFoundError as exc:
            raise MemoryWriteConflictError(
                f"research persist replay conflict: committed bundle is missing {path}"
            ) from exc
        if existing_markdown != expected_markdown:
            raise MemoryWriteConflictError(f"research persist replay conflict: committed content changed at {path}")
    return existing_report, MemoryManifest(
        report_path=report_path,
        evidence_paths=tuple(evidence_paths),
        source_paths=tuple(source_paths),
    )


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
        raise ValueError("LangGraph configurable.thread_id must match identity.thread_id")
    if state.get("thread_id", identity.thread_id) != identity.thread_id:
        raise ValueError("workflow thread_id must match identity.thread_id")


@contextmanager
def _workflow_trace(name: str, state: ResearchWorkflowState):
    identity = state["identity"]
    metadata = {
        "thread_id": identity.thread_id,
        "parent_thread_id": identity.parent_thread_id,
        "root_thread_id": identity.root_thread_id,
        "depth": identity.depth,
        "memory_id": state.get("memory_id"),
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
    memory_paths = tuple(str(item["path"]) for item in fixed_memory if str(item.get("path") or "").strip())
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
                str(item["summary"]).strip() for item in fixed_memory if str(item.get("summary") or "").strip()
            )
        research_gaps = _as_string_tuple(payload.get("research_gaps")) if "research_gaps" in payload else directions
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


def _review_payload(
    brief: ResearchBrief,
    state: ResearchWorkflowState,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "research_brief_confirmation",
        "brief": asdict(brief),
        "allowed_actions": ["confirm", "modify", "cancel"],
        "workflow_id": state["thread_id"],
        "session_id": state.get("session_id"),
        "memory_id": state.get("memory_id"),
    }
    if state.get("expires_at") is not None:
        payload["expires_at"] = state["expires_at"]
    return payload


def _parse_review(value: Any) -> tuple[str, str | None]:
    if isinstance(value, str):
        clean = value.strip()
        if clean.lower() in {"confirm", "confirmed", "approve", "approved", "确认"}:
            return "confirm", None
        if not clean:
            raise ValueError("review response cannot be empty")
        return "modify", clean
    if not isinstance(value, dict):
        raise ValueError("review response must be a string or object")
    action = str(value.get("action") or "").strip().lower()
    if action == "confirm":
        return "confirm", None
    if action == "modify":
        feedback = str(value.get("feedback") or value.get("message") or "").strip()
        if not feedback:
            raise ValueError("modify action requires feedback")
        return "modify", feedback
    if action in {"cancel", "expire"}:
        return action, None
    raise ValueError("review action must be confirm, modify, cancel, or expire")


def create_research_workflow_state(
    question: str,
    identity: ExecutionIdentity,
    limits: AgentLimits | None = None,
    *,
    memory_id: str | None = None,
    session_id: str | None = None,
    created_at: float | None = None,
    expires_at: float | None = None,
) -> ResearchWorkflowState:
    """Create the root workflow input used for the first ``ainvoke`` call."""
    if memory_id is not None:
        validate_memory_id(memory_id)
    created = time.time() if created_at is None else float(created_at)
    if expires_at is not None and float(expires_at) <= created:
        raise ValueError("expires_at must be later than created_at")
    return ResearchWorkflowState(
        question=question,
        workflow_type="research",
        workflow_status="drafting",
        thread_id=identity.thread_id,
        session_id=session_id,
        created_at=created,
        expires_at=float(expires_at) if expires_at is not None else None,
        memory_id=memory_id,
        retrieved_memory=[],
        brief=None,
        alignment_messages=[],
        revision_feedback=None,
        confirmed=False,
        identity=identity,
        limits=limits or AgentLimits(),
        notepad_entries=[],
        report_markdown=None,
        memory_manifest=None,
        workflow_result=None,
        report_review=None,
        failure_code=None,
        result=None,
        research_architecture=ResearchArchitecture.LEGACY,
        supervisor_v2_config=SupervisorV2Config(),
        v2_plan=None,
        v2_supervisor_outcome=None,
        v2_challenge_outcome=None,
        v2_report_draft=None,
        v2_citation_audit=None,
        v2_report_body=None,
        v2_citation_followup_used=False,
        v2_citation_followup_question_ids=[],
        v2_citation_followup_guidance={},
    )


def _v2_event(kind: str, identity: ExecutionIdentity, **details: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "thread_id": identity.thread_id,
        "parent_thread_id": identity.parent_thread_id,
        "root_thread_id": identity.root_thread_id,
        "depth": identity.depth,
        **details,
    }


def _v2_evidence(outcome: SupervisorOutcome) -> tuple[EvidenceItem, ...]:
    return tuple({
        item.evidence_id: item
        for worker in outcome.worker_results
        for item in worker.evidence
    }.values())


def _safe_partial_report(
    outcome: SupervisorOutcome,
    issues: tuple[Any, ...],
    original_markdown: str = "",
) -> str:
    """Build a deterministic, cited fallback when model repair cannot be trusted."""
    if original_markdown.strip():
        unsafe = {
            str(issue.claim_text).strip()
            for issue in issues
            if str(getattr(issue, "claim_text", "")).strip()
        }
        revised = drop_unsafe_markdown_lines(original_markdown, unsafe)
        evidence = _v2_evidence(outcome)
        if (
            revised
            and EVIDENCE_MARKER.search(revised)
            and not deterministic_citation_issues(revised, evidence)
        ):
            return revised + "\n"
    lines = ["# Partial research result", "", "## Supported findings", ""]
    claims = [claim for worker in outcome.worker_results for claim in worker.claims]
    selected: list[Any] = []
    per_question: dict[str, int] = {}
    for claim in claims:
        if any(per_question.get(item, 0) >= 2 for item in claim.question_ids):
            continue
        selected.append(claim)
        for item in claim.question_ids:
            per_question[item] = per_question.get(item, 0) + 1
        if len(selected) >= 18:
            break
    claims = selected
    if claims:
        for claim in claims:
            markers = " ".join(f"[[EVIDENCE:{item}]]" for item in claim.evidence_ids)
            lines.append(f"- {claim.claim[:1000]} {markers}".rstrip())
    else:
        lines.append("- No source-locatable supported finding remained after citation audit.")
    lines.extend(("", "## Unresolved", ""))
    for issue in issues:
        lines.append(f"- Citation audit removed or downgraded: {issue.claim_text}")
    return "\n".join(lines).rstrip() + "\n"


def _safe_citation_outcome(
    outcome: SupervisorOutcome,
    markdown: str,
    evidence: tuple[EvidenceItem, ...],
    issues: tuple[Any, ...],
    unresolved: tuple[str, ...],
) -> tuple[str, CitationAuditOutcome]:
    body = _safe_partial_report(outcome, issues, markdown)
    remaining = deterministic_citation_issues(body, evidence)
    return body, CitationAuditOutcome(
        status="repaired" if not remaining else "partial",
        issues=remaining,
        repaired_markdown=body,
        unresolved=tuple(dict.fromkeys(unresolved)),
    )


def _v2_research_result(
    challenge: ResearchChallengeLoopOutcome,
    draft: ReportDraft,
    audit: CitationAuditOutcome,
) -> ResearchResult:
    supervisor = challenge.supervisor_outcome
    evidence = _v2_evidence(supervisor)
    claims = tuple(
        claim for worker in supervisor.worker_results for claim in worker.claims
    )
    unresolved = tuple(dict.fromkeys((
        *supervisor.unresolved_question_ids,
        *(text for worker in supervisor.worker_results for text in worker.unresolved),
        *draft.unresolved,
        *audit.unresolved,
        *(item.reason for item in challenge.challenges if item.status != "resolved"),
    )))
    critical_incomplete = bool(
        supervisor.unresolved_question_ids
        or draft.output_status is OutputStatus.FALLBACK
        or audit.status == "partial"
    )
    status = (
        ResearchStatus.COMPLETED
        if evidence and not critical_incomplete
        else ResearchStatus.PARTIAL
    )
    repair_actions = tuple(dict.fromkeys((
        *(("composer_safety_repair",)
          if draft.output_status is OutputStatus.REPAIRED else ()),
        *(("citation_repair",)
          if audit.status == "repaired" else ()),
    )))
    output_status = (
        OutputStatus.FALLBACK if draft.output_status is OutputStatus.FALLBACK
        else OutputStatus.REPAIRED
        if audit.status == "partial"
        else OutputStatus.VALID
    )
    return ResearchResult(
        task_id=f"v2-{supervisor.plan_id}",
        status=status,
        summary=(claims[0].claim if claims else "No supported finding was available."),
        findings=tuple(item.claim for item in claims),
        evidence=evidence,
        unresolved=unresolved,
        tool_alerts=tuple(dict.fromkeys((
            *(alert for worker in supervisor.worker_results for alert in worker.alerts),
            *challenge.quality_alerts,
        ))),
        termination_reason=supervisor.termination_reason,
        output_status=output_status,
        stop_reason=("citation_partial" if audit.status == "partial" else None),
        iterations=sum(item.usage.iterations for item in supervisor.worker_results),
        tool_calls_used=sum(item.usage.tool_calls for item in supervisor.worker_results),
        thread_count=1 + len(supervisor.worker_results),
        estimated_tokens_used=sum(item.usage.estimated_tokens for item in supervisor.worker_results),
        retries_used=sum(item.usage.retries for item in supervisor.worker_results),
        source_candidate_count=sum(
            item.usage.source_candidates for item in supervisor.worker_results
        ),
        source_open_count=sum(
            item.usage.sources_opened for item in supervisor.worker_results
        ),
        duplicate_source_count=sum(
            item.usage.duplicate_sources for item in supervisor.worker_results
        ),
        acquisition_call_count=sum(
            item.usage.acquisition_calls for item in supervisor.worker_results
        ),
        repair_applied=bool(repair_actions),
        repair_actions=repair_actions,
    )


def _append_v2_disclosures(
    markdown: str,
    challenge: ResearchChallengeLoopOutcome,
    audit: CitationAuditOutcome,
) -> str:
    lines = [markdown.rstrip(), "", "## 红方未解决问题 / Unresolved Red-team issues", ""]
    open_items = tuple(
        item
        for item in challenge.challenges
        if item.status in {"pending", "accepted", "deferred", "unresolved_disclosed"}
    )
    if open_items:
        lines.extend((
            "| 问题 | 重要程度 | 已采取行动 | 对正文影响 | 后续建议 |",
            "|---|---|---|---|---|",
        ))
        for item in open_items:
            reason = " ".join(item.reason.replace("|", "/").split())
            action = (
                "完成一次定向补研后仍未获得足够证据"
                if item.severity == "high" and challenge.supervisor_outcome.wave_count > 1
                else "记录并披露，未消耗额外补研波次"
            )
            impact = (
                "相关目标结论已从事实正文排除或降级"
                if item.target_claim_ids
                else "报告保留该范围限制，不作确定性结论"
            )
            followup = " ".join(
                (item.requested_evidence or item.suggested_query or "后续检查一手资料")
                .replace("|", "/")
                .split()
            )
            lines.append(
                f"| {reason} | {item.severity} | {action} | {impact} | {followup} |"
            )
    else:
        lines.append("- Red review has no unresolved issue requiring disclosure.")
    resolved = sum(item.status == "resolved" for item in challenge.challenges)
    rejected = sum(item.status == "rejected" for item in challenge.challenges)
    lines.extend(("", f"- Red review summary: {resolved} resolved, {len(open_items)} disclosed unresolved, {rejected} rejected."))
    if audit.issues:
        lines.extend(
            f"- Citation issue `{item.category}` ({item.severity}): {item.repair_action} — {item.claim_text}"
            for item in audit.issues
        )
    else:
        lines.append(f"- Citation audit status: {audit.status}.")
    lines.append(
        f"- Supplemental research waves: {max(0, challenge.supervisor_outcome.wave_count - 1)}."
    )
    if challenge.quality_alerts:
        lines.extend(f"- Quality alert: {item.message}" for item in challenge.quality_alerts)
    return "\n".join(lines).rstrip() + "\n"


def build_research_workflow(
    policy: Any,
    tools: Iterable[Any],
    memory_store: MarkdownMemoryStore,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    report_review_enabled: bool = False,
    research_architecture: ResearchArchitecture = ResearchArchitecture.LEGACY,
    supervisor_v2_config: SupervisorV2Config | None = None,
    vault_write_service: VaultWriteService | None = None,
) -> Any:
    """Build the root workflow around the same homogeneous Research AgentGraph."""
    tool_list = list(tools)
    effective_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    v2_settings = supervisor_v2_config or SupervisorV2Config()
    if not isinstance(research_architecture, ResearchArchitecture):
        research_architecture = ResearchArchitecture(str(research_architecture))
    if research_architecture is ResearchArchitecture.SUPERVISOR_V2:
        v2_settings.validate()
        if not v2_settings.enabled:
            raise ValueError("supervisor_v2 workflow requires enabled=true")
        if report_review_enabled:
            raise ValueError(
                "legacy report_review_enabled conflicts with supervisor_v2 Red/Citation gates"
            )
        research_agent_graph = None
    else:
        research_agent_graph = build_research_agent_graph(
            policy,
            tool_list,
            inherit_checkpointer=True,
            child_checkpointer=effective_checkpointer,
            tool_artifact_store=vault_write_service,
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
            *([_memory_alignment_message(memory_id, retrieved_memory)] if memory_id is not None else []),
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
            "workflow_status": "waiting_confirmation",
        }

    def review_brief(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        brief = state.get("brief")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("draft_brief must produce a ResearchBrief")
        response = interrupt(_review_payload(brief, state))
        action, feedback = _parse_review(response)
        expires_at = state.get("expires_at")
        if action != "expire" and expires_at is not None and time.time() >= expires_at:
            action = "expire"
            feedback = None
        return {
            "confirmed": action == "confirm",
            "revision_feedback": feedback,
            "workflow_status": (
                "cancelled"
                if action == "cancel"
                else "expired" if action == "expire" else "running" if action == "confirm" else "revising"
            ),
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
            "workflow_status": "waiting_confirmation",
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
        evidence_work_items = (
            brief.research_gaps if brief.memory_id is not None and brief.research_gaps else brief.directions
        )
        requirement_descriptions = atomic_requirement_descriptions(evidence_work_items)
        deliverable = f"Deliverable: {brief.expected_output.strip()}" if brief.expected_output.strip() else None
        if deliverable:
            requirement_descriptions.append(deliverable)
        requirement_descriptions = list(dict.fromkeys(requirement_descriptions))
        task_context["research_requirements"] = [
            {
                "requirement_id": f"R{index}",
                "description": description,
                "required": description != deliverable,
            }
            for index, description in enumerate(
                requirement_descriptions or [brief.objective],
                1,
            )
        ]
        task = ResearchTask(
            task_id=f"root-task-{state['identity'].root_thread_id}",
            objective=brief.objective,
            context=task_context,
            expected_output=brief.expected_output,
            constraints=brief.constraints,
            require_evidence=True,
        )
        if research_architecture is ResearchArchitecture.SUPERVISOR_V2:
            return {
                "task": task,
                "research_architecture": research_architecture,
                "supervisor_v2_config": v2_settings,
                "deadline_at": time.time() + state["limits"].max_elapsed_seconds,
                "execution_events": list(state.get("execution_events", [])),
                "workflow_status": "running",
            }
        prepared = dict(
            create_research_agent_state(
                task,
                state["identity"],
                state["limits"],
            )
        )
        prepared["workflow_status"] = "running"
        return prepared

    async def planning(state: ResearchWorkflowState, config: RunnableConfig) -> dict[str, Any]:
        validate_workflow_state(state, config)
        brief = state.get("brief")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("V2 planning requires a ResearchBrief")
        plan = await plan_research(brief, policy)
        return {
            "v2_plan": plan,
            "execution_events": [
                *state.get("execution_events", []),
                _v2_event("planning", state["identity"], status="completed", plan_id=plan.plan_id),
            ],
        }

    async def blue_research(state: ResearchWorkflowState, config: RunnableConfig) -> dict[str, Any]:
        validate_workflow_state(state, config)
        plan = state.get("v2_plan")
        if not isinstance(plan, ResearchPlan):
            raise TypeError("V2 blue research requires a ResearchPlan")
        limits = state["limits"]
        budget = SupervisorBudget(
            total_tool_calls=limits.max_total_tool_calls,
            total_tokens=limits.max_total_tokens,
            deadline_at=float(state["deadline_at"]),
        )
        outcome = await run_research_supervisor(
            plan,
            policy=policy,
            tools=tool_list,
            identity=state["identity"],
            limits=limits,
            settings=v2_settings,
            budget=budget,
            checkpointer=effective_checkpointer,
            tool_artifact_store=vault_write_service,
            checkpoint_thread_id=f"{state['identity'].thread_id}.v2-supervisor",
        )
        return {
            "v2_supervisor_outcome": outcome,
            "execution_events": [
                *state.get("execution_events", []),
                _v2_event(
                    "blue_research", state["identity"], status="completed",
                    worker_count=len(outcome.worker_results), wave_count=outcome.wave_count,
                ),
            ],
        }

    async def red_review_v2(state: ResearchWorkflowState, config: RunnableConfig) -> dict[str, Any]:
        validate_workflow_state(state, config)
        plan = state.get("v2_plan")
        supervisor = state.get("v2_supervisor_outcome")
        if not isinstance(plan, ResearchPlan) or not isinstance(supervisor, SupervisorOutcome):
            raise TypeError("V2 Red review requires plan and supervisor outcome")
        limits = state["limits"]
        outcome = await run_research_challenge_loop(
            plan,
            supervisor,
            policy=policy,
            tools=tool_list,
            identity=state["identity"],
            limits=limits,
            settings=v2_settings,
            budget=SupervisorBudget(
                limits.max_total_tool_calls,
                limits.max_total_tokens,
                float(state["deadline_at"]),
            ),
            checkpointer=effective_checkpointer,
            tool_artifact_store=vault_write_service,
            checkpoint_thread_id=f"{state['identity'].thread_id}.v2-supervisor",
        )
        events = [
            *state.get("execution_events", []),
            _v2_event(
                "red_review", state["identity"], status="completed",
                challenge_count=len(outcome.challenges),
                quality_alert_count=len(outcome.quality_alerts),
            ),
        ]
        if outcome.supervisor_outcome.wave_count > supervisor.wave_count:
            events.append(_v2_event(
                "supplemental", state["identity"], status="completed",
                wave_count=outcome.supervisor_outcome.wave_count,
            ))
        return {
            "v2_challenge_outcome": outcome,
            "v2_supervisor_outcome": outcome.supervisor_outcome,
            "execution_events": events,
        }

    async def drafting_v2(state: ResearchWorkflowState, config: RunnableConfig) -> dict[str, Any]:
        validate_workflow_state(state, config)
        plan = state.get("v2_plan")
        challenge = state.get("v2_challenge_outcome")
        if not isinstance(plan, ResearchPlan) or not isinstance(challenge, ResearchChallengeLoopOutcome):
            raise TypeError("V2 drafting requires reviewed research")
        draft = await compose_report(policy, plan, challenge)
        return {
            "v2_report_draft": draft,
            "execution_events": [
                *state.get("execution_events", []),
                _v2_event("drafting", state["identity"], status=draft.status),
            ],
        }

    async def citation_audit_v2(state: ResearchWorkflowState, config: RunnableConfig) -> dict[str, Any]:
        validate_workflow_state(state, config)
        draft = state.get("v2_report_draft")
        challenge = state.get("v2_challenge_outcome")
        if not isinstance(draft, ReportDraft) or not isinstance(challenge, ResearchChallengeLoopOutcome):
            raise TypeError("V2 citation audit requires a draft and reviewed research")
        evidence = _v2_evidence(challenge.supervisor_outcome)
        body = draft.markdown
        followup_question_ids: tuple[str, ...] = ()
        followup_guidance: dict[str, tuple[str, ...]] = {}
        if draft.status == "abstained":
            audit = CitationAuditOutcome(
                status="partial",
                unresolved=("Citation audit skipped because no valid evidence was available.",),
            )
        else:
            try:
                audit = await audit_citations(policy, body, evidence)
            except Exception as exc:
                deterministic = deterministic_citation_issues(body, evidence)
                audit = CitationAuditOutcome(
                    status="issues" if deterministic else "passed_deterministic",
                    issues=deterministic,
                    unresolved=(f"Semantic citation audit unavailable: {type(exc).__name__}: {exc}",),
                )
            if audit.issues:
                followup_question_ids, followup_guidance = (
                    citation_followup_directives(
                        audit.issues,
                        challenge.supervisor_outcome,
                    )
                )
                can_research = (
                    bool(followup_question_ids)
                    and not state.get("v2_citation_followup_used", False)
                    and challenge.supervisor_outcome.wave_count
                    < v2_settings.max_research_waves
                )
                if can_research:
                    audit = replace(audit, status="research_required")
                elif v2_settings.max_citation_repair_rounds:
                    try:
                        repaired = await repair_citations(policy, body, evidence, audit.issues)
                    except Exception as exc:
                        body, audit = _safe_citation_outcome(
                            challenge.supervisor_outcome,
                            body,
                            evidence,
                            audit.issues,
                            (f"Citation repair unavailable: {type(exc).__name__}: {exc}",),
                        )
                    else:
                        if repaired.status == "repaired" and repaired.repaired_markdown:
                            body = repaired.repaired_markdown
                            audit = repaired
                        else:
                            body, audit = _safe_citation_outcome(
                                challenge.supervisor_outcome,
                                body,
                                evidence,
                                audit.issues,
                                repaired.unresolved,
                            )
                else:
                    body, audit = _safe_citation_outcome(
                        challenge.supervisor_outcome,
                        body,
                        evidence,
                        audit.issues,
                        tuple(item.claim_text for item in audit.issues),
                    )
        body = _append_v2_disclosures(body, challenge, audit)
        result = _v2_research_result(challenge, draft, audit)
        return {
            "v2_citation_audit": audit,
            "v2_report_body": body,
            "result": result,
            "v2_citation_followup_question_ids": (
                list(followup_question_ids)
                if audit.status == "research_required"
                else []
            ),
            "v2_citation_followup_guidance": (
                {key: list(value) for key, value in followup_guidance.items()}
                if audit.status == "research_required"
                else {}
            ),
            "execution_events": [
                *state.get("execution_events", []),
                _v2_event(
                    "citation_audit", state["identity"], status=audit.status,
                    issue_count=len(audit.issues),
                ),
            ],
        }

    async def citation_followup_v2(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        plan = state.get("v2_plan")
        challenge = state.get("v2_challenge_outcome")
        if not isinstance(plan, ResearchPlan) or not isinstance(
            challenge, ResearchChallengeLoopOutcome
        ):
            raise TypeError("Citation follow-up requires reviewed V2 research")
        question_ids = tuple(state.get("v2_citation_followup_question_ids", []))
        guidance = state.get("v2_citation_followup_guidance", {})
        limits = state["limits"]
        budget = SupervisorBudget(
            limits.max_total_tool_calls,
            limits.max_total_tokens,
            float(state["deadline_at"]),
        )
        packets = plan_supplemental_work_packets(
            plan,
            v2_settings,
            budget,
            challenge.supervisor_outcome,
            question_ids,
            guidance_by_question=guidance,
        )
        packets = tuple(
            item
            for item in packets
            if item.max_tool_calls > 0 and item.token_budget > 0
        )
        results = (
            await execute_supplemental_work_packets(
                packets,
                plan=plan,
                policy=policy,
                tools=tool_list,
                identity=state["identity"],
                limits=limits,
                worker_runner=run_research_worker,
                checkpointer=effective_checkpointer,
                tool_artifact_store=vault_write_service,
            )
            if packets
            else ()
        )
        supervisor = (
            merge_supervisor_outcome(
                plan,
                challenge.supervisor_outcome,
                results,
            )
            if results
            else challenge.supervisor_outcome
        )
        reviewed = replace(
            challenge,
            supervisor_outcome=supervisor,
            supplemental_question_ids=tuple(dict.fromkeys((
                *challenge.supplemental_question_ids,
                *question_ids,
            ))),
            supplemental_packet_ids=tuple(dict.fromkeys((
                *challenge.supplemental_packet_ids,
                *(item.packet_id for item in packets),
            ))),
        )
        return {
            "v2_supervisor_outcome": supervisor,
            "v2_challenge_outcome": reviewed,
            "v2_report_draft": None,
            "v2_citation_audit": None,
            "v2_report_body": None,
            "result": None,
            "v2_citation_followup_used": True,
            "v2_citation_followup_question_ids": [],
            "v2_citation_followup_guidance": {},
            "execution_events": [
                *state.get("execution_events", []),
                _v2_event(
                    "supplemental",
                    state["identity"],
                    status="completed" if results else "unavailable",
                    source="citation_audit",
                    worker_count=len(results),
                ),
            ],
        }

    def route_after_citation_v2(state: ResearchWorkflowState) -> str:
        if (
            state.get("v2_citation_followup_question_ids")
            and not state.get("v2_citation_followup_used", False)
        ):
            return "citation_followup"
        return "persist_result"

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
        is_v2 = research_architecture is ResearchArchitecture.SUPERVISOR_V2
        report_body = state.get("v2_report_body") if is_v2 else None
        if is_v2 and (not isinstance(report_body, str) or not report_body.strip()):
            raise TypeError("V2 persistence requires a citation-approved report body")
        with _workflow_trace("persist", state) as observation:
            memory_id = state.get("memory_id")
            v2_persist_kwargs = (
                {"report_body_markdown": report_body} if is_v2 else {}
            )
            replayed = await asyncio.to_thread(
                _reuse_existing_research_commit,
                memory_store,
                brief,
                result,
                state["identity"],
                memory_id=memory_id,
                report_body_markdown=report_body,
            )
            if replayed is not None:
                report_markdown, manifest = replayed
            elif memory_id is None:
                report_markdown, manifest = await asyncio.to_thread(
                    memory_store.persist_research,
                    brief,
                    result,
                    state["identity"],
                    **v2_persist_kwargs,
                )
            elif vault_write_service is not None:
                report_markdown, manifest = await asyncio.to_thread(
                    vault_write_service.persist_research,
                    brief,
                    result,
                    state["identity"],
                    memory_id=memory_id,
                    **v2_persist_kwargs,
                )
            else:
                report_markdown, manifest = await asyncio.to_thread(
                    memory_store.persist_research,
                    brief,
                    result,
                    state["identity"],
                    memory_id=memory_id,
                    **v2_persist_kwargs,
                )
            challenge = state.get("v2_challenge_outcome")
            citation = state.get("v2_citation_audit")
            workflow_result = ResearchWorkflowResult(
                brief=brief,
                research_result=result,
                report_markdown=report_markdown,
                memory_manifest=manifest,
                memory_id=memory_id,
                research_architecture=research_architecture.value,
                challenges=(
                    tuple(asdict(item) for item in challenge.challenges)
                    if isinstance(challenge, ResearchChallengeLoopOutcome) else ()
                ),
                citation_issues=(
                    tuple(asdict(item) for item in citation.issues)
                    if isinstance(citation, CitationAuditOutcome) else ()
                ),
                supplemental_wave_count=(
                    max(0, challenge.supervisor_outcome.wave_count - 1)
                    if isinstance(challenge, ResearchChallengeLoopOutcome) else 0
                ),
                finalization_token_reserve=(
                    challenge.supervisor_outcome.finalization_token_reserve
                    if isinstance(challenge, ResearchChallengeLoopOutcome) else 0
                ),
                core_question_count=(
                    len(
                        tuple(
                            item
                            for item in state["v2_plan"].core_questions
                            if item.required
                        )
                        or state["v2_plan"].core_questions
                    )
                    if isinstance(state.get("v2_plan"), ResearchPlan) else 0
                ),
                assigned_core_question_count=(
                    len(challenge.supervisor_outcome.assigned_question_ids)
                    if isinstance(challenge, ResearchChallengeLoopOutcome) else 0
                ),
                worker_packet_count=(
                    len(challenge.supervisor_outcome.worker_results)
                    if isinstance(challenge, ResearchChallengeLoopOutcome) else 0
                ),
                unique_worker_packet_count=(
                    len({item.packet_id for item in challenge.supervisor_outcome.worker_results})
                    if isinstance(challenge, ResearchChallengeLoopOutcome) else 0
                ),
                source_open_count=(result.source_open_count if is_v2 else 0),
                source_candidate_count=(result.source_candidate_count if is_v2 else 0),
                duplicate_source_count=(
                    result.duplicate_source_count if is_v2 else 0
                ),
                acquisition_call_count=(
                    result.acquisition_call_count if is_v2 else 0
                ),
                repair_applied=(result.repair_applied if is_v2 else False),
                repair_actions=(result.repair_actions if is_v2 else ()),
            )
            observation.add_output(
                {
                    "memory_id": memory_id,
                    "report_path": manifest.report_path,
                    "evidence_paths": list(manifest.evidence_paths),
                    "source_paths": list(manifest.source_paths),
                    "evidence_count": len(manifest.evidence_paths),
                    "source_count": len(manifest.source_paths),
                }
            )
        return {
            "report_markdown": report_markdown,
            "memory_manifest": manifest,
            "workflow_result": workflow_result,
            "report_review": None,
            "execution_events": [
                *state.get("execution_events", []),
                *(
                    [_v2_event("persisting", state["identity"], status="completed")]
                    if is_v2 else []
                ),
            ],
        }

    async def review_report(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        validate_workflow_state(state, config)
        if research_architecture is ResearchArchitecture.SUPERVISOR_V2:
            return {"report_review": None}
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

        with _workflow_trace("review_report", state) as observation:
            try:
                final_report, outcome = await review_final_report(
                    policy,
                    original_report,
                    result,
                    manifest,
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
                    "report_review": outcome,
                }
            except Exception as exc:
                outcome = ReportReviewOutcome(
                    applied=False,
                    fallback_reason=f"{type(exc).__name__}: {exc}",
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
                    "report_review": outcome,
                }

    async def postprocess_report(
        state: ResearchWorkflowState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """Apply one checkpointed review result without re-running Red/Blue."""
        validate_workflow_state(state, config)
        if research_architecture is ResearchArchitecture.SUPERVISOR_V2:
            persisted = state.get("workflow_result")
            if not isinstance(persisted, ResearchWorkflowResult):
                raise TypeError("V2 postprocess requires the persisted workflow result")
            return {
                "workflow_result": persisted,
                "workflow_status": "completed",
                "report_review": None,
            }
        brief = state.get("brief")
        result = state.get("result")
        reviewed_report = state.get("report_markdown")
        manifest = state.get("memory_manifest")
        persisted = state.get("workflow_result")
        outcome = state.get("report_review")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("report postprocess requires a ResearchBrief")
        if not isinstance(result, ResearchResult):
            raise TypeError("report postprocess requires a ResearchResult")
        if not isinstance(reviewed_report, str) or not reviewed_report.strip():
            raise TypeError("report postprocess requires reviewed Markdown")
        if not isinstance(manifest, MemoryManifest):
            raise TypeError("report postprocess requires a MemoryManifest")
        if not isinstance(persisted, ResearchWorkflowResult):
            raise TypeError("report postprocess requires the persisted workflow result")
        if outcome is not None and not isinstance(outcome, ReportReviewOutcome):
            raise TypeError("report postprocess requires a ReportReviewOutcome")
        original_report = persisted.report_markdown

        with _workflow_trace("postprocess_report", state) as observation:
            try:
                if reviewed_report != original_report:
                    memory_id = state.get("memory_id")
                    if memory_id is not None and vault_write_service is not None:
                        await asyncio.to_thread(
                            vault_write_service.replace_report,
                            manifest.report_path,
                            reviewed_report,
                            memory_id=memory_id,
                            original_markdown=original_report,
                            manifest=manifest,
                            origin_thread_id=state["identity"].root_thread_id,
                        )
                    else:
                        await asyncio.to_thread(
                            memory_store.replace_report,
                            manifest.report_path,
                            reviewed_report,
                        )
                final_report = reviewed_report
                final_outcome = outcome
            except Exception as exc:
                try:
                    final_report = await asyncio.to_thread(
                        memory_store.read_text,
                        manifest.report_path,
                    )
                except Exception as read_exc:
                    final_report = original_report
                    reason = (
                        f"{type(exc).__name__}: {exc}; latest report could not be read: "
                        f"{type(read_exc).__name__}: {read_exc}"
                    )
                else:
                    reason = f"{type(exc).__name__}: {exc}"
                final_outcome = ReportReviewOutcome(
                    applied=False,
                    issues=() if outcome is None else outcome.issues,
                    edits=(),
                    fallback_reason=reason,
                )

            workflow_result = ResearchWorkflowResult(
                brief=brief,
                research_result=result,
                report_markdown=final_report,
                memory_manifest=manifest,
                report_review=final_outcome,
                memory_id=state.get("memory_id"),
            )
            observation.add_output(
                {
                    "applied": bool(final_outcome and final_outcome.applied),
                    "issue_count": 0 if final_outcome is None else len(final_outcome.issues),
                    "edit_count": 0 if final_outcome is None else len(final_outcome.edits),
                    "fallback": bool(final_outcome and final_outcome.fallback_reason),
                    "fallback_reason": (None if final_outcome is None else final_outcome.fallback_reason),
                }
            )
            return {
                "report_markdown": final_report,
                "workflow_result": workflow_result,
                "report_review": final_outcome,
                "workflow_status": "completed",
            }

    def route_after_review(state: ResearchWorkflowState) -> str:
        if state.get("workflow_status") in {"cancelled", "expired"}:
            return "terminal"
        return "prepare_research" if state.get("confirmed") else "revise_brief"

    builder = StateGraph(ResearchWorkflowState)
    builder.add_node("draft_brief", draft_brief)
    builder.add_node("review_brief", review_brief)
    builder.add_node("revise_brief", revise_brief)
    builder.add_node("prepare_research", prepare_research)
    builder.add_node("persist_result", persist_result)
    builder.add_node("review_report", review_report)
    builder.add_node("postprocess_report", postprocess_report)
    builder.add_edge(START, "draft_brief")
    builder.add_edge("draft_brief", "review_brief")
    builder.add_conditional_edges(
        "review_brief",
        route_after_review,
        {
            "prepare_research": "prepare_research",
            "revise_brief": "revise_brief",
            "terminal": END,
        },
    )
    builder.add_edge("revise_brief", "review_brief")
    if research_architecture is ResearchArchitecture.SUPERVISOR_V2:
        builder.add_node("planning", planning)
        builder.add_node("blue_research", blue_research)
        builder.add_node("red_review", red_review_v2)
        builder.add_node("drafting", drafting_v2)
        builder.add_node("citation_audit", citation_audit_v2)
        builder.add_node("citation_followup", citation_followup_v2)
        builder.add_edge("prepare_research", "planning")
        builder.add_edge("planning", "blue_research")
        builder.add_edge("blue_research", "red_review")
        builder.add_edge("red_review", "drafting")
        builder.add_edge("drafting", "citation_audit")
        builder.add_conditional_edges(
            "citation_audit",
            route_after_citation_v2,
            {
                "citation_followup": "citation_followup",
                "persist_result": "persist_result",
            },
        )
        builder.add_edge("citation_followup", "drafting")
        builder.add_edge("persist_result", "postprocess_report")
    else:
        builder.add_node("research_agent", research_agent_graph)
        builder.add_edge("prepare_research", "research_agent")
        builder.add_edge("research_agent", "persist_result")
        builder.add_edge("persist_result", "review_report")
        builder.add_edge("review_report", "postprocess_report")
    builder.add_edge("postprocess_report", END)
    return builder.compile(checkpointer=effective_checkpointer)


async def resume_research_workflow(
    graph: Any,
    *,
    thread_id: str,
    action: str,
    feedback: str | None = None,
) -> ResearchWorkflowState:
    """Resume one interrupted brief review with a confirm, modify, or stop decision."""
    payload: dict[str, Any] = {"action": action}
    if feedback is not None:
        payload["feedback"] = feedback
    return await graph.ainvoke(
        Command(resume=payload),
        config={"configurable": {"thread_id": thread_id}},
    )

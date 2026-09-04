"""Minimal contracts shared by every homogeneous Research Agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResearchStatus(str, Enum):
    """Outcome of one Research Agent's scoped task."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ResearchDecision(str, Enum):
    """The three decisions emitted by the in-graph sufficiency assessment."""

    CONTINUE = "continue"
    REPLAN = "replan"
    STOP_RESEARCH = "stop_research"


class RequirementStatus(str, Enum):
    """Evidence support state for one confirmed Research Brief requirement."""

    UNSUPPORTED = "unsupported"
    WEAK = "weak"
    SUPPORTED = "supported"
    CONFLICTED = "conflicted"


class TerminationReason(str, Enum):
    """Why research stopped, kept separate from the result status."""

    COVERAGE_COMPLETE = "coverage_complete"
    SATURATED = "saturated"
    EVIDENCE_EXHAUSTED = "evidence_exhausted"
    BUDGET_FORCED = "budget_forced"
    TOOL_FAILURE = "tool_failure"
    USER_CANCELLED = "user_cancelled"


class OutputStatus(str, Enum):
    """Whether the final structured output was direct, repaired, or synthesized."""

    VALID = "valid"
    REPAIRED = "repaired"
    FALLBACK = "fallback"


class ForkReason(str, Enum):
    """The three product-approved reasons for isolating work in a child Agent."""

    PARALLEL = "parallel"
    CONTEXT_ISOLATION = "context_isolation"
    DEEP_TOOL_CHAIN = "deep_tool_chain"


@dataclass(frozen=True)
class ResearchTask:
    """A scoped research objective passed to any level of Research Agent."""

    task_id: str
    objective: str
    context: dict[str, Any] = field(default_factory=dict)
    expected_output: str = "Evidence-backed findings and a concise summary."
    constraints: tuple[str, ...] = ()
    require_evidence: bool = True

    def validate(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not self.objective.strip():
            raise ValueError("objective must be a non-empty string")


@dataclass(frozen=True)
class ExecutionIdentity:
    """Canonical execution lineage for a root, child, or grandchild Agent."""

    thread_id: str
    parent_thread_id: str | None
    root_thread_id: str
    depth: int

    def validate(self) -> None:
        if not self.thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")
        if not self.root_thread_id.strip():
            raise ValueError("root_thread_id must be a non-empty string")
        if self.depth not in (0, 1, 2):
            raise ValueError("depth must be 0, 1, or 2")
        if self.depth == 0:
            if self.parent_thread_id is not None:
                raise ValueError("root execution requires parent_thread_id is None")
            if self.thread_id != self.root_thread_id:
                raise ValueError("root execution requires thread_id == root_thread_id")
        else:
            if not self.parent_thread_id or not self.parent_thread_id.strip():
                raise ValueError("child execution requires a parent_thread_id")
            if self.thread_id == self.parent_thread_id:
                raise ValueError("child thread_id must differ from parent_thread_id")


@dataclass(frozen=True)
class AgentLimits:
    """Hard bounds consumed by every level of the single AgentGraph."""

    max_iterations: int = 18
    max_tool_calls: int = 30
    max_tool_output_chars: int = 24000
    max_children_per_agent: int = 5
    max_fork_depth: int = 2
    max_concurrent_agents: int = 10
    max_total_agents: int = 24
    # Deprecated aliases retained for config/checkpoint compatibility.  They
    # are normalized into the canonical fields in ``__post_init__``.
    max_children: int = 5
    max_total_threads: int = 24
    max_total_tool_calls: int = 96
    max_elapsed_seconds: float = 900.0
    root_finalization_grace_seconds: float = 0.0
    max_total_tokens: int = 700000
    max_retries_per_action: int = 2
    max_total_retries: int = 12

    def __post_init__(self) -> None:
        default_children = 5
        default_total = 24
        if self.max_children_per_agent != default_children and self.max_children != default_children:
            if self.max_children_per_agent != self.max_children:
                raise ValueError(
                    "max_children_per_agent conflicts with deprecated max_children"
                )
        elif self.max_children_per_agent == default_children and self.max_children != default_children:
            object.__setattr__(self, "max_children_per_agent", self.max_children)
        elif self.max_children_per_agent != default_children and self.max_children == default_children:
            object.__setattr__(self, "max_children", self.max_children_per_agent)

        if self.max_total_agents != default_total and self.max_total_threads != default_total:
            if self.max_total_agents != self.max_total_threads:
                raise ValueError(
                    "max_total_agents conflicts with deprecated max_total_threads"
                )
        elif self.max_total_agents == default_total and self.max_total_threads != default_total:
            object.__setattr__(self, "max_total_agents", self.max_total_threads)
        elif self.max_total_agents != default_total and self.max_total_threads == default_total:
            object.__setattr__(self, "max_total_threads", self.max_total_agents)
        if self.max_concurrent_agents == 10 and self.max_total_agents < 10:
            object.__setattr__(self, "max_concurrent_agents", self.max_total_agents)

    @property
    def effective_max_children_per_agent(self) -> int:
        if "max_children_per_agent" not in self.__dict__:
            return int(getattr(self, "max_children", 5))
        return int(self.max_children_per_agent)

    @property
    def effective_max_total_agents(self) -> int:
        if "max_total_agents" not in self.__dict__:
            return int(getattr(self, "max_total_threads", 24))
        return int(self.max_total_agents)

    @property
    def effective_max_concurrent_agents(self) -> int:
        if "max_concurrent_agents" not in self.__dict__:
            return min(10, self.effective_max_total_agents)
        return int(self.max_concurrent_agents)

    def validate(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls cannot be negative")
        if self.max_tool_output_chars < 500:
            raise ValueError("max_tool_output_chars must be at least 500")
        if self.effective_max_children_per_agent < 0:
            raise ValueError("max_children_per_agent cannot be negative")
        if self.max_fork_depth not in (0, 1, 2):
            raise ValueError("max_fork_depth must be 0, 1, or 2")
        if self.effective_max_concurrent_agents < 1:
            raise ValueError("max_concurrent_agents must be at least 1")
        if self.effective_max_total_agents < 1:
            raise ValueError("max_total_agents must be at least 1")
        if self.effective_max_concurrent_agents > self.effective_max_total_agents:
            raise ValueError("max_concurrent_agents cannot exceed max_total_agents")
        if self.max_total_tool_calls < 0:
            raise ValueError("max_total_tool_calls cannot be negative")
        if self.max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive")
        if self.root_finalization_grace_seconds < 0:
            raise ValueError("root_finalization_grace_seconds cannot be negative")
        if self.max_total_tokens < 0:
            raise ValueError("max_total_tokens cannot be negative")
        if self.max_retries_per_action < 0:
            raise ValueError("max_retries_per_action cannot be negative")
        if self.max_total_retries < 0:
            raise ValueError("max_total_retries cannot be negative")


@dataclass(frozen=True)
class ForkCandidate:
    """A scoped child task proposal emitted by the same Research Agent loop."""

    objective: str
    expected_output: str
    requirement_ids: tuple[str, ...] = ()
    scope_signature: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[ForkReason, ...] = ()
    estimated_tool_calls: int = 0
    independent: bool = True


@dataclass(frozen=True)
class EvidenceItem:
    """Source-locatable evidence collected by a Research Agent."""

    evidence_id: str
    finding: str
    source_type: str
    title: str
    source_ref: str
    locator: str = ""
    excerpt: str = ""
    excerpt_type: str = "paraphrase"
    limitations: str = ""
    requirement_id: str = ""
    action_id: str = ""
    artifact_id: str = ""
    assignment_id: str = ""
    parent_assignment_id: str = ""


@dataclass(frozen=True)
class ToolAvailabilityAlert:
    """Checkpoint-derived warning that an external information path is unavailable."""

    alert_id: str
    tool: str
    category: str
    scope: str
    target: str
    message: str
    action_required: str
    circuit_open: bool = False
    error: str = ""


@dataclass(frozen=True)
class ResearchRequirement:
    """Stable necessary requirement derived from the confirmed Research Brief."""

    requirement_id: str
    description: str
    required: bool = True
    requires_external_evidence: bool = True


@dataclass(frozen=True)
class RequirementCoverage:
    """Current support assessment for one necessary requirement."""

    requirement_id: str
    status: RequirementStatus
    evidence_ids: tuple[str, ...] = ()
    rationale: str = ""
    remaining_gap: str | None = None


@dataclass(frozen=True)
class CriticalGap:
    """An unresolved gap and its expected impact on the final answer."""

    requirement_id: str
    reason: str
    impact: str = "high"


@dataclass(frozen=True)
class NextResearchAction:
    """A concrete, requirement-scoped action proposed by the assessment."""

    requirement_id: str
    strategy: str
    query: str
    expected_value: str
    expected_improvement: str
    action_id: str = ""


@dataclass(frozen=True)
class StrategyAttempt:
    """Checkpointed history of distinct strategies tried for one gap."""

    requirement_id: str
    strategy: str
    query: str
    outcome: str
    evidence_ids: tuple[str, ...] = ()
    action_id: str = ""
    artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchResult:
    """The only result shape exchanged between homogeneous Agents."""

    task_id: str
    status: ResearchStatus
    summary: str
    findings: tuple[str, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    unresolved: tuple[str, ...] = ()
    tool_alerts: tuple[ToolAvailabilityAlert, ...] = ()
    child_result_refs: tuple[str, ...] = ()
    stop_reason: str | None = None
    termination_reason: TerminationReason | None = None
    output_status: OutputStatus = OutputStatus.VALID
    coverage: tuple[RequirementCoverage, ...] = ()
    critical_gaps: tuple[CriticalGap, ...] = ()
    next_actions: tuple[NextResearchAction, ...] = ()
    strategy_attempts: tuple[StrategyAttempt, ...] = ()
    iterations: int = 0
    tool_calls_used: int = 0
    thread_count: int = 1
    estimated_tokens_used: int = 0
    retries_used: int = 0
    source_candidate_count: int = 0
    source_open_count: int = 0
    duplicate_source_count: int = 0
    acquisition_call_count: int = 0
    repair_applied: bool = False
    repair_actions: tuple[str, ...] = ()
    research_memo: str = ""
    report_markdown: str = ""


@dataclass(frozen=True)
class ResearchBrief:
    """User-reviewable research direction prepared before any research tools run."""

    question: str
    objective: str
    scope: tuple[str, ...]
    directions: tuple[str, ...]
    constraints: tuple[str, ...]
    expected_output: str
    revision: int = 0
    memory_id: str | None = None
    memory_paths: tuple[str, ...] = ()
    known_information: tuple[str, ...] = ()
    research_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryManifest:
    """Relative Markdown paths written by one idempotent Memory Store commit."""

    report_path: str
    evidence_paths: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryDescriptor:
    """Minimal identity and location contract for one long-lived Memory."""

    memory_id: str
    title: str
    relative_path: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryCitation:
    """One exact selected-Memory citation attached by PaperPilot."""

    relative_path: str
    title: str
    wikilink: str


@dataclass(frozen=True)
class MemoryAnswer:
    """Transient answer grounded only in cited notes from one Memory."""

    answer_id: str
    memory_id: str
    question: str
    markdown: str
    citations: tuple[MemoryCitation, ...]
    insufficient_evidence: tuple[str, ...]


@dataclass(frozen=True)
class MemoryNoteProposal:
    """Transient, validated proposal for one note and its Home update."""

    proposal_id: str
    answer_id: str
    memory_id: str
    note_id: str
    title: str
    target_path: str
    markdown: str
    wikilink: str
    source_paths: tuple[str, ...]
    home_path: str
    home_content_hash: str
    target_content_hash: str | None
    home_markdown: str


@dataclass(frozen=True)
class WikiClaim:
    """One Wiki statement grounded in existing Evidence notes."""

    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class WikiSection:
    """A titled group of grounded Wiki claims."""

    heading: str
    claims: tuple[WikiClaim, ...]


@dataclass(frozen=True)
class WikiDraft:
    """Client-held Wiki preview that is revalidated before publication."""

    memory_id: str
    action: str
    wiki_id: str
    target_path: str
    title: str
    markdown: str
    source_report_path: str
    source_report_hash: str
    evidence_ids: tuple[str, ...]
    integrated_report_ids: tuple[str, ...]
    expected_target_hash: str | None
    created_at: str
    generated_at: str


@dataclass(frozen=True)
class MemoryImportDuplicate:
    """Existing import returned without extraction, policy, or writes."""

    memory_id: str
    import_id: str
    source_kind: str
    source_ref: str
    locator: str
    content_hash: str
    attachment_path: str
    import_path: str
    note_path: str | None
    wikilinks: tuple[str, ...]


@dataclass(frozen=True)
class MemoryImportProposal:
    """Transient, zero-write proposal for one controlled Memory import."""

    proposal_id: str
    import_id: str
    note_id: str
    memory_id: str
    source_kind: str
    source_ref: str
    locator: str
    media_type: str
    byte_size: int
    content_hash: str
    attachment_path: str
    attachment_bytes: bytes = field(repr=False)
    import_path: str
    import_markdown: str
    import_wikilink: str
    note_path: str
    note_markdown: str
    note_wikilink: str
    note_source_paths: tuple[str, ...]
    home_path: str
    home_content_hash: str
    home_markdown: str


@dataclass(frozen=True)
class ReportIssue:
    """One structured Red review finding about the final report."""

    category: str
    target: str
    description: str


@dataclass(frozen=True)
class ReportEdit:
    """One Blue report-only operation; it never mutates research evidence."""

    operation: str
    target: str
    replacement: str = ""


@dataclass(frozen=True)
class ReportReviewOutcome:
    """Auditable result of the optional final-report Red/Blue pass."""

    applied: bool
    issues: tuple[ReportIssue, ...] = ()
    edits: tuple[ReportEdit, ...] = ()
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "edits", tuple(self.edits))


@dataclass(frozen=True)
class ResearchWorkflowResult:
    """Final output of the root research workflow."""

    brief: ResearchBrief
    research_result: ResearchResult
    report_markdown: str
    memory_manifest: MemoryManifest
    report_review: ReportReviewOutcome | None = None
    research_architecture: str = "legacy"
    challenges: tuple[dict[str, Any], ...] = ()
    citation_issues: tuple[dict[str, Any], ...] = ()
    supplemental_wave_count: int = 0
    finalization_token_reserve: int = 0
    core_question_count: int = 0
    assigned_core_question_count: int = 0
    worker_packet_count: int = 0
    unique_worker_packet_count: int = 0
    source_open_count: int = 0
    source_candidate_count: int = 0
    duplicate_source_count: int = 0
    acquisition_call_count: int = 0
    repair_applied: bool = False
    repair_actions: tuple[str, ...] = ()
    reportable_claim_rejection_count: int = 0
    candidate_claim_count: int = 0
    verified_claim_count: int = 0
    support_assessment_count: int = 0
    entailed_assessment_count: int = 0
    evidence_requirement_coverage: tuple[dict[str, Any], ...] = ()
    composer_claim_count: int = 0
    shared_comparison: bool = False
    structured_report: bool = False
    root_agent_report: bool = False
    shared_selected_evidence_count: int = 0
    coordination_metrics: dict[str, int] = field(default_factory=dict)
    memory_id: str | None = None

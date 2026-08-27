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
    """N1 hard bounds that are actually consumed by the single AgentGraph."""

    max_iterations: int = 8
    max_tool_calls: int = 12
    max_tool_output_chars: int = 12000

    def validate(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls cannot be negative")
        if self.max_tool_output_chars < 500:
            raise ValueError("max_tool_output_chars must be at least 500")


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


@dataclass(frozen=True)
class ResearchResult:
    """The only result shape exchanged between homogeneous Agents."""

    task_id: str
    status: ResearchStatus
    summary: str
    findings: tuple[str, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    unresolved: tuple[str, ...] = ()
    child_result_refs: tuple[str, ...] = ()
    stop_reason: str | None = None
    iterations: int = 0
    tool_calls_used: int = 0


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


@dataclass(frozen=True)
class MemoryManifest:
    """Relative Markdown paths written by one idempotent Memory Store commit."""

    report_path: str
    evidence_paths: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchWorkflowResult:
    """Final output of the N2 root workflow."""

    brief: ResearchBrief
    research_result: ResearchResult
    report_markdown: str
    memory_manifest: MemoryManifest

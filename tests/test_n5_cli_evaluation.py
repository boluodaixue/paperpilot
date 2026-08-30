from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evaluation.benchmarks.hotpotqa import HotpotQABenchmark
from evaluation.metrics.rule_based import RuleBasedMetrics
from evaluation.report import EvaluationReport
from scripts._workflow_cli import report_path, run_reviewed_workflow
from scripts.run_eval import (
    research_completion_score,
    researchbench_summary,
    workflow_metrics,
)
from scripts.run_judge import _judge_backend
from scripts.run_repl import _new_root_thread
from src.research.models import (
    EvidenceItem,
    CriticalGap,
    MemoryManifest,
    OutputStatus,
    RequirementCoverage,
    RequirementStatus,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchWorkflowResult,
    TerminationReason,
)


def _workflow_result(report: str = "# Report") -> ResearchWorkflowResult:
    brief = ResearchBrief(
        question="question",
        objective="objective",
        scope=("scope",),
        directions=("direction",),
        constraints=("constraint",),
        expected_output="report",
    )
    evidence = EvidenceItem(
        evidence_id="E-1",
        finding="finding",
        source_type="web",
        title="source",
        source_ref="https://example.com/source",
    )
    research = ResearchResult(
        task_id="task",
        status=ResearchStatus.PARTIAL,
        summary="summary",
        evidence=(evidence,),
        unresolved=("open",),
        stop_reason="tool_budget_exhausted",
        termination_reason=TerminationReason.BUDGET_FORCED,
        output_status=OutputStatus.VALID,
        coverage=(
            RequirementCoverage(
                requirement_id="R1",
                status=RequirementStatus.WEAK,
                evidence_ids=("E-1",),
                remaining_gap="A stronger source remains useful.",
            ),
        ),
        iterations=3,
        tool_calls_used=4,
        thread_count=2,
        estimated_tokens_used=500,
        retries_used=1,
    )
    return ResearchWorkflowResult(
        brief=brief,
        research_result=research,
        report_markdown=report,
        memory_manifest=MemoryManifest(
            report_path="reports/root.md",
            evidence_paths=("evidence/E-1.md",),
            source_paths=("sources/source.md",),
        ),
    )


class _ReviewRuntime:
    def __init__(self, root: Path) -> None:
        self.memory_store = SimpleNamespace(root=root)
        self.result = _workflow_result()
        self.calls: list[tuple[str, str | None]] = []

    async def start(self, question: str, *, thread_id: str) -> dict:
        self.calls.append(("start", thread_id))
        return {"brief": self.result.brief}

    async def review(self, thread_id: str, action: str, feedback: str | None = None) -> dict:
        self.calls.append((action, feedback))
        if action == "modify":
            return {"brief": replace(self.result.brief, revision=1, directions=(feedback or "",))}
        return {"workflow_result": self.result}


@pytest.mark.asyncio
async def test_cli_review_supports_modify_then_confirm(tmp_path: Path) -> None:
    runtime = _ReviewRuntime(tmp_path)
    answers = iter(["m", "focus on primary sources", "c"])
    output: list[str] = []

    result = await run_reviewed_workflow(
        runtime,  # type: ignore[arg-type]
        "question",
        thread_id="root-1",
        input_fn=lambda _: next(answers),
        output_fn=output.append,
    )

    assert result is runtime.result
    assert runtime.calls == [
        ("start", "root-1"),
        ("modify", "focus on primary sources"),
        ("confirm", None),
    ]
    assert any("Revision: 1" in item for item in output)
    assert report_path(runtime, result) == (tmp_path / "reports/root.md").resolve()  # type: ignore[arg-type]


def test_repl_session_prefix_does_not_reuse_root_identity() -> None:
    class Runtime:
        counter = 0

        def new_thread_id(self) -> str:
            self.counter += 1
            return f"research-{self.counter}"

    runtime = Runtime()
    first = _new_root_thread(runtime, "demo session")
    second = _new_root_thread(runtime, "demo session")

    assert first == "demo-session--research-1"
    assert second == "demo-session--research-2"
    assert first != second


def test_workflow_metrics_are_structured_and_do_not_invent_confidence() -> None:
    metrics = workflow_metrics(_workflow_result())

    assert metrics["status"] == "partial"
    assert metrics["research_status"] == "partial"
    assert metrics["research_brief"]["question"] == "question"
    assert metrics["evidence_count"] == 1
    assert metrics["source_count"] == 1
    assert metrics["thread_count"] == 2
    assert metrics["tool_calls_used"] == 4
    assert metrics["estimated_tokens_used"] == 500
    assert metrics["retries_used"] == 1
    assert metrics["termination_reason"] == "budget_forced"
    assert metrics["output_status"] == "valid"
    assert set(metrics["rcs"]) == {
        "objective_coverage",
        "evidence_sufficiency",
        "conflict_resolution",
        "uncertainty_calibration",
        "research_efficiency",
    }
    assert "overall" not in metrics["rcs"]
    assert "confidence" not in metrics
    assert metrics["rcs"] == {
        "objective_coverage": 0.0,
        "evidence_sufficiency": 0.5,
        "conflict_resolution": 1.0,
        "uncertainty_calibration": 0.0,
        "research_efficiency": 0.0,
    }


def test_rcs_scores_mixed_coverage_and_empty_coverage_without_false_credit() -> None:
    mixed_research = replace(
        _workflow_result().research_result,
        coverage=(
            RequirementCoverage("R1", RequirementStatus.SUPPORTED, ("E-1",)),
            RequirementCoverage("R2", RequirementStatus.WEAK, remaining_gap="weak"),
            RequirementCoverage("R3", RequirementStatus.CONFLICTED, remaining_gap="conflict"),
            RequirementCoverage("R4", RequirementStatus.UNSUPPORTED, remaining_gap="missing"),
        ),
        critical_gaps=(
            CriticalGap("R2", "weak"),
            CriticalGap("R3", "conflict"),
            CriticalGap("R4", "missing"),
        ),
        tool_calls_used=4,
    )
    mixed = research_completion_score(
        replace(_workflow_result(), research_result=mixed_research)
    )
    assert mixed == {
        "objective_coverage": 0.25,
        "evidence_sufficiency": 0.4375,
        "conflict_resolution": 0.75,
        "uncertainty_calibration": 1.0,
        "research_efficiency": 0.2,
    }

    empty = research_completion_score(
        replace(
            _workflow_result(),
            research_result=replace(_workflow_result().research_result, coverage=()),
        )
    )
    assert empty == {
        "objective_coverage": 0.0,
        "evidence_sufficiency": 0.0,
        "conflict_resolution": 0.0,
        "uncertainty_calibration": 0.0,
        "research_efficiency": 0.0,
    }


def test_researchbench_summary_is_comparison_ready_and_excludes_failed_rcs() -> None:
    report = EvaluationReport("summary", num_questions=2)
    report.add_detail(
        {
            "composite_score": 0.6,
            "research_status": "partial",
            "termination_reason": "budget_forced",
            "output_status": "valid",
            "elapsed_seconds": 12.5,
            "evidence_count": 3,
            "source_count": 2,
            "thread_count": 1,
            "tool_calls_used": 4,
            "estimated_tokens_used": 500,
            "iterations": 3,
            "unresolved_count": 1,
            "retries_used": 0,
            "rcs": {"objective_coverage": 0.5},
        }
    )
    report.add_detail(
        {
            "status": "failed",
            "error": "offline",
            "composite_score": 0.0,
            "elapsed_seconds": 1.5,
        }
    )

    summary = researchbench_summary(
        report,
        budget=36,
        local_budget=12,
        token_budget=120000,
    )
    assert summary["elapsed_seconds"] == 14.0
    assert summary["source_count"] == 2
    assert summary["iterations"] == 3
    assert summary["research_status_counts"] == {"partial": 1, "unknown": 1}
    assert summary["termination_reason_counts"] == {
        "budget_forced": 1,
        "not_available": 1,
    }
    assert summary["average_rcs"]["objective_coverage"] == 0.5


def test_judge_backend_uses_config_unless_explicitly_overridden() -> None:
    config = {"model": {"backend": "fallback", "backend_mapping": {"judge": "configured"}}}

    assert _judge_backend(config) == "configured"
    assert _judge_backend(config, "override") == "override"


def test_hotpotqa_skips_paperpilot_yaml_frontmatter() -> None:
    report = """---
id: reports/root
type: report
root_thread_id: root
---
# Question

答案：清朝
"""

    assert HotpotQABenchmark.extract_short_answer(report) == "清朝"


def test_rule_metrics_recognize_evidence_wikilinks() -> None:
    report = "Supported finding [[evidence/E-1|Evidence]].\nUncited paragraph."

    assert RuleBasedMetrics.citation_coverage(report) == 0.5

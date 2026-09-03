from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evaluation.benchmarks.hotpotqa import HotpotQABenchmark
from evaluation.metrics.rule_based import RuleBasedMetrics
from evaluation.judge import LLMJudge
from evaluation.report import EvaluationReport
from scripts._workflow_cli import report_path, run_reviewed_workflow
from scripts.run_eval import (
    _evaluate_research_bench,
    add_llm_judge_metrics,
    judge_sampling_kwargs,
    research_completion_score,
    researchbench_summary,
    run_or_resume_evaluation_workflow,
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
            "research_elapsed_seconds": 10.0,
            "rule_evaluation_elapsed_seconds": 0.5,
            "judge_elapsed_seconds": 2.0,
            "evidence_count": 3,
            "source_count": 2,
            "thread_count": 1,
            "tool_calls_used": 4,
            "estimated_tokens_used": 500,
            "iterations": 3,
            "unresolved_count": 1,
            "retries_used": 0,
            "rcs": {"objective_coverage": 0.5},
            "judge_status": "valid",
            "judge_average": 8.0,
            "rule_judge_composite_score": 0.68,
        }
    )
    report.add_detail(
        {
            "status": "failed",
            "error": "offline",
            "composite_score": 0.0,
            "elapsed_seconds": 1.5,
            "research_elapsed_seconds": 1.0,
            "rule_evaluation_elapsed_seconds": 0.5,
            "judge_elapsed_seconds": 0.0,
        }
    )

    summary = researchbench_summary(
        report,
        budget=36,
        local_budget=12,
        token_budget=120000,
        elapsed_budget=1800.0,
        finalization_grace=300.0,
    )
    assert summary["elapsed_seconds"] == 14.0
    assert summary["research_elapsed_seconds"] == 11.0
    assert summary["rule_evaluation_elapsed_seconds"] == 1.0
    assert summary["judge_elapsed_seconds"] == 2.0
    assert summary["source_count"] == 2
    assert summary["iterations"] == 3
    assert summary["research_status_counts"] == {"partial": 1, "unknown": 1}
    assert summary["termination_reason_counts"] == {
        "budget_forced": 1,
        "not_available": 1,
    }
    assert summary["average_rcs"]["objective_coverage"] == 0.5
    assert summary["elapsed_budget_seconds"] == 1800.0
    assert summary["root_finalization_grace_seconds"] == 300.0
    assert summary["judge_success"] == 1
    assert summary["judge_failed"] == 0
    assert summary["average_judge"] == 8.0


def test_llm_judge_metrics_keep_rule_and_judge_scores_separate() -> None:
    detail = {"composite_score": 0.6}
    add_llm_judge_metrics(
        detail,
        {
            "average": 8.0,
            "dimensions": {"factual_accuracy": {"score": 8}},
            "judge_backend": "test",
        },
    )

    assert detail["rule_composite_score"] == 0.6
    assert detail["judge_status"] == "valid"
    assert detail["judge_average"] == 8.0
    assert detail["rule_judge_composite_score"] == 0.68

    failed = {"composite_score": 0.5}
    add_llm_judge_metrics(failed, {"error": "offline", "judge_backend": "test"})
    assert failed["judge_status"] == "failed"
    assert failed["rule_judge_composite_score"] is None


def test_durable_evaluation_config_isolates_vault_and_databases(tmp_path) -> None:
    from scripts.run_eval import isolated_evaluation_config

    original = {
        "research": {"vault_root": "memory"},
        "runtime": {"retrieval_db_path": "data/retrieval.db"},
        "chat": {"db_path": "data/chat.db"},
    }
    checkpoint = tmp_path / "run" / "checkpoints.sqlite"

    isolated = isolated_evaluation_config(original, str(checkpoint))

    assert isolated["research"]["vault_root"] == str(checkpoint.parent / "vault")
    assert isolated["runtime"]["retrieval_db_path"] == str(
        checkpoint.parent / "retrieval.db"
    )
    assert isolated["chat"]["db_path"] == str(checkpoint.parent / "chat.db")
    assert original["research"]["vault_root"] == "memory"


def test_judge_sampling_uses_backend_defaults_and_module_override() -> None:
    config = {
        "model": {
            "backend": "test",
            "backend_sampling": {
                "test": {
                    "temperature": 0.7,
                    "max_tokens": 4096,
                    "top_p": 0.9,
                },
                "modules": {
                    "judge": {"temperature": 0.1, "max_tokens": 2048},
                },
            },
        },
    }

    assert judge_sampling_kwargs(config) == {
        "temperature": 0.1,
        "max_tokens": 2048,
        "top_p": 0.9,
    }


def test_llm_judge_forwards_sampling_to_model_router(monkeypatch) -> None:
    from src.models.model_router import ModelRouter

    sentinel = object()
    calls = []

    def create_backend(backend, **kwargs):
        calls.append((backend, kwargs))
        return sentinel

    monkeypatch.setattr(ModelRouter, "create_backend", create_backend)
    judge = LLMJudge(backend="test", temperature=0.1, max_tokens=2048)

    assert judge._get_policy() is sentinel
    assert judge._get_policy() is sentinel
    assert calls == [("test", {"temperature": 0.1, "max_tokens": 2048})]


def test_llm_judge_covers_long_report_in_bounded_chunks_and_validates_schema() -> None:
    calls: list[list[dict]] = []

    def policy(messages, **_kwargs):
        calls.append(messages)
        prompt = str(messages[-1]["content"])
        if "Do not assign final scores" in prompt:
            return {
                "content": (
                    '{"factual_observations":["fact"],'
                    '"logical_observations":["logic"],'
                    '"citation_observations":["citation"],'
                    '"coverage_observations":["coverage"],'
                    '"missing_or_uncertain":[]}'
                )
            }
        return {
            "content": (
                '{"factual_accuracy":{"score":8,"reason":"ok"},'
                '"logical_consistency":{"score":7,"reason":"ok"},'
                '"citation_quality":{"score":6,"reason":"ok"},'
                '"comprehensiveness":{"score":9,"reason":"ok"},'
                '"overall":{"score":8,"reason":"ok"}}'
            )
        }

    judge = LLMJudge(backend="test")
    judge._policy = policy
    result = judge.score_single("A" * 50_000, "query")

    assert result["average"] == 7.6
    assert result["judge_input"] == {
        "report_chars": 50_000,
        "chunks": 3,
        "complete_report_covered": True,
    }
    assert len(calls) == 4
    assert max(len(str(call[-1]["content"])) for call in calls) < 35_000


def test_llm_judge_scores_single_chunk_from_complete_report_directly() -> None:
    calls: list[list[dict]] = []

    def policy(messages, **_kwargs):
        calls.append(messages)
        assert "Complete research report" in str(messages[-1]["content"])
        assert "non-empty report body" in str(messages[-1]["content"])
        return {
            "content": (
                '{"factual_accuracy":{"score":8,"reason":"ok"},'
                '"logical_consistency":{"score":7,"reason":"ok"},'
                '"citation_quality":{"score":6,"reason":"ok"},'
                '"comprehensiveness":{"score":9,"reason":"ok"},'
                '"overall":{"score":8,"reason":"ok"}}'
            )
        }

    judge = LLMJudge(backend="test")
    judge._policy = policy
    result = judge.score_single("non-empty report body", "query")

    assert result["average"] == 7.6
    assert result["judge_input"]["complete_report_covered"] is True
    assert len(calls) == 1


def test_llm_judge_marks_invalid_chunk_observation_as_failed() -> None:
    calls = 0

    def policy(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return {"content": '{"unexpected": []}'}

    judge = LLMJudge(backend="test")
    judge._policy = policy
    result = judge.score_single("A" * 50_000, "query")

    assert result["error"] == "invalid Judge observation schema for chunk 1"
    assert result["judge_input"]["complete_report_covered"] is False
    assert calls == 2


def test_llm_judge_repairs_one_invalid_chunk_observation() -> None:
    calls: list[list[dict]] = []

    def policy(messages, **_kwargs):
        calls.append(messages)
        prompt = str(messages[-1]["content"])
        if prompt.startswith("REPAIR_CHUNK_OBSERVATION_JSON"):
            return {
                "content": (
                    '{"factual_observations":["fact"],'
                    '"logical_observations":[],"citation_observations":[],'
                    '"coverage_observations":[],"missing_or_uncertain":[]}'
                )
            }
        if "Do not assign final scores" in prompt:
            return {"content": '{"unexpected": []}'}
        return {
            "content": (
                '{"factual_accuracy":{"score":8,"reason":"ok"},'
                '"logical_consistency":{"score":7,"reason":"ok"},'
                '"citation_quality":{"score":6,"reason":"ok"},'
                '"comprehensiveness":{"score":9,"reason":"ok"},'
                '"overall":{"score":8,"reason":"ok"}}'
            )
        }

    judge = LLMJudge(backend="test")
    judge._policy = policy
    result = judge.score_single("A" * 50_000, "query")

    assert result["average"] == 7.6
    assert result["judge_input"]["complete_report_covered"] is True
    assert len(calls) == 7


@pytest.mark.asyncio
async def test_evaluation_workflow_resumes_confirmed_checkpoint() -> None:
    expected = _workflow_result("# Resumed")

    class Runtime:
        async def get_state(self, thread_id):
            assert thread_id == "researchbench-tech_001"
            return {"confirmed": True, "workflow_status": "running"}

        async def continue_research(self, thread_id):
            return {"workflow_result": expected}

        async def start(self, *_args, **_kwargs):
            raise AssertionError("a durable checkpoint must not restart")

    result = await run_or_resume_evaluation_workflow(
        Runtime(),
        "question",
        thread_id="researchbench-tech_001",
    )

    assert result is expected


@pytest.mark.asyncio
async def test_researchbench_fixture_runs_rule_judge_and_progress_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _workflow_result("# Complete fixture report")

    class FakeBench:
        def get_questions(self, **_kwargs):
            return [
                {
                    "id": "tech_001",
                    "query": "fixture question",
                    "ground_truth": {"fact": "expected"},
                }
            ]

        def evaluate_report(self, report, question_id):
            assert report == expected.report_markdown
            assert question_id == "tech_001"
            return {"question_id": question_id, "composite_score": 0.6}

    class FakeRuntime:
        async def get_state(self, thread_id):
            assert thread_id == "researchbench-tech_001"
            return {"workflow_result": expected}

    class FakeJudge:
        def __init__(self, backend, **policy_kwargs):
            assert backend == "test"
            assert policy_kwargs == {"temperature": 0.1, "max_tokens": 2048}

        def score_single(self, report, query, ground_truth):
            assert report == expected.report_markdown
            assert query == "fixture question"
            assert ground_truth == {"fact": "expected"}
            return {
                "average": 8.0,
                "dimensions": {"factual_accuracy": {"score": 8, "reason": "ok"}},
                "judge_backend": "test",
                "judge_input": {"complete_report_covered": True},
            }

    @asynccontextmanager
    async def fake_evaluation_runtime(config, checkpoint_db_path):
        assert config["model"]["backend"] == "test"
        assert checkpoint_db_path == str(tmp_path / "checkpoints.sqlite")
        yield FakeRuntime()

    monkeypatch.setattr("scripts.run_eval.ResearchBench", FakeBench)
    monkeypatch.setattr("scripts.run_eval.LLMJudge", FakeJudge)
    monkeypatch.setattr(
        "scripts.run_eval.evaluation_runtime",
        fake_evaluation_runtime,
    )

    report = await _evaluate_research_bench(
        1,
        None,
        {
            "model": {
                "backend": "test",
                "backend_sampling": {
                    "test": {"temperature": 0.7, "max_tokens": 4096},
                    "modules": {"judge": {"temperature": 0.1, "max_tokens": 2048}},
                },
            },
            "research": {
                "limits": {
                    "max_total_tool_calls": 36,
                    "max_tool_calls": 12,
                    "max_total_tokens": 120000,
                    "max_elapsed_seconds": 300.0,
                }
            },
        },
        question_ids=["tech_001"],
        use_llm_judge=True,
        checkpoint_db_path=str(tmp_path / "checkpoints.sqlite"),
        progress_output_dir=str(tmp_path),
    )

    detail = report.details[0]
    assert detail["rule_composite_score"] == 0.6
    assert detail["judge_average"] == 8.0
    assert detail["judge_status"] == "valid"
    assert detail["checkpoint_thread_id"] == "researchbench-tech_001"
    assert detail["research_elapsed_seconds"] >= 0.0
    assert detail["rule_evaluation_elapsed_seconds"] >= 0.0
    assert detail["judge_elapsed_seconds"] >= 0.0
    assert detail["elapsed_seconds"] >= (
        detail["research_elapsed_seconds"]
        + detail["rule_evaluation_elapsed_seconds"]
        + detail["judge_elapsed_seconds"]
    )
    assert report.summary["judge_success"] == 1
    progress = json.loads(
        (tmp_path / "ResearchBench_Evaluation_progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        progress["details"][0]["llm_judge"]["judge_input"][
            "complete_report_covered"
        ]
        is True
    )


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

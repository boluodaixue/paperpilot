from __future__ import annotations

from unittest.mock import patch

import pytest

from evaluation.benchmarks.hotpotqa import HotpotQABenchmark
from scripts.run_eval import (
    evaluate_hotpotqa,
    extract_grounded_hotpotqa_answer,
    smoke_evaluation_config,
)
from src.research.models import (
    MemoryManifest,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchWorkflowResult,
)


class _Runtime:
    def __init__(self, report: str) -> None:
        self.report = report
        self.queries: list[str] = []

    @staticmethod
    def new_thread_id() -> str:
        return "eval-root"

    async def run_auto_confirmed(self, query: str, *, thread_id: str) -> ResearchWorkflowResult:
        self.queries.append(query)
        brief = ResearchBrief(query, query, (), (), (), "report")
        result = ResearchResult("task", ResearchStatus.COMPLETED, "done")
        return ResearchWorkflowResult(
            brief,
            result,
            self.report,
            MemoryManifest("reports/eval-root.md"),
        )

    async def close(self) -> None:
        return None


def test_extracts_chinese_answer_field_after_title() -> None:
    report = "# 曹雪芹所处朝代\n\n答案：清朝\n\n## 分析\n曹雪芹生活于清代。"

    assert HotpotQABenchmark.extract_short_answer(report) == "清朝"
    assert HotpotQABenchmark.short_answer_extraction_method(report) == "explicit_答案"


def test_extracts_english_short_answer_and_conclusion_labels() -> None:
    short_answer_report = "# Research Report\n\nShort Answer: Paris\n\n## Evidence\nE-1: source"
    conclusion_report = "# Result\n\n## Conclusion\nVaswani et al., 2017."
    heading_inline_report = "# Result\n\n## Answer: Geoffrey Hinton"

    assert HotpotQABenchmark.extract_short_answer(short_answer_report) == "Paris"
    assert HotpotQABenchmark.extract_short_answer(conclusion_report) == "Vaswani et al., 2017."
    assert HotpotQABenchmark.extract_short_answer(heading_inline_report) == "Geoffrey Hinton"


def test_falls_back_to_first_body_statement_not_markdown_title() -> None:
    report = "# Transformer 架构研究\n\nTransformer 由 Vaswani 等人在 2017 年提出。\n\n更多分析。"

    assert HotpotQABenchmark.extract_short_answer(report) == "Transformer 由 Vaswani 等人在 2017 年提出。"
    assert HotpotQABenchmark.short_answer_extraction_method(report) == "first_body_statement"


def test_fallback_skips_research_brief_and_uses_summary() -> None:
    report = """---
id: Report-1
---

# 曹雪芹所处朝代

## Research Brief

**Objective:** 确认曹雪芹所生活的朝代。

**Expected output:** 明确回答朝代。

## Summary

曹雪芹生活于清朝。

## Execution

- Status: completed
"""

    assert HotpotQABenchmark.extract_short_answer(report) == "曹雪芹生活于清朝。"
    assert HotpotQABenchmark.short_answer_extraction_method(report) == "first_body_statement"


def test_seeded_mock_sampling_is_reproducible() -> None:
    benchmark = HotpotQABenchmark(use_mock=True)

    first = benchmark.get_samples(n=5, shuffle=True, seed=17)
    second = benchmark.get_samples(n=5, shuffle=True, seed=17)

    assert [item["query"] for item in first] == [item["query"] for item in second]


def test_chinese_f1_uses_character_tokens() -> None:
    score = HotpotQABenchmark.f1_score(
        "TSMC 4N（4纳米）制程，主要用于AI训练和推理",
        "4 纳米制程，主要用于 AI 训练和推理",
    )

    assert score > 0.8


def test_smoke_limits_are_explicit_and_do_not_mutate_default_config() -> None:
    config = {
        "research": {
            "limits": {
                "max_children": 4,
                "max_fork_depth": 2,
                "max_total_tool_calls": 36,
            }
        }
    }

    smoke = smoke_evaluation_config(config)

    assert config["research"]["limits"]["max_children"] == 4
    assert smoke["research"]["limits"] == {
        "max_children": 0,
        "max_fork_depth": 0,
        "max_total_tool_calls": 12,
        "max_iterations": 8,
        "max_tool_calls": 12,
        "max_total_threads": 1,
        "max_elapsed_seconds": 120.0,
        "max_total_tokens": 30000,
        "max_retries_per_action": 1,
        "max_total_retries": 1,
    }


def test_evaluate_reuses_precomputed_depth_metrics() -> None:
    benchmark = HotpotQABenchmark(use_mock=True)
    depth = {
        "gold_entity_coverage": 0.75,
        "semantic_gold_coverage": 0.5,
        "report_length": 100,
    }

    with patch.object(
        benchmark,
        "evaluate_report",
        side_effect=AssertionError("depth metrics must not be recomputed"),
    ):
        result = benchmark.evaluate(
            [
                {
                    "prediction": "清朝",
                    "gold": "清朝",
                    "report": "完整报告",
                    "depth_metrics": depth,
                }
            ]
        )

    assert result["gold_entity_coverage"] == 0.75
    assert result["semantic_gold_coverage"] == 0.5


def test_depth_metrics_keep_explicit_zero_values() -> None:
    benchmark = HotpotQABenchmark(use_mock=True)

    result = benchmark.evaluate(
        [
            {
                "prediction": "wrong",
                "gold": "answer",
                "report": "non-empty report",
                "depth_metrics": {
                    "gold_entity_coverage": 0.0,
                    "semantic_gold_coverage": 0.0,
                    "report_length": 16,
                },
            }
        ]
    )

    assert result["gold_entity_coverage"] == 0.0
    assert result["semantic_gold_coverage"] == 0.0


def test_empty_report_returns_empty_answer() -> None:
    assert HotpotQABenchmark.extract_short_answer("") == ""
    assert HotpotQABenchmark.short_answer_extraction_method("   ") == "empty_report"


def test_sources_and_evidence_are_never_selected() -> None:
    report = """# Report

## Sources
- https://example.com/paper
Evidence ID: E-1
[E-2] supporting excerpt
"""

    assert HotpotQABenchmark.extract_short_answer(report) == ""
    assert HotpotQABenchmark.short_answer_extraction_method(report) == "no_valid_candidate"

    plain_sources = "Sources:\n- A Paper That Is Not An Answer\nEvidence ID: E-3"
    assert HotpotQABenchmark.extract_short_answer(plain_sources) == ""


def test_fallback_ignores_sources_section_before_body_section() -> None:
    report = """# Report

## 参考文献
- https://example.com/paper

## 分析
正确答案是清朝。
"""

    assert HotpotQABenchmark.extract_short_answer(report) == "正确答案是清朝。"


def test_evaluate_hotpotqa_uses_extractor_and_keeps_full_report() -> None:
    full_report = "# A misleading title\n\n答案：清朝\n\n完整的报告正文。"
    sample = {"query": "曹雪芹生活在哪个朝代？", "expected_answer": "清朝"}
    runtime = _Runtime(full_report)

    with (
        patch("scripts.run_eval.HotpotQABenchmark.get_samples", return_value=[sample]),
        patch("scripts.run_eval.build_research_runtime", return_value=runtime),
        patch.object(
            HotpotQABenchmark, "extract_short_answer", wraps=HotpotQABenchmark.extract_short_answer
        ) as extractor,
        patch.object(
            HotpotQABenchmark,
            "evaluate_report",
            return_value={
                "gold_entity_coverage": 1.0,
                "semantic_gold_coverage": 1.0,
                "report_length": len(full_report),
            },
        ),
    ):
        result = evaluate_hotpotqa(1, {}, use_mock=True)

    extractor.assert_called_once_with(full_report, question=sample["query"])
    assert result.details[0]["prediction"] == "清朝"
    assert result.details[0]["prediction"] != "# A misleading title"
    assert result.details[0]["extraction_method"] == "explicit_答案"
    assert runtime.queries == ["曹雪芹生活在哪个朝代？"]


def test_evaluate_hotpotqa_uses_grounded_model_when_no_explicit_answer() -> None:
    full_report = "# Report\n\n## Summary\n\n曹雪芹生活于清朝。"
    sample = {"query": "曹雪芹生活在哪个朝代？", "expected_answer": "清朝"}
    runtime = _Runtime(full_report)

    with (
        patch(
            "scripts.run_eval.HotpotQABenchmark.get_samples",
            return_value=[sample],
        ),
        patch("scripts.run_eval.build_research_runtime", return_value=runtime),
        patch(
            "scripts.run_eval.extract_grounded_hotpotqa_answer",
            return_value="清朝",
        ) as grounded,
        patch.object(
            HotpotQABenchmark,
            "evaluate_report",
            return_value={
                "gold_entity_coverage": 1.0,
                "semantic_gold_coverage": 1.0,
                "report_length": len(full_report),
            },
        ),
    ):
        result = evaluate_hotpotqa(1, {}, use_mock=True)

    grounded.assert_awaited_once_with(full_report, sample["query"], {})
    assert result.details[0]["prediction"] == "清朝"
    assert result.details[0]["extraction_method"] == "grounded_model_extraction"
    assert result.summary["exact_match"] == 1.0


def test_grounded_model_failure_keeps_research_result() -> None:
    full_report = "# Report\n\n## Summary\n\n曹雪芹生活于清朝。"
    sample = {"query": "曹雪芹生活在哪个朝代？", "expected_answer": "清朝"}
    runtime = _Runtime(full_report)

    with (
        patch(
            "scripts.run_eval.HotpotQABenchmark.get_samples",
            return_value=[sample],
        ),
        patch("scripts.run_eval.build_research_runtime", return_value=runtime),
        patch(
            "scripts.run_eval.extract_grounded_hotpotqa_answer",
            side_effect=RuntimeError("formatter unavailable"),
        ),
        patch.object(
            HotpotQABenchmark,
            "evaluate_report",
            return_value={
                "gold_entity_coverage": 1.0,
                "semantic_gold_coverage": 1.0,
                "report_length": len(full_report),
            },
        ),
    ):
        result = evaluate_hotpotqa(1, {}, use_mock=True)

    assert result.details[0]["status"] == "completed"
    assert result.details[0]["prediction"] == "曹雪芹生活于清朝。"
    assert result.details[0]["extraction_method"] == "first_body_statement_after_model_failure"
    assert result.details[0]["answer_extraction_error"] == "RuntimeError: formatter unavailable"
    assert result.summary["num_failed"] == 0


@pytest.mark.asyncio
async def test_grounded_model_extraction_cleans_label(monkeypatch) -> None:
    def policy(messages):
        assert "QUESTION:\n曹雪芹生活在哪个朝代？" in messages[-1]["content"]
        assert "REPORT:\n曹雪芹生活于清朝。" in messages[-1]["content"]
        return {"content": "Short Answer: 清朝"}

    monkeypatch.setattr(
        "scripts.run_eval.ModelRouter.create_backend",
        lambda *args, **kwargs: policy,
    )

    answer = await extract_grounded_hotpotqa_answer(
        "曹雪芹生活于清朝。",
        "曹雪芹生活在哪个朝代？",
        {"model": {"backend": "test"}},
    )

    assert answer == "清朝"

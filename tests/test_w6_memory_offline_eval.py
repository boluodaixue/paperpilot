"""W6 fixed offline Memory benchmark and CLI routing tests."""
from __future__ import annotations

import json
import sys

from evaluation.benchmarks.memory_wiki import evaluate_memory_wiki
from evaluation.report import EvaluationReport
from scripts import run_eval


def test_fixed_memory_wiki_evaluation_passes_all_five_cases() -> None:
    report = evaluate_memory_wiki()

    assert report.num_questions == 5
    assert [detail["case_id"] for detail in report.details] == [
        "retrieval_hit",
        "citation_completeness",
        "unsupported_refusal",
        "controlled_write",
        "continued_research",
    ]
    assert all(detail["passed"] is True for detail in report.details)
    assert all(detail["checks"] for detail in report.details)
    assert report.summary == {
        "num_passed": 5,
        "num_failed": 0,
        "pass_rate": 1.0,
        "all_passed": True,
    }


def test_fixed_memory_wiki_evaluation_is_deterministic() -> None:
    first = evaluate_memory_wiki()
    second = evaluate_memory_wiki()

    assert second.details == first.details
    assert second.summary == first.summary


def test_memory_wiki_cli_uses_offline_benchmark_without_research_runtime(
    monkeypatch,
    capsys,
) -> None:
    report = EvaluationReport("MemoryWiki_Offline_Evaluation", num_questions=5)
    report.set_summary(
        {
            "num_passed": 5,
            "num_failed": 0,
            "pass_rate": 1.0,
            "all_passed": True,
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(run_eval, "evaluate_memory_wiki", lambda: report)
    monkeypatch.setattr(
        run_eval,
        "build_research_runtime",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("offline benchmark must not build a research runtime")
        ),
    )
    monkeypatch.setattr(
        EvaluationReport,
        "save",
        lambda self, output_dir, filename=None: calls.append(output_dir) or "offline.json",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval.py",
            "--benchmark",
            "memory_wiki",
            "--output-dir",
            "ignored-output",
        ],
    )

    run_eval.main()

    assert calls == ["ignored-output"]
    assert json.loads(capsys.readouterr().out) == report.summary

#!/usr/bin/env python3
"""Run PaperPilot's structured research workflow against supported benchmarks."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.benchmarks.hotpotqa import HotpotQABenchmark
from evaluation.benchmarks.research_bench import ResearchBench
from evaluation.report import EvaluationReport
from src.research.models import ResearchWorkflowResult
from src.research.runtime import build_research_runtime, load_config, setup_logging


def workflow_metrics(result: ResearchWorkflowResult) -> dict[str, Any]:
    """Expose measured workflow counters; never infer a confidence score."""
    research = result.research_result
    source_refs = {item.source_ref for item in research.evidence if item.source_ref}
    status = getattr(research.status, "value", research.status)
    return {
        "research_brief": asdict(result.brief),
        "status": str(status),
        "stop_reason": research.stop_reason,
        "evidence_count": len(research.evidence),
        "source_count": len(source_refs),
        "thread_count": research.thread_count,
        "tool_calls_used": research.tool_calls_used,
        "estimated_tokens_used": research.estimated_tokens_used,
        "retries_used": research.retries_used,
        "iterations": research.iterations,
        "unresolved_count": len(research.unresolved),
        "report_manifest": result.memory_manifest.report_path,
        "evidence_manifests": list(result.memory_manifest.evidence_paths),
        "source_manifests": list(result.memory_manifest.source_paths),
    }


async def _evaluate_research_bench(
    num_questions: int,
    domain: str | None,
    config: dict[str, Any],
) -> EvaluationReport:
    logger = logging.getLogger("run_eval")
    bench = ResearchBench()
    questions = bench.get_questions(domain=domain, n=num_questions)
    report = EvaluationReport(name="ResearchBench_Evaluation", num_questions=len(questions))
    runtime = build_research_runtime(config=config)
    try:
        for index, question in enumerate(questions, 1):
            question_id = question["id"]
            logger.info("[%s/%s] Evaluating %s", index, len(questions), question_id)
            started = time.monotonic()
            try:
                workflow = await runtime.run_auto_confirmed(
                    question["query"], thread_id=runtime.new_thread_id()
                )
                detail = bench.evaluate_report(workflow.report_markdown, question_id)
                detail.update(workflow_metrics(workflow))
                detail["elapsed_seconds"] = time.monotonic() - started
                report.add_detail(detail)
            except Exception as exc:
                logger.warning("Evaluation failed for %s: %s", question_id, exc)
                report.add_detail(
                    {
                        "question_id": question_id,
                        "status": "failed",
                        "error": str(exc),
                        "composite_score": 0.0,
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
    finally:
        await runtime.close()

    scores = [detail["composite_score"] for detail in report.details if "composite_score" in detail]
    report.set_summary(
        {
            "average_composite": sum(scores) / len(scores) if scores else 0.0,
            "num_success": sum("error" not in detail for detail in report.details),
            "num_failed": sum("error" in detail for detail in report.details),
            "evidence_count": sum(detail.get("evidence_count", 0) for detail in report.details),
            "thread_count": sum(detail.get("thread_count", 0) for detail in report.details),
            "tool_calls_used": sum(detail.get("tool_calls_used", 0) for detail in report.details),
            "estimated_tokens_used": sum(
                detail.get("estimated_tokens_used", 0) for detail in report.details
            ),
            "retries_used": sum(detail.get("retries_used", 0) for detail in report.details),
        }
    )
    return report


def evaluate_research_bench(
    num_questions: int,
    domain: str | None,
    config: dict[str, Any],
) -> EvaluationReport:
    return asyncio.run(_evaluate_research_bench(num_questions, domain, config))


async def _evaluate_hotpotqa(
    num_questions: int,
    config: dict[str, Any],
    use_mock: bool,
) -> EvaluationReport:
    logger = logging.getLogger("run_eval")
    bench = HotpotQABenchmark(use_mock=use_mock)
    questions = bench.get_samples(n=num_questions, shuffle=True)
    report = EvaluationReport(name="HotpotQA_PaperPilot_Evaluation", num_questions=len(questions))
    predictions: list[dict[str, Any]] = []
    runtime = build_research_runtime(config=config)
    try:
        for index, question in enumerate(questions, 1):
            query = question["query"]
            gold = question["expected_answer"]
            started = time.monotonic()
            try:
                workflow = await runtime.run_auto_confirmed(
                    query, thread_id=runtime.new_thread_id()
                )
                report_text = workflow.report_markdown
                prediction = bench.extract_short_answer(report_text, question=query)
                extraction_method = bench.short_answer_extraction_method(
                    report_text, question=query
                )
                structured = workflow_metrics(workflow)
            except Exception as exc:
                logger.warning("Evaluation failed for item %s: %s", index, exc)
                report_text = ""
                prediction = ""
                extraction_method = "research_failed"
                structured = {"status": "failed", "error": str(exc)}

            predictions.append(
                {
                    "query_id": index,
                    "prediction": prediction,
                    "gold": gold,
                    "report": report_text,
                }
            )
            depth = bench.evaluate_report(report_text, gold) if report_text else {}
            report.add_detail(
                {
                    "query_id": index,
                    "query": query,
                    "prediction": prediction,
                    "gold": gold,
                    "extraction_method": extraction_method,
                    "depth_metrics": depth,
                    "elapsed_seconds": time.monotonic() - started,
                    **structured,
                }
            )
    finally:
        await runtime.close()

    summary = bench.evaluate(predictions, metrics=["em", "f1", "pass@1"])
    summary.update(
        {
            "num_success": sum(detail.get("status") != "failed" for detail in report.details),
            "num_failed": sum(detail.get("status") == "failed" for detail in report.details),
            "evidence_count": sum(detail.get("evidence_count", 0) for detail in report.details),
            "thread_count": sum(detail.get("thread_count", 0) for detail in report.details),
            "tool_calls_used": sum(detail.get("tool_calls_used", 0) for detail in report.details),
            "estimated_tokens_used": sum(
                detail.get("estimated_tokens_used", 0) for detail in report.details
            ),
            "retries_used": sum(detail.get("retries_used", 0) for detail in report.details),
        }
    )
    report.set_summary(summary)
    return report


def evaluate_hotpotqa(
    num_questions: int,
    config: dict[str, Any],
    use_mock: bool = False,
) -> EvaluationReport:
    return asyncio.run(_evaluate_hotpotqa(num_questions, config, use_mock))


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperPilot benchmark evaluation")
    parser.add_argument("--benchmark", choices=["research_bench", "hotpotqa"], required=True)
    parser.add_argument("--num-questions", "--num_questions", type=int, default=20)
    parser.add_argument("--domain", default=None)
    parser.add_argument("--use-mock", "--use_mock", action="store_true")
    parser.add_argument("--config", default=None)
    parser.add_argument("--output-dir", "--output_dir", default="outputs/evaluation")
    parser.add_argument(
        "--log-level",
        "--log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()
    setup_logging(args.log_level)
    config = load_config(args.config)

    if args.benchmark == "research_bench":
        report = evaluate_research_bench(args.num_questions, args.domain, config)
    else:
        report = evaluate_hotpotqa(args.num_questions, config, use_mock=args.use_mock)

    path = report.save(args.output_dir)
    logging.getLogger("run_eval").info("Evaluation report saved: %s", path)
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
